"""Identifier encoding between Telegram and MaiBot.

Chat ids are Bot-API style "marked" ids: users are positive, basic groups ``-id`` and
channels / supergroups ``-100id``.

* Group ids keep the original Telegram adapter's forum-topic scheme
  (``<chat_id>::tg-topic::mt=<topic_id>``) so existing MaiBot sessions carry over.
* Message ids are ``<chat_id>:<msg_id>``. MaiBot resolves reply targets by message id across
  all chats, and Telegram message ids are only unique per chat.
"""

from __future__ import annotations

from dataclasses import dataclass

_TOPIC_SPLITTER = "::tg-topic::"


@dataclass(frozen=True)
class ChatTarget:
    chat_id: int
    topic_id: int | None = None


def encode_group_id(chat_id: int, topic_id: int | None = None) -> str:
    if topic_id is None:
        return str(chat_id)
    return f"{chat_id}{_TOPIC_SPLITTER}mt={topic_id}"


def decode_group_id(group_id: str) -> ChatTarget:
    raw = str(group_id).strip()
    if _TOPIC_SPLITTER not in raw:
        return ChatTarget(int(raw))
    base, payload = raw.split(_TOPIC_SPLITTER, 1)
    topic_id = None
    for part in payload.split("&"):
        key, _, value = part.partition("=")
        # "dm" (direct-messages topics) from the original adapter has no MTProto equivalent here.
        if key == "mt" and value.lstrip("-").isdigit():
            topic_id = int(value)
    return ChatTarget(int(base), topic_id)


def encode_message_id(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def decode_message_id(value: str, default_chat_id: int | None = None) -> tuple[int, int] | None:
    """Return ``(chat_id, msg_id)``; bare ids fall back to ``default_chat_id``."""
    raw = str(value or "").strip()
    if not raw:
        return None
    chat_part, sep, msg_part = raw.rpartition(":")
    try:
        if sep:
            return int(chat_part), int(msg_part)
        if default_chat_id is not None:
            return default_chat_id, int(raw)
    except ValueError:
        return None
    return None
