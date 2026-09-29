"""Telethon objects -> backend-neutral models."""

from __future__ import annotations

import logging
from typing import Any

from telethon import TelegramClient, utils
from telethon.tl import types

from ..text.entities import Entity
from .models import Forward, Media, Message, Peer, WebPage

logger = logging.getLogger(__name__)

_ENTITY_TYPES: dict[type, str] = {
    types.MessageEntityBold: "bold",
    types.MessageEntityItalic: "italic",
    types.MessageEntityUnderline: "underline",
    types.MessageEntityStrike: "strike",
    types.MessageEntitySpoiler: "spoiler",
    types.MessageEntityCode: "code",
    types.MessageEntityPre: "pre",
    types.MessageEntityTextUrl: "text_url",
    types.MessageEntityMentionName: "mention_name",
    types.InputMessageEntityMentionName: "mention_name",
    types.MessageEntityBlockquote: "blockquote",
    types.MessageEntityCustomEmoji: "custom_emoji",
    types.MessageEntityMention: "mention",
    types.MessageEntityUrl: "url",
    types.MessageEntityEmail: "email",
    types.MessageEntityHashtag: "hashtag",
    types.MessageEntityCashtag: "cashtag",
    types.MessageEntityBotCommand: "bot_command",
    types.MessageEntityPhone: "phone",
    types.MessageEntityBankCard: "bank_card",
}


def entity_from_tl(tl: Any) -> Entity:
    kind = _ENTITY_TYPES.get(type(tl), "unknown")
    user_id = getattr(tl, "user_id", None)
    if not isinstance(user_id, int):  # InputMessageEntityMentionName carries an InputUser
        user_id = getattr(user_id, "user_id", None)
    return Entity(
        type=kind,
        offset=tl.offset,
        length=tl.length,
        url=getattr(tl, "url", None),
        user_id=user_id,
        language=getattr(tl, "language", None) or None,
        custom_emoji_id=getattr(tl, "document_id", None),
        collapsed=bool(getattr(tl, "collapsed", False)),
    )


def peer_from_entity(entity: Any) -> Peer | None:
    if entity is None:
        return None
    peer_id = utils.get_peer_id(entity)
    if isinstance(entity, types.User):
        if entity.deleted:
            name = "Deleted Account"
        else:
            name = utils.get_display_name(entity) or (entity.username or "") or str(entity.id)
        return Peer(peer_id, "bot" if entity.bot else "user", name, entity.username)
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return Peer(peer_id, "group", entity.title or str(peer_id))
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        broadcast = bool(getattr(entity, "broadcast", False))
        return Peer(
            peer_id,
            "channel" if broadcast else "supergroup",
            entity.title or str(peer_id),
            getattr(entity, "username", None),
            is_forum=bool(getattr(entity, "forum", False)),
        )
    return None


def _file_key(kind: str, obj: Any) -> str | None:
    media_id = getattr(obj, "id", None)
    return f"{kind}:{media_id}" if media_id is not None else None


def media_from_message(msg: Any) -> Media | None:
    """Describe the message's media. Web page previews are handled separately."""
    file = msg.file
    common: dict[str, Any] = {}
    if file is not None:
        common = {
            "mime_type": file.mime_type,
            "file_name": file.name,
            "size": file.size,
            "duration": file.duration,
            "width": file.width,
            "height": file.height,
        }
    if msg.photo is not None:
        return Media("photo", _file_key("photo", msg.photo), **common, has_thumb=True)
    if msg.sticker is not None:
        mime = file.mime_type if file else ""
        fmt = "animated" if mime == "application/x-tgsticker" else "video" if mime == "video/webm" else "static"
        return Media("sticker", _file_key("document", msg.document), **common, emoji=file.emoji if file else None,
                     sticker_format=fmt, has_thumb=bool(msg.document.thumbs))
    if msg.document is not None:
        document = msg.document
        if msg.gif is not None:
            kind = "animation"
        elif msg.video_note is not None:
            kind = "video_note"
        elif msg.video is not None:
            kind = "video"
        elif msg.voice is not None:
            kind = "voice"
        elif msg.audio is not None:
            kind = "audio"
        else:
            kind = "document"
        return Media(kind, _file_key("document", document), **common, title=file.title if file else None,
                     performer=file.performer if file else None, has_thumb=bool(document.thumbs))
    if msg.poll is not None:
        poll = msg.poll.poll
        question = getattr(poll.question, "text", poll.question)
        options = [getattr(answer.text, "text", answer.text) for answer in poll.answers]
        return Media("poll", title=str(question), details=" / ".join(str(o) for o in options))
    if msg.venue is not None:
        return Media("venue", title=msg.venue.title, details=msg.venue.address)
    if msg.geo is not None and isinstance(msg.geo, types.GeoPoint):
        return Media("geo", details=f"{msg.geo.lat:.5f}, {msg.geo.long:.5f}")
    if msg.contact is not None:
        contact = msg.contact
        name = f"{contact.first_name} {contact.last_name}".strip()
        return Media("contact", title=name, details=contact.phone_number)
    if msg.dice is not None:
        return Media("dice", emoji=msg.dice.emoticon, details=str(msg.dice.value))
    if msg.game is not None:
        return Media("game", title=msg.game.title)
    if msg.invoice is not None:
        return Media("invoice", title=msg.invoice.title)
    if msg.media is not None and not isinstance(msg.media, (types.MessageMediaWebPage, types.MessageMediaEmpty)):
        return Media("unsupported", details=type(msg.media).__name__)
    return None


def webpage_from_message(msg: Any) -> WebPage | None:
    page = msg.web_preview
    if not isinstance(page, types.WebPage):
        return None
    return WebPage(url=page.url, site_name=page.site_name, title=page.title, description=page.description)


async def _get_entity_for_peer(client: TelegramClient, peer: Any) -> Any:
    try:
        return await client.get_entity(peer)
    except Exception as exc:  # unknown / inaccessible peers are common for forwards
        logger.debug("Cannot resolve peer %s: %s", peer, exc)
        return None


async def forward_from_message(client: TelegramClient, msg: Any) -> Forward | None:
    header = msg.fwd_from
    if header is None:
        return None
    sender = None
    if header.from_id is not None:
        entity = (msg.forward.sender or msg.forward.chat) if msg.forward else None
        if entity is None:
            entity = await _get_entity_for_peer(client, header.from_id)
        sender = peer_from_entity(entity)
    return Forward(sender=sender, sender_name=header.from_name, post_author=header.post_author, date=header.date)


def topic_of(msg: Any, chat: Peer) -> tuple[int | None, int | None]:
    """Return ``(topic_id, reply_to_msg_id)`` separating forum topic membership from real replies."""
    header = msg.reply_to
    if not isinstance(header, types.MessageReplyHeader):
        return None, None
    reply_id = header.reply_to_msg_id
    if chat.is_forum and header.forum_topic:
        if header.reply_to_top_id:
            return header.reply_to_top_id, reply_id
        return reply_id, None  # a plain message inside a topic "replies" to the topic root
    return None, reply_id


async def message_from_tl(client: TelegramClient, msg: Any, *, resolve_reply: bool = True) -> Message | None:
    if not isinstance(msg, types.Message):
        return None  # MessageService / MessageEmpty
    chat = peer_from_entity(msg.chat or await msg.get_chat())
    if chat is None:
        return None
    sender_entity = msg.sender or await msg.get_sender()
    sender = peer_from_entity(sender_entity)

    topic_id, reply_to_id = topic_of(msg, chat)
    header = msg.reply_to if isinstance(msg.reply_to, types.MessageReplyHeader) else None
    reply_to_chat_id = None
    if header is not None and header.reply_to_peer_id is not None:
        reply_to_chat_id = utils.get_peer_id(header.reply_to_peer_id)

    result = Message(
        chat=chat,
        id=msg.id,
        date=msg.date,
        sender=sender,
        outgoing=bool(msg.out),
        text=msg.message or "",
        entities=[entity_from_tl(e) for e in (msg.entities or [])],
        reply_to_id=reply_to_id,
        reply_to_chat_id=reply_to_chat_id,
        reply_quote=header.quote_text if header is not None and header.quote else None,
        forward=await forward_from_message(client, msg),
        media=media_from_message(msg),
        webpage=webpage_from_message(msg),
        topic_id=topic_id,
        edit_date=msg.edit_date,
        grouped_id=msg.grouped_id,
        mentioned=bool(msg.mentioned),
        post_author=msg.post_author,
        raw=msg,
    )
    if resolve_reply and reply_to_id is not None and reply_to_chat_id in (None, chat.id):
        try:
            replied = await msg.get_reply_message()
        except Exception as exc:
            logger.debug("Cannot fetch replied message %s/%s: %s", chat.id, reply_to_id, exc)
            replied = None
        if replied is not None:
            result.reply_message = await message_from_tl(client, replied, resolve_reply=False)
    return result
