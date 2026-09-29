"""Execute send plans with the quote policies (requirements R14, R21)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..backend.client import TelegramBackend
from ..config import OutboundSection
from ..ids import encode_message_id
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
        logger: logging.Logger,
    ) -> None:
        self.backend = backend
        self.store = store
        self.settings = settings
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

    async def _send_item(self, plan: SendPlan, item: OutItem, reply_to: int | None) -> int:
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
                is_outgoing=True, routed=True,
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
