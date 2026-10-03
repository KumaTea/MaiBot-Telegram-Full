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
    has_photo: bool = False
    raw: Any = field(default=None, repr=False, compare=False)  # the TL page, to download its photo


@dataclass(frozen=True)
class MediaRef:
    """Enough to resend a Telegram file without uploading it again (``file_reference`` expires)."""

    type: Literal["photo", "document"]
    id: int
    access_hash: int
    file_reference: bytes


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
    has_thumb: bool = False  # a real thumbnail (not the tiny inline preview) is available
    ref: MediaRef | None = None
    sticker_set: tuple[int, int] | None = None  # (id, access_hash)


@dataclass(frozen=True)
class ReactionChange:
    """Someone added a reaction to a message."""

    chat_id: int
    msg_id: int
    emoji: str  # the emoji, "[自定义表情]" for custom emoji, "⭐" for paid reactions
    actor: Peer | None = None  # None when Telegram only reports counts
    big: bool = False  # sent with the long-press "big" animation (user accounts can see this)


@dataclass(frozen=True)
class CallbackPress:
    """A user pressed an inline keyboard button of one of our messages (bot accounts only)."""

    query_id: int
    chat: Peer
    msg_id: int
    data: str
    sender: Peer | None
    label: str | None = None  # button text, when it could be read from the message


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
    webpage: WebPage | None = None  # full preview: user accounts only, bots get an empty page
    link_preview: bool = False  # shown with a link preview (the sender left it on)
    link_preview_url: str | None = None  # the link it previews, when Telegram says
    topic_id: int | None = None
    edit_date: datetime | None = None
    grouped_id: int | None = None
    mentioned: bool = False
    post_author: str | None = None
    raw: Any = field(default=None, repr=False, compare=False)
