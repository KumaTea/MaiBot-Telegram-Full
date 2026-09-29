"""Neutral ``Message`` -> MaiBot ``MessageDict``.

Text is passed as markdown (requirement R22). Markers for things MaiBot has no component
for (forwards, stickers, polls, …) are short bracketed Chinese texts, matching the style of
MaiBot's own markers such as ``[图片：…]``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..backend.models import Media, Message, Peer
from ..config import InboundSection
from ..constants import PLATFORM
from ..ids import encode_group_id, encode_message_id
from ..text.entities import Entity
from ..text.md_in import to_markdown
from .filters import command_target

Downloader = Callable[[Message, bool], Awaitable["bytes | None"]]
KnownChecker = Callable[[int, int], Awaitable[bool]]

_SELF_MENTION = "\x00@self\x00"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def _binary_segment(kind: str, data: bytes) -> dict[str, Any]:
    return {
        "type": kind,
        "data": "",
        "hash": hashlib.sha256(data).hexdigest(),
        "binary_data_base64": base64.b64encode(data).decode("ascii"),
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "data": text}


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if limit <= 0 or len(text) <= limit else text[:limit].rstrip() + "…"


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    return f" {seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f" {seconds}秒"


def media_marker(media: Media) -> str:
    kind = media.kind
    if kind == "photo":
        return "[图片]"
    if kind == "sticker":
        return f"[贴纸{media.emoji or ''}]"
    if kind == "animation":
        return "[动图]"
    if kind == "video":
        return f"[视频{_duration(media.duration)}]"
    if kind == "video_note":
        return f"[视频消息{_duration(media.duration)}]"
    if kind == "voice":
        return f"[语音{_duration(media.duration)}]"
    if kind == "audio":
        name = " - ".join(x for x in (media.performer, media.title) if x) or media.file_name or ""
        return f"[音频{': ' + name if name else ''}{_duration(media.duration)}]"
    if kind == "document":
        return f"[文件: {media.file_name or '未命名'}]"
    if kind == "poll":
        return f"[投票: {media.title}（选项: {media.details}）]"
    if kind == "geo":
        return f"[位置: {media.details}]"
    if kind == "venue":
        return f"[地点: {media.title}，{media.details}]"
    if kind == "contact":
        return f"[联系人: {media.title} {media.details}]"
    if kind == "dice":
        return f"[骰子 {media.emoji} 点数 {media.details}]"
    if kind == "game":
        return f"[游戏: {media.title}]"
    if kind == "invoice":
        return f"[账单: {media.title}]"
    return f"[暂不支持的消息: {media.details or kind}]"


def sender_identity(message: Message) -> tuple[str, str]:
    """``(user_id, nickname)`` of the author, attributing channel and anonymous posts properly."""
    chat, sender = message.chat, message.sender
    if sender is None or sender.id == chat.id:
        # Anonymous group admin (posting as the group) or a post without a sender.
        suffix = f": {message.post_author}" if message.post_author else ""
        return str(chat.id), f"{chat.name}（匿名管理员{suffix}）"
    if sender.is_channel or sender.kind == "supergroup":
        # Linked channel auto-posts and users posting "as a channel" keep the channel identity.
        return str(sender.id), f"{sender.name}（频道）"
    return str(sender.id), sender.name


def display_name(peer: Peer | None, fallback: str = "未知用户") -> str:
    return peer.name if peer is not None else fallback


class InboundCodec:
    def __init__(
        self,
        *,
        me: Peer,
        settings: Callable[[], InboundSection],
        download: Downloader,
        is_known: KnownChecker,
        logger: logging.Logger,
    ) -> None:
        self.me = me
        self.settings = settings
        self.download = download
        self.is_known = is_known
        self.logger = logger

    # ---- helpers -----------------------------------------------------------------------

    def _mention_hook(self, entity: Entity, inner: str) -> str | None:
        username = (self.me.username or "").lower()
        if entity.type == "mention" and username and inner.strip().lower() == f"@{username}":
            return _SELF_MENTION
        if entity.type == "mention_name" and entity.user_id == self.me.id:
            return _SELF_MENTION
        return None

    def mentions_me(self, message: Message) -> bool:
        if message.mentioned:
            return True
        reply = message.reply_message
        if reply is not None and reply.sender is not None and reply.sender.id == self.me.id:
            return True
        username = (self.me.username or "").lower()
        is_command, addressed = command_target(message.text)
        if is_command and username and (addressed or "").lower() == username:
            return True
        return any(e.type == "mention_name" and e.user_id == self.me.id for e in message.entities)

    def _text_segments(self, message: Message) -> list[dict[str, Any]]:
        markdown = to_markdown(message.text, message.entities, self._mention_hook)
        segments: list[dict[str, Any]] = []
        for index, chunk in enumerate(markdown.split(_SELF_MENTION)):
            if index:
                segments.append({"type": "at", "data": {"target_user_id": str(self.me.id)}})
                chunk = chunk.lstrip()
            if chunk.strip():
                segments.append(_text(chunk))
        return segments

    def _preview(self, message: Message) -> str:
        parts = []
        if message.media is not None:
            parts.append(media_marker(message.media))
        if message.text:
            parts.append(to_markdown(message.text, message.entities))
        return _truncate(" ".join(parts), self.settings().reply_preview_length)

    async def _reply_segments(self, message: Message) -> list[dict[str, Any]]:
        if message.reply_to_id is None:
            return []
        segments: list[dict[str, Any]] = []
        replied = message.reply_message
        if message.reply_to_chat_id in (None, message.chat.id):
            data: dict[str, Any] = {"target_message_id": encode_message_id(message.chat.id, message.reply_to_id)}
            if replied is not None:
                author_id, author_name = sender_identity(replied)
                data["target_message_sender_id"] = author_id
                data["target_message_sender_nickname"] = author_name
            segments.append({"type": "reply", "data": data})

        author = sender_identity(replied)[1] if replied is not None else "某条消息"
        if message.reply_quote:
            # A partial quote carries information the original message alone does not.
            segments.append(_text(f"[引用 {author}: 「{_truncate(message.reply_quote, 200)}」]\n"))
        elif replied is not None and not await self.is_known(message.chat.id, message.reply_to_id):
            # MaiBot only shows reply targets by id; add a preview when it has likely never seen it.
            segments.append(_text(f"[回复 {author}: {self._preview(replied)}]\n"))
        return segments

    def _forward_segments(self, message: Message) -> list[dict[str, Any]]:
        forward = message.forward
        if forward is None:
            return []
        sender = message.sender
        if forward.sender is not None and sender is not None and forward.sender.id == sender.id and sender.is_channel:
            return []  # a linked channel's automatic forward into its discussion group
        name = forward.sender.name if forward.sender is not None else forward.sender_name or "隐藏用户"
        if forward.post_author:
            name = f"{name}（{forward.post_author}）"
        return [_text(f"[转发自 {name}]\n")]

    async def _download(self, message: Message, thumb: bool) -> bytes | None:
        media = message.media
        if media is not None and not thumb and media.size and media.size > MAX_DOWNLOAD_BYTES:
            return None
        try:
            return await self.download(message, thumb)
        except Exception as exc:
            self.logger.warning("Download failed for %s/%s: %s", message.chat.id, message.id, exc)
            return None

    async def _media_segments(self, message: Message) -> list[dict[str, Any]]:
        media = message.media
        if media is None:
            return []
        if media.kind == "photo":
            data = await self._download(message, thumb=False)
            return [_binary_segment("image", data)] if data else [_text(media_marker(media))]
        if media.kind == "sticker":
            static = media.sticker_format == "static"
            data = await self._download(message, thumb=not static) if static or media.has_thumb else None
            if data:
                return [_binary_segment("emoji", data)]
            return [_text(media_marker(media))]
        if media.kind == "animation":
            data = await self._download(message, thumb=True) if media.has_thumb else None
            return [_binary_segment("emoji", data)] if data else [_text(media_marker(media))]
        if media.kind == "voice":
            data = await self._download(message, thumb=False)
            return [_binary_segment("voice", data)] if data else [_text(media_marker(media))]
        if media.kind == "document":
            payload = {"name": media.file_name or "", "size": media.size or "", "mime_type": media.mime_type or ""}
            return [{"type": "file", "data": {k: v for k, v in payload.items() if v}}]
        return [_text(media_marker(media))]

    def _webpage_segments(self, message: Message) -> list[dict[str, Any]]:
        page = message.webpage
        if page is None or not (page.title or page.description):
            return []
        head = " | ".join(x for x in (page.site_name, page.title) if x)
        body = _truncate(page.description or "", 300)
        return [_text(f"\n[链接预览: {head}{' — ' + body if body else ''}]")]

    # ---- public ------------------------------------------------------------------------

    async def build(self, message: Message) -> dict[str, Any] | None:
        segments: list[dict[str, Any]] = []
        segments += await self._reply_segments(message)
        segments += self._forward_segments(message)
        segments += self._text_segments(message)
        segments += await self._media_segments(message)
        segments += self._webpage_segments(message)
        if not any(seg["type"] != "reply" for seg in segments):
            return None

        chat = message.chat
        user_id, nickname = sender_identity(message)
        is_at = self.mentions_me(message) or any(seg["type"] == "at" for seg in segments)
        additional: dict[str, Any] = {
            "telegram_chat_id": chat.id,
            "telegram_message_id": message.id,
        }
        if message.topic_id is not None:
            additional["telegram_topic_id"] = message.topic_id
        if message.sender is not None:
            if message.sender.username:
                additional["telegram_sender_username"] = message.sender.username
            additional["telegram_sender_is_bot"] = message.sender.is_bot
        if is_at:
            additional["at_bot"] = True

        message_info: dict[str, Any] = {
            "user_info": {"user_id": user_id, "user_nickname": nickname, "user_cardname": None},
            "additional_config": additional,
        }
        if chat.is_private:
            additional["platform_io_target_user_id"] = str(chat.id)
        else:
            group_id = encode_group_id(chat.id, message.topic_id)
            additional["platform_io_target_group_id"] = group_id
            message_info["group_info"] = {"group_id": group_id, "group_name": chat.name}

        plain_text = "".join(seg["data"] for seg in segments if seg["type"] == "text")
        result: dict[str, Any] = {
            "message_id": encode_message_id(chat.id, message.id),
            "timestamp": str(message.date.timestamp()),
            "platform": PLATFORM,
            "message_info": message_info,
            "raw_message": segments,
            "is_mentioned": is_at,
            "is_at": is_at,
            "is_emoji": any(seg["type"] == "emoji" for seg in segments),
            "is_picture": any(seg["type"] == "image" for seg in segments),
            "is_command": command_target(message.text)[0],
            "is_notify": False,
            "processed_plain_text": plain_text,
        }
        if message.reply_to_id is not None and message.reply_to_chat_id in (None, chat.id):
            result["reply_to"] = encode_message_id(chat.id, message.reply_to_id)
        return result
