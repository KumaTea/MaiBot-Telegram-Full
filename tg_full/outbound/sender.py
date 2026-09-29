"""Execute send plans with the quote policies (requirements R14, R21)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..backend.client import TelegramBackend
from ..backend.models import MediaRef
from ..config import MediaSection, OutboundSection
from ..ids import encode_message_id
from ..media.cache import MediaCache
from ..store import Store
from .codec import OutboundError, OutItem, SendPlan, build_plan


class _Drop(Exception):
    pass


class OutboundSender:
    def __init__(
        self,
        *,
        backend: TelegramBackend,
        store: Store,
        settings: Callable[[], OutboundSection],
        media_settings: Callable[[], MediaSection] = MediaSection,
        cache: MediaCache | None = None,
        logger: logging.Logger,
    ) -> None:
        self.backend = backend
        self.store = store
        self.settings = settings
        self.media_settings = media_settings
        self.cache = cache
        self.logger = logger

    async def _quote_target(self, plan: SendPlan) -> int | None:
        settings = self.settings()
        if plan.reply_to is None or settings.quote_policy == "never":
            return None
        if settings.reply_to_bots != "normal":
            meta = await self.store.get_message_meta(plan.target.chat_id, plan.reply_to)
            if meta is not None and meta["sender_is_bot"] and not meta["is_outgoing"]:
                if settings.reply_to_bots == "drop":
                    raise _Drop
                return None
        return plan.reply_to

    async def _send_native(self, plan: SendPlan, item: OutItem, reply_to: int | None) -> int | None:
        """Resend a Telegram file MaiBot got from us (e.g. a collected sticker) by reference."""
        if self.cache is None or not item.source_hash or not self.media_settings().native_resend:
            return None
        found = await self.cache.ref_for_hash(item.source_hash)
        if found is None:
            return None
        ref, row = found

        async def refresh() -> MediaRef | None:
            if row["origin_chat"] is None or row["origin_msg"] is None:
                return None
            fresh = await self.backend.fetch_media_ref(row["origin_chat"], row["origin_msg"])
            if fresh is None or fresh.id != ref.id:
                return None
            await self.store.update_file_reference(row["file_key"], fresh.file_reference)
            return fresh

        try:
            return await self.backend.send_ref(plan.target, ref, reply_to=reply_to, refresh=refresh)
        except Exception as exc:
            self.logger.info("Native resend of %s failed (%r); uploading instead", row["file_key"], exc)
            return None

    async def _send_item(self, plan: SendPlan, item: OutItem, reply_to: int | None) -> int:
        if item.kind != "text":
            native = await self._send_native(plan, item, reply_to)
            if native is not None:
                return native
        if item.kind == "text" and item.text is not None:
            return await self.backend.send_text(
                plan.target, item.text.text, item.text.entities, reply_to, self.settings().link_preview
            )
        if item.data is None:
            raise OutboundError(f"{item.kind} has no data")
        return await self.backend.send_media(
            plan.target, item.kind, item.data,
            file_name=item.file_name, mime_type=item.mime_type, reply_to=reply_to,
            width=item.width, height=item.height,
        )

    async def send(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = build_plan(message, self.settings())
            reply_to = await self._quote_target(plan)
        except ValueError as exc:  # OutboundError, TextTooLongError
            return {"success": False, "error": str(exc)}
        except _Drop:
            return {"success": False, "error": "Replying to messages from other bots is disabled"}

        for warning in plan.warnings:
            self.logger.warning("%s", warning)

        me = self.backend.me
        sent: list[int] = []
        failures: list[str] = []
        for item in plan.items:
            quote = reply_to if not sent else None
            try:
                msg_id = await self._send_item(plan, item, quote)
            except Exception as exc:
                self.logger.warning("Sending %s to %s failed: %r", item.kind, plan.target.chat_id, exc)
                failures.append(f"{item.kind}: {exc}")
                continue
            sent.append(msg_id)
            await self.store.record_message(
                plan.target.chat_id, msg_id, me.id if me else None, bool(me and me.is_bot),
                is_outgoing=True, routed=True, topic_id=plan.target.topic_id,
                text=item.text.text[:500] if item.text is not None else f"[{item.kind}]",
            )

        if not sent:
            return {"success": False, "error": "; ".join(failures) or "Nothing was sent"}
        return {
            "success": True,
            "external_message_id": encode_message_id(plan.target.chat_id, sent[0]),
            "metadata": {
                "telegram_message_ids": sent,
                "warnings": plan.warnings,
                "partial_failures": failures,
            },
        }
