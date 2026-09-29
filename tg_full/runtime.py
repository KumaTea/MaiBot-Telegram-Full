"""Adapter runtime: owns the Telegram connection and moves messages between Telegram and MaiBot."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from .backend.client import LoginError, TelegramBackend
from .backend.models import CallbackPress, Message, Peer, ReactionChange
from .backend.raw_api import RawApiError, build_request, result_to_json
from .config import TelegramFullConfig
from .constants import GATEWAY_NAME, PLATFORM, PROTOCOL, STORE_FILENAME, VERSION
from .ids import ChatTarget, decode_message_id, encode_message_id
from .inbound.codec import InboundCodec, sender_identity
from .inbound.pipeline import InboundPipeline
from .media.cache import MediaCache
from .media.link_preview import LinkPreviewer
from .outbound.sender import OutboundSender
from .outbound.telegraph import TelegraphClient, TelegraphError, derive_title, summary_excerpt, summary_language
from .store import Store
from .streams import StreamResolver
from .text.latex import latex_to_plain
from .text.length import check_length
from .text.md_out import render

_INITIAL_RETRY_DELAY = 5
_MAX_BUTTON_DATA_BYTES = 64  # Telegram's limit for callback data
_MESSAGE_META_RETENTION = 7 * 24 * 3600


class ActionError(RuntimeError):
    """A tool/API action failed; the message is meant for the caller (often an LLM)."""


def normalize_buttons(buttons: Any) -> list[list[dict[str, str]]]:
    """Validate button rows: ``[[{"text", "data"} | {"text", "url"}, …], …]`` (a flat list is one row)."""
    if not isinstance(buttons, list) or not buttons:
        raise ActionError("buttons must be a non-empty array of rows")
    if all(isinstance(b, dict) for b in buttons):
        buttons = [buttons]
    rows: list[list[dict[str, str]]] = []
    for raw_row in buttons:
        if not isinstance(raw_row, list) or not raw_row:
            raise ActionError("each button row must be a non-empty array")
        row = []
        for raw in raw_row:
            if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
                raise ActionError("each button needs a non-empty text")
            button = {"text": str(raw["text"]).strip()}
            if raw.get("url"):
                button["url"] = str(raw["url"])
            else:
                data = str(raw.get("data") or button["text"])
                if len(data.encode()) > _MAX_BUTTON_DATA_BYTES:
                    raise ActionError(f"button data must be at most {_MAX_BUTTON_DATA_BYTES} bytes: {data!r}")
                button["data"] = data
            row.append(button)
        rows.append(row)
    return rows


def _plain_emoji(value: str) -> str:
    """Compare emoji ignoring variation selectors (❤ vs ❤️)."""
    return value.replace("️", "").replace("︎", "").strip()


def _substitute_chat(value: Any, chat_id: int) -> Any:
    """Replace the placeholder ``"$chat"`` with the current chat's id, anywhere in the params."""
    if value == "$chat":
        return chat_id
    if isinstance(value, dict):
        return {k: _substitute_chat(v, chat_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_chat(v, chat_id) for v in value]
    return value


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
        self.streams: StreamResolver | None = None
        self.telegraph: TelegraphClient | None = None
        self.cache: MediaCache | None = None
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
        self.telegraph = TelegraphClient(self.store, lambda: self.config().outbound, self.logger)
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
        self.backend.on_reactions(self._on_reaction)
        self.backend.on_callback(self._on_callback)
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
        self.cache = MediaCache(self.store, self.ctx.db, self.logger)
        self.inbound = InboundCodec(
            me=me,
            settings=lambda: self.config().inbound,
            media_settings=lambda: self.config().media,
            download=backend.download,
            is_known=self.store.is_known_to_core,
            cache=self.cache,
            links=LinkPreviewer(self.store, lambda: self.config().media, self.logger),
            logger=self.logger,
        )
        self.sender = OutboundSender(
            backend=backend,
            store=self.store,
            settings=lambda: self.config().outbound,
            media_settings=lambda: self.config().media,
            cache=self.cache,
            logger=self.logger,
        )
        await self._close_pipeline()
        self.streams = StreamResolver(self.ctx, me)
        self.pipeline = InboundPipeline(
            ctx=self.ctx, me=me, backend=backend, store=self.store, codec=self.inbound, streams=self.streams,
            config=self.config, logger=self.logger,
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

    async def _on_reaction(self, change: ReactionChange) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_reaction(change)

    async def _on_callback(self, press: CallbackPress) -> None:
        if self.pipeline is not None:
            await self.pipeline.on_callback(press)

    # ---- actions for tools, hooks and APIs ---------------------------------------------

    def _online(self) -> tuple[TelegramBackend, StreamResolver, InboundPipeline]:
        if self.backend is None or self.streams is None or self.pipeline is None or not self.backend.is_connected():
            raise ActionError("The Telegram adapter is not connected")
        return self.backend, self.streams, self.pipeline

    async def _target(self, stream_id: str) -> ChatTarget:
        _, streams, _ = self._online()
        target = await streams.chat_for_stream(stream_id) if stream_id else None
        if target is None:
            raise ActionError("This chat is not a Telegram chat of this adapter")
        return target

    async def on_reply_started(self, session_id: str, attempt: int) -> None:
        """MaiBot started generating a reply: show "typing…" once (R19)."""
        if attempt != 1 or not self.config().outbound.typing_indicator or self.streams is None:
            return
        target = await self.streams.chat_for_stream(session_id)
        if target is not None and self.backend is not None and self.backend.is_connected():
            try:
                await self.backend.set_typing(target)
            except Exception as exc:
                self.logger.debug("Typing status failed in %s: %r", target.chat_id, exc)

    async def react(self, stream_id: str, msg_id: str, emoji: str, big: bool = False) -> dict[str, Any]:
        backend, _, _ = self._online()
        target = await self._target(stream_id)
        decoded = decode_message_id(msg_id, default_chat_id=target.chat_id)
        if decoded is None or decoded[0] != target.chat_id:
            raise ActionError(f"msg_id {msg_id!r} is not a message of this chat")
        try:
            await backend.send_reaction(target.chat_id, decoded[1], emoji.strip(), big)
        except Exception as exc:
            if "REACTION_INVALID" in str(exc) or "REACTIONS_TOO_MANY" in str(exc):
                raise ActionError(
                    f"Telegram rejected the reaction {emoji!r}: this chat does not allow it. Use a standard "
                    "reaction such as 👍 ❤️ 🔥 😁 🤔 😢 🎉 🙏 👌 😭 or 🤣"
                ) from exc
            raise ActionError(str(exc)) from exc
        return {"success": True, "content": f"已给消息 {msg_id} 添加表情回应 {emoji}" if emoji else "已取消表情回应"}

    async def send_buttons(
        self, stream_id: str, text: str, buttons: list[list[dict[str, str]]], receive_presses: bool = True
    ) -> dict[str, Any]:
        backend, _, pipeline = self._online()
        if self.me is None or not self.me.is_bot:
            raise ActionError("Only bot accounts can send inline buttons")
        target = await self._target(stream_id)
        rows = normalize_buttons(buttons)
        formatted = render(latex_to_plain(text), keep_entities=self.config().outbound.text_format == "markdown")
        if not formatted.text:
            raise ActionError("text must not be empty")
        check_length(formatted.text, 0)
        msg_id = await backend.send_text(target, formatted.text, formatted.entities, buttons=rows)
        callbacks = {b["data"]: b["text"] for row in rows for b in row if "data" in b}
        if receive_presses and callbacks:
            await pipeline.register_callbacks(exact=callbacks)
        assert self.store is not None
        await self.store.record_message(
            target.chat_id, msg_id, self.me.id, True, is_outgoing=True, routed=True, topic_id=target.topic_id,
            text=formatted.text[:500],
        )
        labels = " | ".join(b["text"] for row in rows for b in row)
        return {
            "success": True,
            "message_id": encode_message_id(target.chat_id, msg_id),
            "content": f"已发送带按钮的消息「{formatted.text[:100]}」，按钮: {labels}",
        }

    async def register_callback_pattern(self, pattern: str) -> dict[str, Any]:
        _, _, pipeline = self._online()
        try:
            await pipeline.register_callbacks(pattern=pattern)
        except re.error as exc:
            raise ActionError(f"Invalid regular expression: {exc}") from exc
        return {"success": True, "patterns": await pipeline.callback_patterns()}

    async def unregister_callback_pattern(self, pattern: str) -> dict[str, Any]:
        _, _, pipeline = self._online()
        removed = await pipeline.unregister_callback_pattern(pattern)
        return {"success": removed, "patterns": await pipeline.callback_patterns()}

    async def find_stickers(self, emoji: str = "", limit: int = 8) -> dict[str, Any]:
        """Stickers seen in chats (newest first), optionally only those for ``emoji``."""
        self._online()
        assert self.store is not None and self.cache is not None
        wanted = _plain_emoji(emoji)
        rows = [r for r in await self.store.recent_stickers() if not wanted or _plain_emoji(r["emoji"] or "") == wanted]
        found = []
        for row in rows[: max(1, min(int(limit or 8), 30))]:
            description = await self.cache.describe(row["sha256"], "emoji")
            found.append({"sticker_id": row["file_key"], "emoji": row["emoji"], "description": description})
        if not found:
            return {"success": True, "stickers": [], "content": f"没有找到{'「' + emoji + '」的' if emoji else ''}贴纸"}
        lines = [f"{s['sticker_id']} {s['emoji'] or ''} {s['description'] or '（尚未识别）'}" for s in found]
        return {"success": True, "stickers": found, "content": "可用贴纸（sticker_id emoji 描述）:\n" + "\n".join(lines)}

    async def send_sticker(self, stream_id: str, sticker_id: str) -> dict[str, Any]:
        self._online()
        assert self.store is not None and self.sender is not None
        row = await self.store.media_by_key(sticker_id.strip())
        if row is None or row["kind"] not in ("sticker", "animation"):
            raise ActionError(f"Unknown sticker_id {sticker_id!r}; use telegram_find_stickers first")
        target = await self._target(stream_id)
        try:
            msg_id = await self.sender.send_known(target, row)
        except Exception as exc:
            raise ActionError(f"Sending the sticker failed: {exc}") from exc
        return {"success": True, "message_id": encode_message_id(target.chat_id, msg_id),
                "content": f"已发送贴纸 {row['emoji'] or ''}".strip()}

    async def get_message(self, stream_id: str, msg_id: str) -> dict[str, Any]:
        backend, _, _ = self._online()
        assert self.inbound is not None
        target = await self._target(stream_id)
        decoded = decode_message_id(msg_id, default_chat_id=target.chat_id)
        if decoded is None or decoded[0] != target.chat_id:
            raise ActionError(f"msg_id {msg_id!r} is not a message of this chat")
        message = (await backend.get_messages(target.chat_id, [decoded[1]]))[0]
        if message is None:
            raise ActionError(f"Message {msg_id} does not exist any more or cannot be read")
        _, author = sender_identity(message)
        when = message.date.astimezone().strftime("%Y-%m-%d %H:%M")
        details = {"msg_id": encode_message_id(target.chat_id, message.id), "sender": author, "date": when}
        if message.reply_to_id is not None:
            details["reply_to"] = encode_message_id(target.chat_id, message.reply_to_id)
        if message.edit_date is not None:
            details["edited"] = message.edit_date.astimezone().strftime("%Y-%m-%d %H:%M")
        text = self.inbound.snapshot(message, limit=3000)
        return {"success": True, **details, "content": f"{author}（{when}）: {text}"}

    async def chat_info(self, stream_id: str) -> dict[str, Any]:
        backend, _, _ = self._online()
        target = await self._target(stream_id)
        info = await backend.chat_info(target.chat_id)
        if target.topic_id is not None:
            info["topic_id"] = target.topic_id
        summary = "，".join(f"{k}={v}" for k, v in info.items())
        return {"success": True, **info, "content": summary}

    async def post_long_text(
        self, stream_id: str, title: str, markdown: str, send_link: bool = True
    ) -> dict[str, Any]:
        """Publish ``markdown`` to Telegra.ph and (optionally) send it to the chat (R26.1–R26.3).

        The chat message is the link after either Telegram's AI summary of the article (user
        accounts with ``outbound.long_text_ai_summary``) or ``outbound.long_text_notice``. Telegram's
        link preview (Instant View) shows the title and opening of the page.
        """
        backend, _, _ = self._online()
        assert self.store is not None and self.me is not None and self.telegraph is not None
        if not markdown.strip():
            raise ActionError("markdown must not be empty")
        title = title.strip() or derive_title(markdown)
        author_url = f"https://t.me/{self.me.username}" if self.me.username else ""
        try:
            page = await self.telegraph.publish(title, markdown, self.me.name, author_url)
        except (TelegraphError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ActionError(f"Publishing to Telegraph failed: {exc!r}") from exc
        url = str(page["url"])
        self.logger.info("Published a Telegraph page: %s", url)
        result: dict[str, Any] = {"success": True, "url": url, "title": title}
        if send_link and stream_id:
            target = await self._target(stream_id)
            settings = self.config().outbound
            summary = None
            if settings.long_text_ai_summary and not self.me.is_bot:
                excerpt = summary_excerpt(markdown)
                summary = await backend.summarize_text(excerpt, summary_language(excerpt))
            if summary and len(summary) > 3500:
                summary = summary[:3500].rstrip() + "…"  # keep room for the link within 4096
            notice = summary or settings.long_text_notice.strip()
            text = f"{notice}\n{url}" if notice else url
            if summary:
                result["summary"] = summary
            msg_id = await backend.send_text(target, text, link_preview=True)
            await self.store.record_message(
                target.chat_id, msg_id, self.me.id, self.me.is_bot, is_outgoing=True, routed=True,
                topic_id=target.topic_id, text=f"{notice} {title} {url}".strip(),
            )
            result["message_id"] = encode_message_id(target.chat_id, msg_id)
            body = f"，消息正文是 Telegram AI 摘要：{summary}" if summary else ""
            result["content"] = f"已发布到 Telegraph（标题「{title}」）并在聊天中发送了链接：{url}{body}"
        else:
            result["content"] = f"已发布到 Telegraph：{url}"
        result["content"] += "。之后可用 telegram_edit_long_text 修改或清空这个页面"
        return result

    async def edit_long_text(self, page: str, markdown: str = "", title: str = "", clear: bool = False) -> dict[str, Any]:
        """Replace or clear a Telegraph page this adapter published; its link stays the same."""
        self._online()
        assert self.me is not None and self.telegraph is not None
        if not clear and not markdown.strip():
            raise ActionError("Give the complete new markdown, or clear=true to empty the page")
        author_url = f"https://t.me/{self.me.username}" if self.me.username else ""
        try:
            if clear:
                result = await self.telegraph.clear(page)
            else:
                result = await self.telegraph.edit(page, title.strip(), markdown, self.me.name, author_url)
        except (TelegraphError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ActionError(f"Editing the Telegraph page failed: {exc}") from exc
        url = str(result["url"])
        self.logger.info("%s a Telegraph page: %s", "Cleared" if clear else "Edited", url)
        done = "已清空" if clear else "已更新"
        return {"success": True, "url": url, "title": result.get("title", ""),
                "content": f"Telegraph 页面{done}：{url}（链接不变；Telegram 的即时预览可能稍后才刷新）"}

    async def list_long_texts(self) -> dict[str, Any]:
        self._online()
        assert self.telegraph is not None
        pages = await self.telegraph.pages()
        lines = [f"{p['url']} {p['title']}" for p in pages[:20]]
        return {"success": True, "pages": pages, "content": "\n".join(lines) or "还没有发布过 Telegraph 页面"}

    async def raw_invoke(self, method: str, params: Any = None, stream_id: str = "") -> dict[str, Any]:
        """Call any MTProto method (R4.1 / R4.2), when enabled and allowed by the lists."""
        backend, _, _ = self._online()
        settings = self.config().advanced
        if not settings.raw_api_enabled:
            raise ActionError("Raw MTProto calls are disabled (advanced.raw_api_enabled)")
        if isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except json.JSONDecodeError as exc:
                raise ActionError(f"params is not valid JSON: {exc}") from exc
        if stream_id and params:
            target = await self._target(stream_id)
            params = _substitute_chat(params, target.chat_id)
        try:
            request, canonical = build_request(method, params, settings.raw_api_allow, settings.raw_api_deny)
        except RawApiError as exc:
            raise ActionError(str(exc)) from exc
        self.logger.warning("Raw MTProto call: %s", canonical)
        try:
            result = await backend.invoke_raw(request)
        except Exception as exc:
            raise ActionError(f"{canonical} failed: {type(exc).__name__}: {exc}") from exc
        text, truncated = result_to_json(result, settings.raw_api_max_result_chars)
        note = "（结果已截断）" if truncated else ""
        return {"success": True, "method": canonical, "result": text, "truncated": truncated,
                "content": f"{canonical} 返回{note}: {text}"}

    # ---- outbound ----------------------------------------------------------------------

    async def send_outbound(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.sender is None or self.backend is None or not self.backend.is_connected():
            return {"success": False, "error": "The Telegram adapter is not connected"}
        return await self.sender.send(message)
