"""Inbound orchestration: Telegram events -> filters -> dispatcher -> MaiBot.

* New messages are filtered, queued in the per-chat dispatcher and delivered with ``route_message``.
* Edits (R7) replace a still-queued message, or become a debounced notice for one MaiBot has seen.
* Deletions drop a still-queued message, or become a notice for one MaiBot has seen.
* Typing (R9) holds a chat's queue.
* History polling (R11), for bots by default, re-reads recent messages to find edits and
  deletions; those only update MaiBot's context and never trigger a reply (R11.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from ..backend.client import TelegramBackend
from ..backend.models import Message, Peer
from ..config import TelegramFullConfig
from ..constants import GATEWAY_NAME, PLATFORM
from ..ids import encode_group_id
from ..store import Store
from .codec import InboundCodec, sender_identity
from .dispatcher import Debouncer, Dispatcher, Item, Notice
from .filters import drop_reason

POLICY_CACHE_SECONDS = 300
_STREAM_CACHE_SECONDS = 600
_POLL_WINDOW_SECONDS = 3600
_STACKABLE_REASONS = ("command", "command for another bot")


class InboundPipeline:
    def __init__(
        self,
        *,
        ctx: Any,
        me: Peer,
        backend: TelegramBackend,
        store: Store,
        codec: InboundCodec,
        config: Callable[[], TelegramFullConfig],
        logger: logging.Logger,
    ) -> None:
        self.ctx = ctx
        self.me = me
        self.backend = backend
        self.store = store
        self.codec = codec
        self.config = config
        self.logger = logger
        self.dispatcher = Dispatcher(lambda: config().dispatch, self._deliver, logger)
        self.edits = Debouncer(lambda: config().dispatch.edit_debounce, self._edit_settled)
        self._blocked_until: dict[int, float] = {}  # chat id -> monotonic deadline
        self._streams: dict[tuple[int, int | None], tuple[str, float]] = {}
        self._poll_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        mode = self.config().dispatch.history_poll
        if mode == "on" or (mode == "auto" and self.me.is_bot):
            self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram_full.history_poll")

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        await self.edits.close()
        await self.dispatcher.close()

    def _blocked(self, chat_id: int) -> bool:
        return self._blocked_until.get(chat_id, 0.0) > time.monotonic()

    # ---- Telegram events ---------------------------------------------------------------

    async def on_message(self, message: Message) -> None:
        if self._blocked(message.chat.id):
            return  # MaiBot's adapter policy rejects this chat (cached verdict)
        sender = message.sender
        await self.store.record_message(
            message.chat.id, message.id, sender.id if sender else None, bool(sender and sender.is_bot),
            is_outgoing=message.outgoing, routed=False, date=message.date.timestamp(), topic_id=message.topic_id,
        )
        settings = self.config()
        reason = drop_reason(message, self.me, settings.inbound)
        stacked = reason in _STACKABLE_REASONS and settings.inbound.command_filter_mode == "stack"
        if reason is not None and not stacked:
            self.logger.debug("Dropped %s/%s: %s", message.chat.id, message.id, reason)
            return
        urgent = message.chat.is_private or self.codec.mentions_me(message)
        self.dispatcher.add(Item(message.chat.id, message=message, triggers=not stacked, urgent=urgent))

    async def on_edit(self, message: Message) -> None:
        if message.chat.is_channel or self.dispatcher.replace_pending(message):
            return
        meta = await self.store.get_message_meta(message.chat.id, message.id)
        if meta is None or not meta["routed"]:
            return  # MaiBot never saw the original
        snapshot = self.codec.snapshot(message)
        if snapshot == (meta["text"] or ""):
            return  # reactions, pins, view counts and other non-content edits
        await self.store.set_snapshot(message.chat.id, message.id, snapshot)
        self.dispatcher.activity(message.chat.id)
        if self.config().dispatch.edit_notice:
            self.edits.push((message.chat.id, message.id), message, first=meta["text"])

    async def _edit_settled(self, key: Any, message: Message, old_text: str | None) -> None:
        actor_id, actor = sender_identity(message)
        new_text = self.codec.snapshot(message)
        change = f"「{old_text}」→「{new_text}」" if old_text else f"「{new_text}」"
        edited_at = int(message.edit_date.timestamp()) if message.edit_date else int(time.time())
        notice = Notice(
            chat=message.chat, topic_id=message.topic_id, actor_id=actor_id, actor_name=actor,
            text=f"{actor} 编辑了消息: {change}", key=f"{message.chat.id}:{message.id}:edit:{edited_at}",
        )
        self.dispatcher.add(Item(message.chat.id, notice=notice, urgent=self.codec.mentions_me(message)))

    async def on_delete(self, chat_id: int | None, msg_ids: list[int]) -> None:
        if chat_id is not None:
            pairs = [(chat_id, msg_id) for msg_id in msg_ids]
        else:
            pairs = await self.store.find_common_box(msg_ids)
        for chat, msg_id in pairs:
            if self.dispatcher.remove_pending(chat, {msg_id}):
                continue
            meta = await self.store.get_message_meta(chat, msg_id)
            await self.store.forget_message(chat, msg_id)
            if meta is None or not meta["routed"] or not self.config().dispatch.deletion_notice:
                continue
            peer = await self.backend.peer(chat)
            if peer is None:
                continue
            self.dispatcher.activity(chat)
            notice = Notice(
                chat=peer, topic_id=meta["topic_id"],
                actor_id=str(meta["sender_id"] or chat), actor_name=self._author(meta),
                text=self._deletion_text(meta), key=f"{chat}:{msg_id}:deleted",
            )
            self.dispatcher.add(Item(chat, notice=notice))

    async def on_typing(self, chat_id: int, user_id: int, active: bool) -> None:
        if user_id != self.me.id:
            self.dispatcher.typing(chat_id, user_id, active)

    def _author(self, meta: dict[str, Any]) -> str:
        if meta["is_outgoing"] or meta["sender_id"] == self.me.id:
            return "你"
        return meta["sender_name"] or "某人"

    def _deletion_text(self, meta: dict[str, Any]) -> str:
        content = f": 「{meta['text']}」" if meta["text"] else ""
        return f"{self._author(meta)}发送的一条消息被删除了{content}"

    # ---- delivery ----------------------------------------------------------------------

    async def _deliver(self, item: Item, context_only: bool) -> None:
        if item.notice is not None:
            await self._route(self.codec.build_notice(item.notice), item.chat_id)
            return
        message = item.message
        assert message is not None
        if context_only:
            # Stacked items that no real message came to pick up: context only, never a trigger.
            _, author = sender_identity(message)
            await self.append_context(message.chat.id, message.topic_id, f"{author}: {self.codec.snapshot(message)}")
            return
        payload = await self.codec.build(message)
        if payload is None:
            return
        if await self._route(payload, message.chat.id):
            await self.store.mark_routed(
                message.chat.id, message.id, sender_identity(message)[1], self.codec.snapshot(message)
            )

    async def _route(self, payload: dict[str, Any], chat_id: int) -> bool:
        """Hand a message to MaiBot; remember chats that MaiBot's adapter policy blocks.

        User accounts sit in many chats. Caching the policy verdict avoids downloading media
        for chats that MaiBot would reject anyway. Policy edits apply within the cache TTL.
        """
        message_id = payload["message_id"]
        # The raw host call (what ctx.gateway.route_message wraps) also returns the policy verdict.
        result = await self.ctx.call_host_method(
            "host.route_message",
            payload={
                "gateway_name": GATEWAY_NAME,
                "message": payload,
                "route_metadata": {"self_id": str(self.me.id), "platform_io_account_id": str(self.me.id)},
                "external_message_id": message_id,
                "dedupe_key": message_id,
            },
        )
        if isinstance(result, dict) and result.get("accepted"):
            return True
        route_key = result.get("route_key") if isinstance(result, dict) else None
        policy = route_key.get("policy") if isinstance(route_key, dict) else None
        if isinstance(policy, dict) and policy.get("allowed") is False:
            if not self._blocked(chat_id):
                self.logger.info(
                    "Chat %s is blocked by MaiBot's adapter policy (%s); ignoring it for %ss",
                    chat_id, policy.get("reason"), POLICY_CACHE_SECONDS,
                )
            self._blocked_until[chat_id] = time.monotonic() + POLICY_CACHE_SECONDS
        else:
            self.logger.debug("MaiBot did not accept %s: %s", message_id, result)
        return False

    async def _stream_id(self, chat_id: int, topic_id: int | None) -> str | None:
        """MaiBot's chat stream (session) id for a Telegram chat of this account."""
        key = (chat_id, topic_id)
        cached = self._streams.get(key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        streams = await self.ctx.chat.get_all_streams(PLATFORM)
        group_id = encode_group_id(chat_id, topic_id)
        found = None
        for stream in streams if isinstance(streams, list) else []:
            if not isinstance(stream, dict) or str(stream.get("account_id") or "") not in ("", str(self.me.id)):
                continue
            if chat_id < 0 and str(stream.get("group_id")) == group_id:
                found = str(stream.get("stream_id") or stream.get("session_id"))
                break
            if chat_id > 0 and not stream.get("is_group_session") and str(stream.get("user_id")) == str(chat_id):
                found = str(stream.get("stream_id") or stream.get("session_id"))
                break
        if found:
            self._streams[key] = (found, time.monotonic() + _STREAM_CACHE_SECONDS)
        return found

    async def append_context(self, chat_id: int, topic_id: int | None, text: str) -> bool:
        """Add a line to MaiBot's context for a chat without triggering a reply (not persisted)."""
        stream_id = await self._stream_id(chat_id, topic_id)
        if stream_id is None:
            self.logger.debug("No MaiBot stream for chat %s; context update dropped", chat_id)
            return False
        result = await self.ctx.maisaka.context.append(
            stream_id, [{"type": "text", "data": text}], visible_text=text, source_kind=f"plugin:{self.ctx.plugin_id}"
        )
        return isinstance(result, dict) and bool(result.get("success"))

    # ---- history polling (R11) ---------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config().dispatch.poll_interval)
            try:
                await self.poll_once()
            except Exception:
                self.logger.exception("History polling failed")

    async def poll_once(self) -> None:
        settings = self.config().dispatch
        recent = await self.store.recent_routed(time.time() - _POLL_WINDOW_SECONDS, settings.poll_count)
        for chat_id, msg_ids in recent.items():
            if self._blocked(chat_id):
                continue
            try:
                messages = await self.backend.get_messages(chat_id, msg_ids)
            except Exception as exc:
                self.logger.debug("Polling chat %s failed: %r", chat_id, exc)
                continue
            for msg_id, message in zip(msg_ids, messages, strict=False):
                if self.edits.pending((chat_id, msg_id)):
                    continue
                meta = await self.store.get_message_meta(chat_id, msg_id)
                if meta is None:
                    continue
                if message is None:
                    await self.store.forget_message(chat_id, msg_id)
                    self.logger.info("History poll: message %s/%s was deleted", chat_id, msg_id)
                    if settings.deletion_notice:
                        await self.append_context(chat_id, meta["topic_id"], f"[消息删除] {self._deletion_text(meta)}")
                    continue
                snapshot = self.codec.snapshot(message)
                if meta["text"] is not None and snapshot != meta["text"]:
                    await self.store.set_snapshot(chat_id, msg_id, snapshot)
                    self.logger.info("History poll: message %s/%s was edited", chat_id, msg_id)
                    if settings.edit_notice:
                        change = f"「{meta['text']}」→「{snapshot}」"
                        await self.append_context(
                            chat_id, meta["topic_id"], f"[消息编辑] {self._author(meta)}编辑了消息: {change}"
                        )
