"""Adapter runtime: owns the Telegram connection and moves messages between Telegram and MaiBot."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backend.client import LoginError, TelegramBackend
from .backend.models import Message, Peer
from .config import TelegramFullConfig
from .constants import GATEWAY_NAME, PLATFORM, PROTOCOL, STORE_FILENAME, VERSION
from .inbound.codec import InboundCodec
from .inbound.pipeline import InboundPipeline
from .media.cache import MediaCache
from .media.link_preview import LinkPreviewer
from .outbound.sender import OutboundSender
from .store import Store

_INITIAL_RETRY_DELAY = 5
_MESSAGE_META_RETENTION = 7 * 24 * 3600


class AdapterRuntime:
    def __init__(self, ctx: Any, config: Callable[[], TelegramFullConfig], logger: logging.Logger) -> None:
        self.ctx = ctx
        self.config = config
        self.logger = logger
        self.store: Store | None = None
        self.backend: TelegramBackend | None = None
        self.inbound: InboundCodec | None = None
        self.sender: OutboundSender | None = None
        self.me: Peer | None = None
        self.pipeline: InboundPipeline | None = None
        self._task: asyncio.Task[None] | None = None
        self._codes: asyncio.Queue[str] = asyncio.Queue()
        # A code already in the config belongs to an earlier login attempt and has expired.
        self._used_codes: set[str] = {config().account.login_code} - {""}

    # ---- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        cfg = self.config()
        logging.getLogger("telethon").setLevel(cfg.connection.telethon_log_level)
        try:
            import cryptg  # noqa: F401
        except ImportError:
            self.logger.warning("cryptg is not installed; Telegram encryption falls back to a much slower implementation")
        data_dir = Path(self.ctx.paths.data_dir)
        self.store = Store(data_dir / STORE_FILENAME)
        await self.store.open()
        await self.store.prune_messages(_MESSAGE_META_RETENTION)
        self.backend = TelegramBackend(
            session_path=data_dir / f"{cfg.account.session_name}.session",
            api_id=cfg.account.api_id,
            api_hash=cfg.account.api_hash,
            proxy=cfg.connection.proxy,
            flood_sleep_threshold=cfg.connection.flood_sleep_threshold,
            app_version=VERSION,
            logger=self.logger,
        )
        self.backend.on_new_message(self._on_message)
        self.backend.on_message_edited(self._on_edit)
        self.backend.on_message_deleted(self._on_delete)
        self.backend.on_typing(self._on_typing)
        self._task = asyncio.create_task(self._run(), name="telegram_full.connection")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_pipeline()
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await self.backend.disconnect()
        await self._set_ready(False)
        if self.store is not None:
            await self.store.close()

    def provide_login_code(self, code: str) -> None:
        if code and code not in self._used_codes:
            self._codes.put_nowait(code)

    # ---- connection loop ---------------------------------------------------------------

    async def _run(self) -> None:
        delay = _INITIAL_RETRY_DELAY
        while True:
            try:
                connected = await self._session()
                if connected:
                    delay = _INITIAL_RETRY_DELAY
                self.logger.warning("Disconnected from Telegram; reconnecting in %ss", delay)
            except asyncio.CancelledError:
                raise
            except LoginError as exc:
                self.logger.error("Telegram login failed: %s Fix the plugin configuration to retry.", exc)
                await self._set_ready(False, login_state="failed", login_error=str(exc))
                return
            except Exception as exc:
                self.logger.error("Telegram connection failed (%r); retrying in %ss", exc, delay)
            await self._set_ready(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.config().connection.reconnect_max_delay)

    async def _session(self) -> bool:
        """Connect, log in and serve until disconnected. Returns whether we got online."""
        assert self.backend is not None and self.store is not None
        backend = self.backend
        account = self.config().account
        await backend.connect()
        if account.type == "bot":
            me = await backend.login_bot(account.bot_token)
        else:
            me = await backend.login_user(account.phone, account.password, self._wait_for_code)
        self.me = me
        cache = MediaCache(self.store, self.ctx.db, self.logger)
        self.inbound = InboundCodec(
            me=me,
            settings=lambda: self.config().inbound,
            media_settings=lambda: self.config().media,
            download=backend.download,
            is_known=self.store.is_known_to_core,
            cache=cache,
            links=LinkPreviewer(self.store, lambda: self.config().media, self.logger),
            logger=self.logger,
        )
        self.sender = OutboundSender(
            backend=backend,
            store=self.store,
            settings=lambda: self.config().outbound,
            media_settings=lambda: self.config().media,
            cache=cache,
            logger=self.logger,
        )
        await self._close_pipeline()
        self.pipeline = InboundPipeline(
            ctx=self.ctx, me=me, backend=backend, store=self.store, codec=self.inbound, config=self.config,
            logger=self.logger,
        )
        self.pipeline.start()
        self.logger.info(
            "Connected to Telegram as %s (id=%s%s). Use platform account \"telegram:%s\" in MaiBot if needed.",
            me.name, me.id, f", @{me.username}" if me.username else "", me.id,
        )
        await self._set_ready(True)
        await backend.disconnected
        return True

    async def _close_pipeline(self) -> None:
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            await pipeline.close()

    async def _wait_for_code(self) -> str:
        await self._set_ready(False, login_state="waiting_for_code")
        while True:
            code = await self._codes.get()
            if code not in self._used_codes:
                self._used_codes.add(code)
                return code

    async def _set_ready(self, ready: bool, **extra: Any) -> None:
        me = self.me
        metadata = {"protocol": PROTOCOL, "account_type": self.config().account.type, **extra}
        if me is not None and me.username:
            metadata["username"] = me.username
        try:
            await self.ctx.gateway.update_state(
                GATEWAY_NAME,
                ready=ready,
                platform=PLATFORM,
                account_id=str(me.id) if me is not None else "",
                metadata=metadata,
            )
        except Exception as exc:
            self.logger.debug("Gateway state update failed: %r", exc)

    # ---- inbound (forwarded to the pipeline of the current session) ---------------------

    async def _on_message(self, message: Message) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_message(message)

    async def _on_edit(self, message: Message) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_edit(message)

    async def _on_delete(self, chat_id: int | None, msg_ids: list[int]) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_delete(chat_id, msg_ids)

    async def _on_typing(self, chat_id: int, user_id: int, active: bool) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_typing(chat_id, user_id, active)

    # ---- outbound ----------------------------------------------------------------------

    async def send_outbound(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.sender is None or self.backend is None or not self.backend.is_connected():
            return {"success": False, "error": "The Telegram adapter is not connected"}
        return await self.sender.send(message)
