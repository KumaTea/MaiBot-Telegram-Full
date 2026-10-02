"""Neutral ``Message`` -> MaiBot ``MessageDict``.

Text is passed as markdown (requirement R22). Markers for things MaiBot has no component
for (forwards, stickers, polls, …) are short bracketed Chinese texts, matching the style of
MaiBot's own markers such as ``[图片：…]``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..backend.models import Media, Message, Peer
from ..config import InboundSection, MediaSection
from ..constants import PLATFORM
from ..ids import encode_group_id, encode_message_id
from ..media import animation
from ..media.cache import MediaCache
from ..media.link_preview import LinkInfo, LinkPreviewer
from ..text.entities import Entity, units_to_str, utf16_units
from ..text.md_in import to_markdown
from .dispatcher import Notice
from .filters import command_target

Downloader = Callable[[Message, bool], Awaitable["bytes | None"]]
KnownChecker = Callable[[int, int], Awaitable[bool]]

_SELF_MENTION = "\x00@self\x00"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_GIF_SOURCE_BYTES = 10 * 1024 * 1024
MAX_LINKS_PER_MESSAGE = 2


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


def _message_info(
    chat: Peer, topic_id: int | None, user_id: str, nickname: str, additional: dict[str, Any]
) -> dict[str, Any]:
    """``message_info`` with the routing fields MaiBot needs for this chat."""
    message_info: dict[str, Any] = {
        "user_info": {"user_id": user_id, "user_nickname": nickname, "user_cardname": None},
        "additional_config": additional,
    }
    if chat.is_private:
        additional["platform_io_target_user_id"] = str(chat.id)
    else:
        group_id = encode_group_id(chat.id, topic_id)
        additional["platform_io_target_group_id"] = group_id
        message_info["group_info"] = {"group_id": group_id, "group_name": chat.name}
    return message_info


def display_name(peer: Peer | None, fallback: str = "未知用户") -> str:
    return peer.name if peer is not None else fallback


def urls_in(message: Message) -> list[str]:
    """Links in the message text, in order, without duplicates."""
    units = utf16_units(message.text)
    urls: list[str] = []
    for entity in message.entities:
        if entity.type == "url":
            url = units_to_str(units, entity.offset, entity.end)
            if "://" not in url:
                url = "http://" + url  # Telegram also detects scheme-less links such as example.com
        elif entity.type == "text_url" and entity.url:
            url = entity.url
        else:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def link_marker(info: LinkInfo) -> str:
    head = " | ".join(x for x in (info.site_name, info.title) if x)
    body = _truncate(info.description or "", 300)
    return f"\n[链接预览: {head}{' — ' + body if body else ''}]"


class InboundCodec:
    def __init__(
        self,
        *,
        me: Peer,
        settings: Callable[[], InboundSection],
        media_settings: Callable[[], MediaSection] = MediaSection,
        download: Downloader,
        is_known: KnownChecker,
        cache: MediaCache | None = None,
        links: LinkPreviewer | None = None,
        logger: logging.Logger,
    ) -> None:
        self.me = me
        self.settings = settings
        self.media_settings = media_settings
        self.download = download
        self.is_known = is_known
        self.cache = cache
        self.links = links
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

    async def _visual(self, message: Message, segment_type: str, variant: str) -> dict[str, Any] | None:
        """An image/emoji segment: MaiBot's cached description when available, else fresh bytes.

        ``variant`` is what MaiBot gets: the ``full`` file, its ``thumb``nail, or a converted ``gif``.
        """
        media = message.media
        assert media is not None
        settings = self.media_settings()
        if self.cache is not None and settings.recognition_cache:
            cached = await self.cache.cached_segment(media, variant, segment_type)
            if cached is not None:
                return cached
        if variant == "gif":
            source = None if (media.size or 0) > MAX_GIF_SOURCE_BYTES else await self._download(message, thumb=False)
            data = await self._to_gif(source) if source else None
            if data is None and media.has_thumb:
                variant = "thumb"  # conversion failed: fall back to the thumbnail
                data = await self._download(message, thumb=True)
        else:
            data = await self._download(message, thumb=variant == "thumb")
        if not data:
            return None
        segment = _binary_segment(segment_type, data)
        if self.cache is not None:
            await self.cache.remember(media, variant, segment["hash"], message.chat.id, message.id)
        return segment

    async def _to_gif(self, data: bytes) -> bytes | None:
        try:
            return await asyncio.to_thread(animation.video_to_gif, data)
        except Exception as exc:
            self.logger.debug("GIF conversion failed: %r", exc)
            return None

    def _animation_variant(self, media: Media) -> str | None:
        """How to show an animation: ``gif``, ``thumb``, or ``None`` for just a marker."""
        settings = self.media_settings()
        if settings.animation == "drop":
            return None
        if settings.animation == "gif" and animation.pyav_available():
            return "gif"
        return "thumb" if media.has_thumb else None

    async def _media_segments(self, message: Message) -> list[dict[str, Any]]:
        media = message.media
        if media is None:
            return []
        marker = [_text(media_marker(media))]
        kind = media.kind
        if kind == "photo":
            segment = await self._visual(message, "image", "full")
            return [segment] if segment else marker
        if kind == "sticker":
            hint = [_text(f"[贴纸 {media.emoji}]")] if media.emoji and self.media_settings().sticker_emoji_hint else []
            if media.sticker_format == "static":
                variant: str | None = "full"
            elif media.sticker_format == "video":
                variant = self._animation_variant(media)
            else:  # Lottie (.tgs) animations cannot be rendered without extra libraries
                variant = "thumb" if media.has_thumb else None
            segment = await self._visual(message, "emoji", variant) if variant else None
            if segment is None:
                return marker
            return [*hint, segment]
        if kind == "animation":
            variant = self._animation_variant(media)
            segment = await self._visual(message, "emoji", variant) if variant else None
            return [segment] if segment else marker
        if kind in ("video", "video_note"):
            if self.media_settings().video_thumbnail and media.has_thumb:
                segment = await self._visual(message, "image", "thumb")
                if segment is not None:
                    return [*marker, segment]
            return marker
        if kind == "voice":
            data = await self._download(message, thumb=False)
            return [_binary_segment("voice", data)] if data else marker
        if kind == "document":
            payload = {"name": media.file_name or "", "size": media.size or "", "mime_type": media.mime_type or ""}
            return [{"type": "file", "data": {k: v for k, v in payload.items() if v}}]
        return marker

    async def _link_segments(self, message: Message) -> list[dict[str, Any]]:
        mode = self.media_settings().link_preview
        if mode == "off":
            return []
        page = message.webpage
        if page is not None and (page.title or page.description):
            return [_text(link_marker(LinkInfo(page.url, page.title, page.description, page.site_name)))]
        if self.links is None:
            return []
        segments = []
        for url in urls_in(message)[:MAX_LINKS_PER_MESSAGE]:
            info = await self.links.describe(url, fetch=mode == "fetch")
            if info is not None:
                segments.append(_text(link_marker(info)))
        return segments

    # ---- public ------------------------------------------------------------------------

    def snapshot(self, message: Message, limit: int = 500) -> str:
        """Short text form of a message, stored to describe later edits and deletions."""
        parts = []
        if message.media is not None:
            parts.append(media_marker(message.media))
        if message.text:
            parts.append(to_markdown(message.text, message.entities))
        return _truncate(" ".join(parts), limit)

    def build_notice(self, notice: Notice) -> dict[str, Any]:
        """A notice message in the style of MaiBot's NapCat adapter (``is_notify``)."""
        additional: dict[str, Any] = {"telegram_chat_id": notice.chat.id, "telegram_notice": notice.key}
        if notice.topic_id is not None:
            additional["telegram_topic_id"] = notice.topic_id
        return {
            "message_id": f"tg-notice-{notice.key}",
            "timestamp": str(time.time()),
            "platform": PLATFORM,
            "message_info": _message_info(notice.chat, notice.topic_id, notice.actor_id, notice.actor_name, additional),
            "raw_message": [_text(notice.text)],
            "is_mentioned": False,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "is_command": False,
            "is_notify": notice.is_notify,
            "processed_plain_text": notice.text,
        }

    async def build(self, message: Message) -> dict[str, Any] | None:
        segments: list[dict[str, Any]] = []
        segments += await self._reply_segments(message)
        segments += self._forward_segments(message)
        segments += self._text_segments(message)
        segments += await self._media_segments(message)
        segments += await self._link_segments(message)
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

        message_info = _message_info(chat, message.topic_id, user_id, nickname, additional)

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
