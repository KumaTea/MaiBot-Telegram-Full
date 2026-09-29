"""Telegram media MaiBot has already seen (requirements R15, R15.1, R16).

Every media file passed to MaiBot is recorded as
``Telegram file (+ variant) -> sha256 of the bytes MaiBot got -> Telegram file reference``.

* Inbound: when the same Telegram file shows up again and MaiBot already has a description for
  that sha256 (host ``Images`` table), the segment is sent with that description and no bytes.
  MaiBot then skips recognition, and the adapter skips the download.
* Outbound: when MaiBot sends an image or emoji whose hash is a known Telegram file (typically a
  sticker it collected), the adapter resends the original file by reference, so stickers and
  GIFs stay native and nothing is uploaded.
"""

from __future__ import annotations

import logging
from typing import Any

from ..backend.models import Media
from ..store import Store

# MaiBot's own rendering of recognized media (src/chat/message_receive/message.py).
_CONTENT_FORMAT = {"image": "[图片：{}]", "emoji": "[表情包: {}]"}


def _record(value: Any) -> dict[str, Any] | None:
    """Unwrap a ``database.get`` result into a single record."""
    if isinstance(value, dict) and "result" in value and "success" in value:
        value = value["result"]
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict) and "image_hash" in value:
        return value
    return None


class MediaCache:
    def __init__(self, store: Store, db: Any, logger: logging.Logger) -> None:
        self.store = store
        self.db = db
        self.logger = logger

    async def describe(self, sha256: str, segment_type: str) -> str | None:
        """MaiBot's stored description for these bytes, if it has recognized them already."""
        try:
            record = _record(
                await self.db.get(
                    "Images", filters={"image_hash": sha256, "image_type": segment_type}, single_result=True
                )
            )
        except Exception as exc:
            self.logger.debug("Images lookup failed for %s: %r", sha256, exc)
            return None
        if record is None:
            return None
        description = str(record.get("description") or "").strip()
        return description or None

    async def cached_segment(self, media: Media, variant: str, segment_type: str) -> dict[str, Any] | None:
        """A ready-made segment for a file MaiBot already recognized, or ``None``."""
        if not media.file_key:
            return None
        row = await self.store.get_media(media.file_key, variant)
        if row is None:
            return None
        description = await self.describe(row["sha256"], segment_type)
        if description is None:
            return None
        return {
            "type": segment_type,
            "data": _CONTENT_FORMAT[segment_type].format(description),
            "hash": row["sha256"],
        }

    async def remember(self, media: Media, variant: str, sha256: str, origin_chat: int, origin_msg: int) -> None:
        if not media.file_key or media.ref is None:
            return
        ref = media.ref
        await self.store.remember_media({
            "file_key": media.file_key,
            "variant": variant,
            "sha256": sha256,
            "kind": media.kind,
            "ref_type": ref.type,
            "media_id": ref.id,
            "access_hash": ref.access_hash,
            "file_reference": ref.file_reference,
            "origin_chat": origin_chat,
            "origin_msg": origin_msg,
            "emoji": media.emoji,
            "set_id": media.sticker_set[0] if media.sticker_set else None,
            "set_access_hash": media.sticker_set[1] if media.sticker_set else None,
        })
