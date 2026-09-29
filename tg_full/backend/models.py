"""Plain data models describing Telegram objects without exposing Telethon types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..text.entities import Entity

PeerKind = Literal["user", "bot", "group", "supergroup", "channel"]


@dataclass(frozen=True)
class Peer:
    id: int  # Bot-API style marked id
    kind: PeerKind
    name: str
    username: str | None = None
    is_forum: bool = False

    @property
    def is_bot(self) -> bool:
        return self.kind == "bot"

    @property
    def is_private(self) -> bool:
        return self.kind in ("user", "bot")

    @property
    def is_channel(self) -> bool:
        return self.kind == "channel"


@dataclass(frozen=True)
class Forward:
    """Where a forwarded message originally came from."""

    sender: Peer | None = None      # original author, when not hidden
    sender_name: str | None = None  # name of an author who hides their account
    post_author: str | None = None  # signature of a channel post
    date: datetime | None = None


@dataclass(frozen=True)
class WebPage:
    url: str
    site_name: str | None = None
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Media:
    kind: str  # photo, sticker, animation, video, video_note, voice, audio, document, poll, geo, contact, dice, …
    file_key: str | None = None  # stable per file across chats: "<kind>:<telegram id>"
    mime_type: str | None = None
    file_name: str | None = None
    size: int | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    emoji: str | None = None  # sticker emoji / dice emoticon
    title: str | None = None  # audio title, poll question, venue title
    performer: str | None = None
    sticker_format: Literal["static", "animated", "video"] | None = None
    details: str | None = None  # short human readable extras (poll options, coordinates, …)
    has_thumb: bool = False


@dataclass
class Message:
    chat: Peer
    id: int
    date: datetime
    sender: Peer | None
    outgoing: bool
    text: str
    entities: list[Entity] = field(default_factory=list)
    reply_to_id: int | None = None
    reply_to_chat_id: int | None = None  # set when replying to a message in another chat
    reply_quote: str | None = None       # the quoted fragment of a partial-quote reply
    reply_message: Message | None = None
    forward: Forward | None = None
    media: Media | None = None
    webpage: WebPage | None = None
    topic_id: int | None = None
    edit_date: datetime | None = None
    grouped_id: int | None = None
    mentioned: bool = False
    post_author: str | None = None
    raw: Any = field(default=None, repr=False, compare=False)
