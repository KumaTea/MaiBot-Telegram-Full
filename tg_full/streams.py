"""MaiBot chat streams (sessions) <-> Telegram chats of this account.

Tools and hooks receive MaiBot's stream / session id; context updates need it the other way
round. Both come from ``chat.get_all_streams``, filtered to this adapter's account (several
Telegram adapters can share the ``telegram`` platform).
"""

from __future__ import annotations

import time
from typing import Any

from .backend.models import Peer
from .constants import PLATFORM
from .ids import ChatTarget, decode_group_id

_TTL_SECONDS = 600
_MIN_REFRESH_SECONDS = 5


class StreamResolver:
    def __init__(self, ctx: Any, me: Peer) -> None:
        self.ctx = ctx
        self.me = me
        self._by_stream: dict[str, ChatTarget] = {}
        self._by_chat: dict[tuple[int, int | None], str] = {}
        self._loaded_at = 0.0

    async def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        age = now - self._loaded_at
        if age < _MIN_REFRESH_SECONDS or (not force and age < _TTL_SECONDS):
            return
        self._loaded_at = now
        streams = await self.ctx.chat.get_all_streams(PLATFORM)
        by_stream: dict[str, ChatTarget] = {}
        by_chat: dict[tuple[int, int | None], str] = {}
        for stream in streams if isinstance(streams, list) else []:
            if not isinstance(stream, dict) or str(stream.get("account_id") or "") not in ("", str(self.me.id)):
                continue
            stream_id = str(stream.get("stream_id") or stream.get("session_id") or "")
            try:
                if stream.get("is_group_session"):
                    target = decode_group_id(str(stream.get("group_id")))
                else:
                    target = ChatTarget(int(str(stream.get("user_id"))))
            except (TypeError, ValueError):
                continue
            if stream_id:
                by_stream[stream_id] = target
                by_chat[(target.chat_id, target.topic_id)] = stream_id
        self._by_stream, self._by_chat = by_stream, by_chat

    async def chat_for_stream(self, stream_id: str) -> ChatTarget | None:
        await self._refresh()
        if stream_id not in self._by_stream:
            await self._refresh(force=True)
        return self._by_stream.get(stream_id)

    async def stream_for_chat(self, chat_id: int, topic_id: int | None) -> str | None:
        await self._refresh()
        key = (chat_id, topic_id)
        if key not in self._by_chat:
            await self._refresh(force=True)
        return self._by_chat.get(key)
