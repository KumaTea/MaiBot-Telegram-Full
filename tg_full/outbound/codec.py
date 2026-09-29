"""MaiBot outbound ``MessageDict`` -> a list of Telegram sends.

Consecutive text and @mention segments are merged into one text message; each media
segment becomes its own message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..config import OutboundSection
from ..ids import ChatTarget, decode_group_id, decode_message_id
from ..media.images import detect_format, to_sticker_webp
from ..text.latex import latex_to_plain
from ..text.length import check_length
from ..text.md_out import FormattedText, render

MAX_PHOTO_BYTES = 10 * 1024 * 1024


class OutboundError(ValueError):
    pass


@dataclass
class OutItem:
    kind: str  # text, photo, sticker, animation, voice, document
    text: FormattedText | None = None
    data: bytes | None = None
    file_name: str | None = None
    mime_type: str | None = None
    width: int = 512
    height: int = 512
    source_hash: str | None = None  # MaiBot's hash of the original bytes, for native resending


@dataclass
class SendPlan:
    target: ChatTarget
    reply_to: int | None
    items: list[OutItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_target(message: dict[str, Any]) -> ChatTarget:
    info = message.get("message_info") or {}
    additional = info.get("additional_config") or {}
    group_id = additional.get("platform_io_target_group_id") or (info.get("group_info") or {}).get("group_id")
    user_id = additional.get("platform_io_target_user_id")
    try:
        if group_id:
            return decode_group_id(str(group_id))
        if user_id:
            return ChatTarget(int(str(user_id)))
    except ValueError as exc:
        raise OutboundError(f"Invalid Telegram target id: {exc}") from exc
    raise OutboundError("Cannot determine the target Telegram chat")


def _reply_target(message: dict[str, Any], target: ChatTarget) -> int | None:
    candidates: list[Any] = []
    for segment in message.get("raw_message") or []:
        if isinstance(segment, dict) and segment.get("type") == "reply":
            data = segment.get("data")
            candidates.append(data.get("target_message_id") if isinstance(data, dict) else data)
    additional = (message.get("message_info") or {}).get("additional_config") or {}
    candidates.append(additional.get("reply_message_id"))
    for candidate in candidates:
        decoded = decode_message_id(str(candidate or ""), default_chat_id=target.chat_id)
        if decoded is not None and decoded[0] == target.chat_id:
            return decoded[1]
    return None


def _decode_binary(segment: dict[str, Any]) -> bytes | None:
    raw = segment.get("binary_data_base64")
    if not raw:
        data = segment.get("data")
        raw = data.get("base64") if isinstance(data, dict) else None
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except (binascii.Error, ValueError):
        return None


def _source_hash(segment: dict[str, Any], payload: bytes) -> str:
    return str(segment.get("hash") or "") or hashlib.sha256(payload).hexdigest()


def _mention_markdown(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    user_id = str(data.get("target_user_id") or "").strip()
    name = str(data.get("target_user_cardname") or data.get("target_user_nickname") or user_id).strip()
    name = name.replace("[", "\\[").replace("]", "\\]")
    if user_id.isdigit():  # users only; channels (negative ids) cannot be mentioned
        return f"[@{name}](tg://user?id={user_id})"
    return f"@{name}"


class _PlanBuilder:
    def __init__(self, plan: SendPlan, settings: OutboundSection) -> None:
        self.plan = plan
        self.settings = settings
        self.buffer: list[str] = []

    def flush_text(self) -> None:
        markdown = "".join(self.buffer)
        self.buffer.clear()
        if not markdown.strip():
            return
        formatted = render(latex_to_plain(markdown), keep_entities=self.settings.text_format == "markdown")
        if not formatted.text:
            return
        warning = check_length(formatted.text, self.settings.soft_length_warning)
        if warning:
            self.plan.warnings.append(warning)
        self.plan.items.append(OutItem("text", text=formatted))

    def add_media(self, item: OutItem) -> None:
        self.flush_text()
        self.plan.items.append(item)

    def segment(self, segment: dict[str, Any]) -> None:
        kind = str(segment.get("type") or "")
        data = segment.get("data")
        if kind == "text":
            self.buffer.append(str(data or ""))
        elif kind == "at":
            self.buffer.append(_mention_markdown(data))
        elif kind == "image":
            self.image(segment)
        elif kind == "emoji":
            self.emoji(segment)
        elif kind == "voice":
            payload = _decode_binary(segment)
            if payload:
                self.add_media(OutItem("voice", data=payload))
        elif kind == "file":
            self.file(data if isinstance(data, dict) else {}, segment)
        elif kind == "forward":
            self.forward(data)
        # reply is handled separately; dict / custom segments are not sendable.

    def image(self, segment: dict[str, Any]) -> None:
        payload = _decode_binary(segment)
        data = segment.get("data")
        if payload:
            fmt = detect_format(payload)
            if fmt == "gif":
                kind = "animation"
            elif len(payload) > MAX_PHOTO_BYTES:
                kind = "document"  # Telegram rejects photos over 10 MB
            else:
                kind = "photo"
            self.add_media(OutItem(kind, data=payload, file_name=f"image.{fmt if fmt != 'unknown' else 'jpg'}",
                                   source_hash=_source_hash(segment, payload)))
        elif isinstance(data, str) and data.startswith(("http://", "https://")):
            self.buffer.append(f"\n{data}\n")

    def emoji(self, segment: dict[str, Any]) -> None:
        payload = _decode_binary(segment)
        if not payload:
            return
        fmt = detect_format(payload)
        source_hash = _source_hash(segment, payload)
        if fmt == "gif":
            self.add_media(OutItem("animation", data=payload, source_hash=source_hash))
            return
        if fmt in ("png", "webp"):
            sticker = to_sticker_webp(payload)
            if sticker is not None:
                self.add_media(OutItem("sticker", data=sticker[0], width=sticker[1], height=sticker[2],
                                       source_hash=source_hash))
                return
        self.add_media(OutItem("photo", data=payload, file_name=f"emoji.{fmt if fmt != 'unknown' else 'jpg'}",
                               source_hash=source_hash))

    def file(self, data: dict[str, Any], segment: dict[str, Any]) -> None:
        payload = _decode_binary(segment)
        name = str(data.get("name") or data.get("file_name") or "file")
        if payload:
            self.add_media(OutItem("document", data=payload, file_name=name, mime_type=data.get("mime_type") or None))
        elif data.get("url"):
            self.buffer.append(f"\n[{name}]({data['url']})\n")

    def forward(self, nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            texts = [str(c.get("data") or "") for c in node.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                self.buffer.append(f"**{node.get('user_nickname') or '未知用户'}**: {''.join(texts)}\n")


def build_plan(message: dict[str, Any], settings: OutboundSection) -> SendPlan:
    """Raises ``OutboundError`` or ``TextTooLongError`` (a ``ValueError``) with an LLM-readable reason."""
    target = resolve_target(message)
    plan = SendPlan(target=target, reply_to=_reply_target(message, target))
    builder = _PlanBuilder(plan, settings)
    for segment in message.get("raw_message") or []:
        if isinstance(segment, dict):
            builder.segment(segment)
    builder.flush_text()
    if not plan.items:
        raise OutboundError("The message has no content that can be sent to Telegram")
    return plan
