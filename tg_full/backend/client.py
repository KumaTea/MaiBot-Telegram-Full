"""Telethon client wrapper: connection, login, sending and downloading."""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telethon import TelegramClient, errors, events, functions, types, utils

from ..ids import ChatTarget
from ..text.entities import Entity
from .convert import message_from_tl, peer_from_entity
from .models import Message, Peer

MessageCallback = Callable[[Message], Awaitable[None]]
CodeProvider = Callable[[], Awaitable[str]]


class LoginError(RuntimeError):
    """Login cannot proceed without the user changing the configuration."""


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
        logger: logging.Logger,
    ) -> None:
        self.logger = logger
        self.client = TelegramClient(
            str(session_path),
            api_id,
            api_hash,
            proxy=parse_proxy(proxy),
            flood_sleep_threshold=flood_sleep_threshold,
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            request_retries=3,
        )
        self.me: Peer | None = None

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
        sent = await self.client.send_code_request(phone)
        self.logger.warning(
            "Telegram sent a login code to %s. Enter it in the plugin config field account.login_code "
            "(MaiBot WebUI) and save.", phone,
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

    def on_new_message(self, callback: MessageCallback) -> None:
        """Deliver new messages to ``callback`` one at a time per chat, in arrival order.

        Telethon runs each update in its own task; without the per-chat lock a message with a
        slow download could be overtaken by the next one.
        """
        locks: dict[int, asyncio.Lock] = {}

        async def handler(event: events.NewMessage.Event) -> None:
            async with locks.setdefault(event.chat_id, asyncio.Lock()):
                try:
                    message = await message_from_tl(self.client, event.message)
                    if message is not None:
                        await callback(message)
                except Exception:
                    self.logger.exception("Failed to handle message %s in chat %s", event.id, event.chat_id)

        self.client.add_event_handler(handler, events.NewMessage())

    # ---- sending -----------------------------------------------------------------------

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
            except (errors.ReplyMessageIdInvalidError, errors.MsgIdInvalidError):
                fallback = self._reply_header(target, None)
                if request.reply_to == fallback:
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
    ) -> int:
        peer = await self.client.get_input_entity(target.chat_id)
        request = functions.messages.SendMessageRequest(
            peer=peer,
            message=text,
            reply_to=self._reply_header(target, reply_to),
            no_webpage=not link_preview,
            entities=await self._tl_entities(entities) or None,
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

    # ---- reading -----------------------------------------------------------------------

    async def download(self, message: Message, thumb: bool = False) -> bytes | None:
        raw = message.raw
        if raw is None:
            return None
        data = await self.client.download_media(raw, file=bytes, thumb=-1 if thumb else None)
        return data if isinstance(data, bytes) else None
