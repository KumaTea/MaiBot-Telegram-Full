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
from .constants import GATEWAY_NAME, PLATFORM, PROTOCOL, STORE_FILENAME
from .inbound.codec import InboundCodec
from .inbound.filters import drop_reason
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
            logger=self.logger,
        )
        self.backend.on_new_message(self._on_message)
        self._task = asyncio.create_task(self._run(), name="telegram_full.connection")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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
        self.inbound = InboundCodec(
            me=me,
            settings=lambda: self.config().inbound,
            download=backend.download,
            is_known=self.store.is_known_to_core,
            logger=self.logger,
        )
        self.sender = OutboundSender(
            backend=backend, store=self.store, settings=lambda: self.config().outbound, logger=self.logger
        )
        self.logger.info(
            "Connected to Telegram as %s (id=%s%s). Use platform account \"telegram:%s\" in MaiBot if needed.",
            me.name, me.id, f", @{me.username}" if me.username else "", me.id,
        )
        await self._set_ready(True)
        await backend.disconnected
        return True

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

    # ---- inbound -----------------------------------------------------------------------

    async def _on_message(self, message: Message) -> None:
        me, store, codec = self.me, self.store, self.inbound
        if me is None or store is None or codec is None:
            return
        sender = message.sender
        await store.record_message(
            message.chat.id, message.id, sender.id if sender else None, bool(sender and sender.is_bot),
            is_outgoing=message.outgoing, routed=False, date=message.date.timestamp(),
        )
        reason = drop_reason(message, me, self.config().inbound)
        if reason is not None:
            self.logger.debug("Dropped %s/%s: %s", message.chat.id, message.id, reason)
            return
        payload = await codec.build(message)
        if payload is None:
            return
        message_id = payload["message_id"]
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            payload,
            route_metadata={"self_id": str(me.id), "platform_io_account_id": str(me.id)},
            external_message_id=message_id,
            dedupe_key=message_id,
        )
        if accepted:
            await store.mark_routed(message.chat.id, message.id)
        else:
            self.logger.debug("MaiBot did not accept %s (adapter chat policy?)", message_id)

    # ---- outbound ----------------------------------------------------------------------

    async def send_outbound(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.sender is None or self.backend is None or not self.backend.is_connected():
            return {"success": False, "error": "The Telegram adapter is not connected"}
        return await self.sender.send(message)
