"""Telethon client wrapper: connection, login, sending and downloading."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import mimetypes
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telethon import Button, TelegramClient, errors, events, functions, types, utils

from ..ids import ChatTarget
from ..text.entities import Entity
from .convert import best_thumb, media_ref, message_from_tl, peer_from_entity, webpage_from_tl
from .models import CallbackPress, MediaRef, Message, Peer, ReactionChange, WebPage

MessageCallback = Callable[[Message], Awaitable[None]]
CodeProvider = Callable[[], Awaitable[str]]
RefRefresher = Callable[[], Awaitable["MediaRef | None"]]
DeletionCallback = Callable[["int | None", list[int]], Awaitable[None]]
ReactionCallback = Callable[[ReactionChange], Awaitable[None]]
PressCallback = Callable[[CallbackPress], Awaitable[None]]
_RECENT_REACTION_SECONDS = 120
TypingCallback = Callable[[int, int, bool], Awaitable[None]]


class LoginError(RuntimeError):
    """Login cannot proceed without the user changing the configuration."""


def inline_markup(rows: Sequence[Sequence[dict[str, str]]]) -> types.ReplyInlineMarkup:
    """Inline keyboard from ``{"text", "data"}`` / ``{"text", "url"}`` rows.

    Built with Telethon's ``Button`` helpers, which track Telegram layer changes (layer 2xx replaced
    ``KeyboardButtonCallback`` with ``KeyboardInlineButton`` + ``InlineButtonTypeCallback``).
    """
    return types.ReplyInlineMarkup([
        types.KeyboardInlineButtonRow([
            Button.url(b["text"], b["url"]) if b.get("url")
            else Button.inline(b["text"], str(b.get("data") or b["text"]).encode()[:64])
            for b in row
        ])
        for row in rows
    ])


def _reaction_text(reaction: Any) -> str:
    if isinstance(reaction, types.ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, types.ReactionCustomEmoji):
        return "[自定义表情]"
    if isinstance(reaction, types.ReactionPaid):
        return "⭐"
    return "[表情]"


class _Client(TelegramClient):
    """``TelegramClient`` that also catches up after its own automatic reconnects.

    With ``catch_up=True`` Telethon resumes from the update state saved in the session file on
    ``connect()``, much like the Bot API's update offset. After a reconnect inside its sender it
    only pings, so updates sent while the network was down would surface only once a later update
    reveals the gap. Asking for the difference right away closes that window.
    """

    async def _handle_auto_reconnect(self) -> None:
        await super()._handle_auto_reconnect()
        if self._catch_up:
            await self.catch_up()


class RateLimitedError(RuntimeError):
    """Telegram asked us to wait longer than ``flood_sleep_threshold``."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"rate limited by Telegram, retry after {seconds}s")
        self.seconds = seconds


def parse_proxy(url: str) -> dict[str, Any] | None:
    if not url:
        return None
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("socks5", "socks5h", "socks4", "http", "https") or not parsed.hostname:
        raise ValueError(f"Unsupported proxy URL: {url!r}")
    proxy: dict[str, Any] = {
        "proxy_type": "socks5" if scheme.startswith("socks5") else "http" if scheme.startswith("http") else scheme,
        "addr": parsed.hostname,
        "port": parsed.port or (1080 if scheme.startswith("socks") else 8080),
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
        proxy["password"] = unquote(parsed.password or "")
    return proxy


# Errors that retrying cannot fix: the configuration (credentials) must change.
_FATAL_LOGIN_ERRORS = (
    errors.ApiIdInvalidError,
    errors.ApiIdPublishedFloodError,
    errors.AccessTokenInvalidError,
    errors.AccessTokenExpiredError,
    errors.PhoneNumberInvalidError,
    errors.PhoneNumberBannedError,
    errors.PhoneNumberUnoccupiedError,
    errors.AuthKeyDuplicatedError,
)

# Telegram errors for a reply target that no longer exists. Telethon only has a class for
# MSG_ID_INVALID; the others arrive as a plain BadRequestError carrying this message.
_REPLY_TARGET_ERRORS = frozenset({"MSG_ID_INVALID", "REPLY_MESSAGE_ID_INVALID", "REPLY_TO_INVALID"})

_TL_SIMPLE = {
    "bold": types.MessageEntityBold,
    "italic": types.MessageEntityItalic,
    "underline": types.MessageEntityUnderline,
    "strike": types.MessageEntityStrike,
    "spoiler": types.MessageEntitySpoiler,
    "code": types.MessageEntityCode,
}


def _sniff_audio(data: bytes) -> tuple[str, str]:
    """Return ``(mime_type, extension)`` for common audio containers."""
    if data.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg", "mp3"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    if data.startswith(b"#!SILK") or data.startswith(b"\x02#!SILK"):
        return "audio/silk", "silk"
    return "application/octet-stream", "bin"


class TelegramBackend:
    def __init__(
        self,
        *,
        session_path: Path,
        api_id: int,
        api_hash: str,
        proxy: str = "",
        flood_sleep_threshold: int = 60,
        catch_up: bool = True,
        receive_updates: bool = True,
        app_version: str = "",
        logger: logging.Logger,
    ) -> None:
        self.logger = logger
        self.client = _Client(
            str(session_path),
            api_id,
            api_hash,
            proxy=parse_proxy(proxy),
            flood_sleep_threshold=flood_sleep_threshold,
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            catch_up=catch_up,
            receive_updates=receive_updates,
            request_retries=3,
            device_model="MaiBot Telegram Full",
            app_version=app_version,
        )
        self.me: Peer | None = None
        self._chat_locks: dict[int, asyncio.Lock] = {}

    # ---- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> None:
        await self.client.connect()

    @property
    def disconnected(self) -> Awaitable[None]:
        return self.client.disconnected

    def is_connected(self) -> bool:
        return self.client.is_connected()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def login_bot(self, token: str) -> Peer:
        try:
            if not await self.client.is_user_authorized():
                await self.client.sign_in(bot_token=token)
            return await self._load_me()
        except _FATAL_LOGIN_ERRORS as exc:
            raise LoginError(f"{type(exc).__name__}: {exc}") from exc

    async def login_user(self, phone: str, password: str, wait_for_code: CodeProvider) -> Peer:
        """Log in a user account; ``wait_for_code`` blocks until a new login code is supplied."""
        try:
            return await self._login_user(phone, password, wait_for_code)
        except _FATAL_LOGIN_ERRORS as exc:
            raise LoginError(f"{type(exc).__name__}: {exc}") from exc

    async def _login_user(self, phone: str, password: str, wait_for_code: CodeProvider) -> Peer:
        if await self.client.is_user_authorized():
            return await self._load_me()
        if not phone:
            raise LoginError("The session is not logged in and account.phone is empty.")
        sent = await self.client.send_code_request(phone)
        masked = phone[:3] + "*" * max(len(phone) - 6, 0) + phone[-3:] if len(phone) > 6 else "***"
        self.logger.warning(
            "Telegram sent a login code for %s to your other devices. Enter it in the plugin config field "
            "account.login_code (MaiBot WebUI) and save.", masked,
        )
        while True:
            code = await wait_for_code()
            try:
                await self.client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except errors.SessionPasswordNeededError:
                if not password:
                    raise LoginError(
                        "This account has two-step verification enabled, but account.password is empty."
                    ) from None
                try:
                    await self.client.sign_in(password=password)
                except errors.PasswordHashInvalidError:
                    raise LoginError("account.password is not the correct two-step verification password.") from None
                break
            except errors.PhoneCodeInvalidError:
                self.logger.warning("The login code is invalid; enter the correct one in account.login_code.")
            except errors.PhoneCodeExpiredError:
                sent = await self.client.send_code_request(phone)
                self.logger.warning("The login code expired; a new one was sent. Enter it in account.login_code.")
        return await self._load_me()

    async def _load_me(self) -> Peer:
        entity = await self.client.get_me()
        peer = peer_from_entity(entity)
        if peer is None:
            raise LoginError("Cannot read the logged-in account")
        self.me = peer
        return peer

    # ---- events ------------------------------------------------------------------------

    def _message_handler(self, callback: MessageCallback, what: str) -> Callable[[Any], Awaitable[None]]:
        """Wrap ``callback`` so messages of one chat are handled one at a time, in arrival order.

        Telethon runs each update in its own task; without the per-chat lock a message with a slow
        download could be overtaken by the next one, or by its own edit.
        """

        async def handler(event: Any) -> None:
            async with self._chat_locks.setdefault(event.chat_id, asyncio.Lock()):
                try:
                    message = await message_from_tl(self.client, event.message)
                    if message is not None:
                        await callback(message)
                except Exception:
                    self.logger.exception("Failed to handle %s %s in chat %s", what, event.id, event.chat_id)

        return handler

    def on_new_message(self, callback: MessageCallback) -> None:
        self.client.add_event_handler(self._message_handler(callback, "message"), events.NewMessage())

    def on_message_edited(self, callback: MessageCallback) -> None:
        self.client.add_event_handler(self._message_handler(callback, "edit"), events.MessageEdited())

    def on_message_deleted(self, callback: DeletionCallback) -> None:
        """``callback(chat_id, ids)``; ``chat_id`` is ``None`` for private chats and basic groups of
        user accounts, which Telegram reports without a chat. Bots receive no deletions at all."""

        async def handler(event: events.MessageDeleted.Event) -> None:
            try:
                await callback(event.chat_id, list(event.deleted_ids or []))
            except Exception:
                self.logger.exception("Failed to handle deletion in chat %s", event.chat_id)

        self.client.add_event_handler(handler, events.MessageDeleted())

    def on_typing(self, callback: TypingCallback) -> None:
        """``callback(chat_id, user_id, active)`` for "typing / recording / uploading" and their cancel.

        Only user accounts receive these updates."""

        async def handler(event: events.UserUpdate.Event) -> None:
            if event.action is None or event.chat_id is None or event.user_id is None:
                return  # online-status updates
            active = not isinstance(event.action, types.SendMessageCancelAction)
            try:
                await callback(event.chat_id, event.user_id, active)
            except Exception:
                self.logger.exception("Failed to handle typing in chat %s", event.chat_id)

        self.client.add_event_handler(handler, events.UserUpdate())

    def on_reactions(self, callback: ReactionCallback) -> None:
        """Report newly added reactions.

        * User accounts get ``UpdateMessageReactions`` with the latest reactors (including the
          "big" flag); entries newer than two minutes and not seen before are reported. When
          Telegram hides who reacted, increased counts are reported without an actor.
        * Bots get ``UpdateBotMessageReaction`` (they must be admins) with old and new reactions.
        """
        seen: dict[tuple[int, int], set[tuple[Any, ...]]] = {}
        counts: dict[tuple[int, int], dict[str, int]] = {}

        async def emit(change: ReactionChange) -> None:
            try:
                await callback(change)
            except Exception:
                self.logger.exception("Failed to handle reaction in chat %s", change.chat_id)

        async def handler(update: Any) -> None:
            if isinstance(update, types.UpdateBotMessageReaction):
                chat_id = utils.get_peer_id(update.peer)
                old = {_reaction_text(r) for r in update.old_reactions}
                actor = peer_from_entity(await self._entity_or_none(update.actor))
                for reaction in update.new_reactions:
                    if _reaction_text(reaction) not in old:
                        await emit(ReactionChange(chat_id, update.msg_id, _reaction_text(reaction), actor))
                return
            if not isinstance(update, types.UpdateMessageReactions):
                return
            chat_id = utils.get_peer_id(update.peer)
            key = (chat_id, update.msg_id)
            if len(seen) > 2048:
                seen.clear()
                counts.clear()
            recent = update.reactions.recent_reactions or []
            if recent:
                known = seen.setdefault(key, set())
                cutoff = time.time() - _RECENT_REACTION_SECONDS
                for entry in recent:
                    marker = (utils.get_peer_id(entry.peer_id), _reaction_text(entry.reaction), entry.date)
                    if entry.my or marker in known or entry.date.timestamp() < cutoff:
                        known.add(marker)
                        continue
                    known.add(marker)
                    actor = peer_from_entity(await self._entity_or_none(entry.peer_id))
                    await emit(ReactionChange(chat_id, update.msg_id, marker[1], actor, bool(entry.big)))
                return
            current = {_reaction_text(r.reaction): r.count for r in update.reactions.results}
            previous = counts.get(key, {})
            counts[key] = current
            for emoji, count in current.items():
                if count > previous.get(emoji, 0):
                    await emit(ReactionChange(chat_id, update.msg_id, emoji))

        self.client.add_event_handler(
            handler, events.Raw(types=[types.UpdateMessageReactions, types.UpdateBotMessageReaction])
        )

    def on_callback(self, callback: PressCallback) -> None:
        """Inline keyboard presses. Every press is answered so the button stops spinning."""

        async def handler(event: events.CallbackQuery.Event) -> None:
            try:
                data = (event.data or b"").decode("utf-8", errors="replace")
                label = None
                with contextlib.suppress(Exception):
                    message = await event.get_message()
                    for row in (message.buttons or []) if message else []:
                        for button in row:
                            if getattr(button, "data", None) == event.data:
                                label = button.text
                chat = peer_from_entity(await event.get_chat())
                if chat is not None:
                    press = CallbackPress(
                        event.id, chat, event.message_id, data, peer_from_entity(await event.get_sender()), label
                    )
                    await callback(press)
            except Exception:
                self.logger.exception("Failed to handle button press in chat %s", event.chat_id)
            finally:
                with contextlib.suppress(Exception):
                    await event.answer()

        self.client.add_event_handler(handler, events.CallbackQuery())

    async def _entity_or_none(self, peer: Any) -> Any:
        try:
            return await self.client.get_entity(peer)
        except (ValueError, errors.RPCError):
            return None

    # ---- sending ----------------------------------------------------------------------

    async def set_typing(self, target: ChatTarget) -> None:
        """Show "typing…" once; Telegram clears it after ~5 s or when our message arrives."""
        peer = await self.client.get_input_entity(target.chat_id)
        top = target.topic_id if target.topic_id not in (None, 1) else None
        await self.client(functions.messages.SetTypingRequest(peer, types.SendMessageTypingAction(), top_msg_id=top))

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str, big: bool = False) -> None:
        peer = await self.client.get_input_entity(chat_id)
        reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
        try:
            await self.client(functions.messages.SendReactionRequest(
                peer=peer, msg_id=msg_id, reaction=reaction, big=big or None, add_to_recent=True
            ))
        except errors.FloodWaitError as exc:
            raise RateLimitedError(exc.seconds) from exc

    @staticmethod
    def _reply_header(target: ChatTarget, reply_to: int | None) -> types.InputReplyToMessage | None:
        if reply_to is not None:
            return types.InputReplyToMessage(reply_to_msg_id=reply_to, top_msg_id=target.topic_id)
        if target.topic_id is not None and target.topic_id != 1:  # topic 1 is "General": no header
            return types.InputReplyToMessage(reply_to_msg_id=target.topic_id)
        return None

    async def _tl_entities(self, entities: Sequence[Entity]) -> list[Any]:
        result: list[Any] = []
        for e in entities:
            if e.type in _TL_SIMPLE:
                result.append(_TL_SIMPLE[e.type](e.offset, e.length))
            elif e.type == "pre":
                result.append(types.MessageEntityPre(e.offset, e.length, e.language or ""))
            elif e.type == "text_url" and e.url:
                result.append(types.MessageEntityTextUrl(e.offset, e.length, e.url))
            elif e.type == "blockquote":
                result.append(types.MessageEntityBlockquote(e.offset, e.length, collapsed=e.collapsed or None))
            elif e.type == "custom_emoji" and e.custom_emoji_id:
                result.append(types.MessageEntityCustomEmoji(e.offset, e.length, e.custom_emoji_id))
            elif e.type == "mention_name" and e.user_id:
                try:
                    input_user = utils.get_input_user(await self.client.get_input_entity(e.user_id))
                    result.append(types.InputMessageEntityMentionName(e.offset, e.length, input_user))
                except (ValueError, TypeError):
                    result.append(types.MessageEntityTextUrl(e.offset, e.length, f"tg://user?id={e.user_id}"))
        return result

    async def _send(self, request: Any, peer: Any, target: ChatTarget) -> int:
        try:
            try:
                result = await self.client(request)
            except errors.RPCError as exc:
                fallback = self._reply_header(target, None)
                if exc.message not in _REPLY_TARGET_ERRORS or request.reply_to == fallback:
                    raise
                request.reply_to = fallback  # the quoted message is gone; send without quoting
                result = await self.client(request)
        except errors.FloodWaitError as exc:
            raise RateLimitedError(exc.seconds) from exc
        sent = self.client._get_response_message(request, result, peer)
        if sent is None:
            raise RuntimeError("Telegram did not return the sent message")
        return sent.id

    async def send_text(
        self,
        target: ChatTarget,
        text: str,
        entities: Sequence[Entity] = (),
        reply_to: int | None = None,
        link_preview: bool = False,
        buttons: Sequence[Sequence[dict[str, str]]] | None = None,
    ) -> int:
        """Send text. ``buttons`` rows of ``{"text", "data"}`` or ``{"text", "url"}`` need a bot account."""
        peer = await self.client.get_input_entity(target.chat_id)
        markup = inline_markup(buttons) if buttons else None
        request = functions.messages.SendMessageRequest(
            peer=peer,
            message=text,
            reply_to=self._reply_header(target, reply_to),
            no_webpage=not link_preview,
            entities=await self._tl_entities(entities) or None,
            reply_markup=markup,
        )
        return await self._send(request, peer, target)

    async def send_media(
        self,
        target: ChatTarget,
        kind: str,
        data: bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        reply_to: int | None = None,
        caption: str = "",
        caption_entities: Sequence[Entity] = (),
        sticker_emoji: str = "",
        width: int = 512,
        height: int = 512,
    ) -> int:
        """Upload ``data`` and send it as ``kind`` (photo, sticker, animation, voice, audio, document)."""
        peer = await self.client.get_input_entity(target.chat_id)
        media: Any
        if kind == "photo":
            uploaded = await self.client.upload_file(io.BytesIO(data), file_name=file_name or "image.jpg")
            media = types.InputMediaUploadedPhoto(file=uploaded)
        else:
            attributes: list[Any]
            if kind == "sticker":
                mime_type, file_name = "image/webp", "sticker.webp"
                attributes = [
                    types.DocumentAttributeSticker(alt=sticker_emoji, stickerset=types.InputStickerSetEmpty()),
                    types.DocumentAttributeImageSize(w=width, h=height),
                ]
            elif kind == "animation":
                mime_type, file_name = mime_type or "image/gif", file_name or "animation.gif"
                attributes = [types.DocumentAttributeAnimated()]
            elif kind in ("voice", "audio"):
                sniffed_mime, extension = _sniff_audio(data)
                if kind == "voice" and sniffed_mime != "audio/ogg":
                    kind = "audio"  # only OGG/Opus renders as a voice note
                mime_type, file_name = mime_type or sniffed_mime, file_name or f"{kind}.{extension}"
                attributes = [types.DocumentAttributeAudio(duration=0, voice=kind == "voice")]
            else:
                file_name = file_name or "file.bin"
                mime_type = mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                attributes = []
            attributes.append(types.DocumentAttributeFilename(file_name))
            uploaded = await self.client.upload_file(io.BytesIO(data), file_name=file_name)
            media = types.InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime_type,
                attributes=attributes,
                force_file=kind == "document",
            )
        request = functions.messages.SendMediaRequest(
            peer=peer,
            media=media,
            message=caption,
            reply_to=self._reply_header(target, reply_to),
            entities=await self._tl_entities(caption_entities) or None,
        )
        return await self._send(request, peer, target)

    async def send_ref(
        self,
        target: ChatTarget,
        ref: MediaRef,
        *,
        reply_to: int | None = None,
        refresh: RefRefresher | None = None,
    ) -> int:
        """Send an existing Telegram file by reference, refreshing an expired ``file_reference`` once."""
        peer = await self.client.get_input_entity(target.chat_id)
        for attempt in range(2):
            if ref.type == "photo":
                media: Any = types.InputMediaPhoto(types.InputPhoto(ref.id, ref.access_hash, ref.file_reference))
            else:
                media = types.InputMediaDocument(types.InputDocument(ref.id, ref.access_hash, ref.file_reference))
            request = functions.messages.SendMediaRequest(
                peer=peer, media=media, message="", reply_to=self._reply_header(target, reply_to)
            )
            try:
                return await self._send(request, peer, target)
            except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError, errors.FileReferenceEmptyError):
                fresh = await refresh() if refresh is not None and attempt == 0 else None
                if fresh is None:
                    raise
                ref = fresh
        raise RuntimeError("unreachable")

    # ---- reading -----------------------------------------------------------------------

    async def download(self, message: Message, thumb: bool = False) -> bytes | None:
        """Download the message's media, or its best real thumbnail (``None`` if there is none)."""
        raw = message.raw
        if raw is None:
            return None
        thumb_size = None
        if thumb:
            thumb_size = best_thumb(raw.document) if raw.document is not None else None
            if thumb_size is None:
                return None
        data = await self.client.download_media(raw, file=bytes, thumb=thumb_size)
        return data if isinstance(data, bytes) else None

    async def peer(self, chat_id: int) -> Peer | None:
        """Describe a chat from Telethon's entity cache (fetching it if needed)."""
        try:
            return peer_from_entity(await self.client.get_entity(chat_id))
        except (ValueError, errors.RPCError) as exc:
            self.logger.debug("Cannot resolve chat %s: %r", chat_id, exc)
            return None

    async def get_messages(self, chat_id: int, ids: Sequence[int]) -> list[Message | None]:
        """Re-read messages by id (works for bots too). Deleted or inaccessible ones come back ``None``."""
        raws = await self.client.get_messages(chat_id, ids=list(ids))
        return [await message_from_tl(self.client, raw, resolve_reply=False) if raw is not None else None for raw in raws]

    async def summarize_text(self, text: str, to_lang: str | None = None) -> str | None:
        """Telegram's AI summary (Cocoon) of ``text``, for user accounts.

        ``messages.summarizeText`` only works on an existing message, so the text is posted to the
        account's own Saved Messages, summarized once and deleted again. Returns ``None`` when no
        summary is available (quota used up, unsupported, …).
        """
        if self.me is None or self.me.is_bot:
            return None
        sent = await self.client.send_message("me", text, link_preview=False)
        try:
            result = await self.client(functions.messages.SummarizeTextRequest(
                peer=types.InputPeerSelf(), id=sent.id, to_lang=to_lang
            ))
            return (result.text or "").strip() or None
        except errors.RPCError as exc:
            self.logger.info("No Telegram AI summary (%s)", exc.message or type(exc).__name__)
            return None
        finally:
            with contextlib.suppress(Exception):
                await self.client.delete_messages("me", [sent.id])

    async def web_preview(self, url: str) -> WebPage | None:
        """Telegram's own link preview of ``url``, for user accounts. Telegram's servers fetch the page.

        A page Telegram has not seen yet comes back pending; asking again after a short wait
        returns it (usually within a few seconds). ``None`` when Telegram has no preview.
        """
        if self.me is None or self.me.is_bot:
            return None
        for delay in (1, 2, 4, None):
            result = await self.client(functions.messages.GetWebPagePreviewRequest(message=url))
            page = getattr(result.media, "webpage", None)
            if not isinstance(page, types.WebPagePending) or delay is None:
                return webpage_from_tl(page)  # None for no preview or one still pending
            await asyncio.sleep(delay)
        return None

    async def invoke_raw(self, request: Any) -> Any:
        """Send an arbitrary MTProto request (see ``raw_api``); Telethon resolves peer-like parameters."""
        try:
            return await self.client(request)
        except errors.FloodWaitError as exc:
            raise RateLimitedError(exc.seconds) from exc

    async def chat_info(self, chat_id: int) -> dict[str, Any]:
        """Basic facts about a chat: title, type, username, member count and description when available."""
        entity = await self.client.get_entity(chat_id)
        peer = peer_from_entity(entity)
        info: dict[str, Any] = {"id": chat_id}
        if peer is not None:
            info.update({"title": peer.name, "type": peer.kind, "username": peer.username, "forum": peer.is_forum})
        with contextlib.suppress(errors.RPCError, TypeError, ValueError):
            if isinstance(entity, types.Channel):
                full = (await self.client(functions.channels.GetFullChannelRequest(entity))).full_chat
                info.update({"members": full.participants_count, "about": full.about or None})
            elif isinstance(entity, types.Chat):
                full = (await self.client(functions.messages.GetFullChatRequest(entity.id))).full_chat
                info.update({"members": entity.participants_count, "about": full.about or None})
            elif isinstance(entity, types.User):
                full = (await self.client(functions.users.GetFullUserRequest(entity))).full_user
                info.update({"about": full.about or None})
        return {k: v for k, v in info.items() if v is not None}

    async def fetch_media_ref(self, chat_id: int, msg_id: int) -> MediaRef | None:
        """Re-read a message to get a fresh ``file_reference`` for its media."""
        try:
            raw = await self.client.get_messages(chat_id, ids=msg_id)
        except (ValueError, errors.RPCError) as exc:
            self.logger.debug("Cannot refetch %s/%s: %r", chat_id, msg_id, exc)
            return None
        if raw is None:
            return None
        return media_ref(raw.photo or raw.document)
