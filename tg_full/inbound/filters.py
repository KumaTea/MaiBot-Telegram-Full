"""Adapter-side inbound filters.

Chat allow/block lists are not here: MaiBot applies its unified adapter policy
(``config/adapter_policy.toml``, editable in the WebUI) to every routed message.
"""

from __future__ import annotations

import re

from ..backend.models import Message, Peer
from ..config import InboundSection

_COMMAND = re.compile(r"^/[A-Za-z0-9_]+(?:@([A-Za-z0-9_]+))?(?:\s|$)")


def command_target(text: str) -> tuple[bool, str | None]:
    """Return ``(is_command, addressed_bot_username)``."""
    match = _COMMAND.match(text)
    if match is None:
        return False, None
    return True, match.group(1)


def drop_reason(message: Message, me: Peer, settings: InboundSection) -> str | None:
    """Why ``message`` should not reach MaiBot, or ``None`` to keep it."""
    if message.chat.is_channel:
        return "broadcast channel"
    # Telethon never dispatches updates caused by our own requests, so "own" messages were typed by
    # a human on another device logged into the same (user) account.
    own = message.outgoing or (message.sender is not None and message.sender.id == me.id)
    if own and settings.own_messages == "drop":
        return "own message"
    if not own and settings.ignore_bot_messages and message.sender is not None and message.sender.is_bot:
        return "bot sender"
    if settings.command_filter != "off":
        is_command, addressed = command_target(message.text)
        if is_command:
            if settings.command_filter == "all":
                return "command"
            if addressed and (me.username or "").lower() != addressed.lower():
                return "command for another bot"
    return None
