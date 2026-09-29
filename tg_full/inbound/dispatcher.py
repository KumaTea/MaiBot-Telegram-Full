"""Per-chat delivery timing (requirements R7–R10, and R12's "stack" mode).

Every chat has a queue of items waiting to be handed to MaiBot. A per-chat task asks
:func:`next_action` what to do, then flushes the whole queue in order or sleeps until the next
deadline (or until something happens in the chat).

* R8 silence window: wait until the chat has been quiet (no new, edited or deleted messages).
* R9 typing hold: wait while someone is typing (user accounts only), up to a limit.
* R10 lazy push: wait for N messages, a fixed interval, or an adaptive N that grows with activity.
* R12 stack: stacked items (filtered commands) never cause a flush; they ride along with the next
  real message, or go to MaiBot as context-only after ``max_wait``.
* Mentions of the account and private chats skip the silence window and lazy push by default.
* Nothing waits longer than ``max_wait``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..backend.models import Message, Peer
from ..config import DispatchSection

TYPING_TTL = 6.0  # Telegram shows "typing…" for ~5 s unless the client refreshes it
_IDLE_EXIT = 300.0  # an idle chat's task stops after this long
_EMA_ALPHA = 0.3


@dataclass(frozen=True)
class Notice:
    """Something that happened in a chat, reported to MaiBot like NapCat's notices."""

    chat: Peer
    topic_id: int | None
    actor_id: str
    actor_name: str
    text: str
    key: str  # unique and stable, used for the message id and de-duplication
    is_notify: bool = True  # False for user actions aimed at us, such as button presses


@dataclass
class Item:
    chat_id: int
    message: Message | None = None
    notice: Notice | None = None
    triggers: bool = True  # False for stacked items
    urgent: bool = False
    arrived: float = 0.0


@dataclass
class ChatState:
    items: list[Item] = field(default_factory=list)
    last_activity: float = 0.0
    last_flush: float = 0.0
    typing: dict[int, float] = field(default_factory=dict)  # user id -> typing-until
    interval_ema: float | None = None
    last_arrival: float | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


Action = tuple[Literal["flush", "stale", "wait"], float | None]


def adaptive_threshold(state: ChatState, settings: DispatchSection) -> int:
    """Batch size from the chat's pace: about one message per 10 s of recent activity."""
    if state.interval_ema is None:
        return settings.lazy_count
    per_minute = 60.0 / max(state.interval_ema, 0.5)
    return max(1, min(settings.lazy_max_count, math.ceil(per_minute / 6)))


def next_action(state: ChatState, now: float, settings: DispatchSection) -> Action:
    """``("flush", 0)``, ``("stale", 0)`` (deliver stacked items as context) or ``("wait", seconds | None)``."""
    triggering = [item for item in state.items if item.triggers]
    if not triggering:
        stacked = [item.arrived + settings.max_wait for item in state.items]
        if not stacked:
            return "wait", None
        deadline = min(stacked)
        return ("stale", 0.0) if now >= deadline else ("wait", deadline - now)

    oldest = min(item.arrived for item in triggering)
    hard_deadline = oldest + settings.max_wait
    if now >= hard_deadline:
        return "flush", 0.0

    deadline = now
    typing_until = max((until for until in state.typing.values() if until > now), default=0.0)
    if settings.typing_hold and typing_until > now:
        deadline = max(deadline, min(typing_until, oldest + settings.typing_max_hold))

    if not (settings.urgent_bypass and any(item.urgent for item in triggering)):
        if settings.silence_window > 0:
            deadline = max(deadline, state.last_activity + settings.silence_window)
        if settings.lazy_mode in ("count", "adaptive"):
            threshold = settings.lazy_count if settings.lazy_mode == "count" else adaptive_threshold(state, settings)
            if len(triggering) < threshold:
                deadline = hard_deadline
        elif settings.lazy_mode == "interval":
            deadline = max(deadline, state.last_flush + settings.lazy_interval)

    delay = min(deadline, hard_deadline) - now
    return ("flush", 0.0) if delay <= 0 else ("wait", delay)


Deliver = Callable[[Item, bool], Awaitable[None]]  # (item, context_only)


class Dispatcher:
    def __init__(
        self,
        settings: Callable[[], DispatchSection],
        deliver: Deliver,
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.deliver = deliver
        self.logger = logger
        self.clock = clock
        self._chats: dict[int, ChatState] = {}

    # ---- producers ---------------------------------------------------------------------

    def _state(self, chat_id: int) -> ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = self._chats[chat_id] = ChatState()
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._run(chat_id, state), name=f"telegram_full.dispatch.{chat_id}")
        return state

    def _activity(self, state: ChatState, now: float) -> None:
        state.last_activity = now
        state.wake.set()

    def add(self, item: Item) -> None:
        now = self.clock()
        item.arrived = now
        state = self._state(item.chat_id)
        if item.triggers and item.message is not None:
            if state.last_arrival is not None:
                interval = now - state.last_arrival
                ema = state.interval_ema
                state.interval_ema = interval if ema is None else ema + _EMA_ALPHA * (interval - ema)
            state.last_arrival = now
        state.items.append(item)
        self._activity(state, now)

    def replace_pending(self, message: Message) -> bool:
        """Swap in the edited version of a message that has not been delivered yet."""
        state = self._chats.get(message.chat.id)
        if state is None:
            return False
        for item in state.items:
            if item.message is not None and item.message.id == message.id:
                item.message = message
                self._activity(state, self.clock())
                return True
        return False

    def remove_pending(self, chat_id: int, msg_ids: set[int]) -> set[int]:
        """Drop deleted messages that have not been delivered yet; returns the ids removed."""
        state = self._chats.get(chat_id)
        if state is None:
            return set()
        removed = {i.message.id for i in state.items if i.message is not None and i.message.id in msg_ids}
        if removed:
            state.items = [i for i in state.items if i.message is None or i.message.id not in removed]
            self._activity(state, self.clock())
        return removed

    def activity(self, chat_id: int) -> None:
        """An edit or deletion happened: it restarts the silence window of a chat with pending items."""
        state = self._chats.get(chat_id)
        if state is not None:
            self._activity(state, self.clock())

    def typing(self, chat_id: int, user_id: int, active: bool) -> None:
        state = self._chats.get(chat_id)
        if state is None:
            return  # nothing pending: typing only matters while messages wait
        if active:
            state.typing[user_id] = self.clock() + TYPING_TTL
        else:
            state.typing.pop(user_id, None)
        state.wake.set()

    # ---- consumer ----------------------------------------------------------------------

    async def _run(self, chat_id: int, state: ChatState) -> None:
        while True:
            state.wake.clear()
            now = self.clock()
            state.typing = {user: until for user, until in state.typing.items() if until > now}
            action, delay = next_action(state, now, self.settings())
            if action == "wait":
                try:
                    await asyncio.wait_for(state.wake.wait(), timeout=_IDLE_EXIT if delay is None else delay)
                except asyncio.TimeoutError:
                    if delay is None and not state.items:
                        self._chats.pop(chat_id, None)
                        return
                continue
            if action == "stale":
                items, state.items = state.items, []
                context_only = True
            else:
                items, state.items = state.items, []
                state.last_flush = now
                context_only = False
            for item in items:
                try:
                    await self.deliver(item, context_only)
                except Exception:
                    self.logger.exception("Delivering an item for chat %s failed", chat_id)

    async def close(self) -> None:
        tasks = [state.task for state in self._chats.values() if state.task is not None]
        self._chats.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class Debouncer:
    """Collapse bursts of events per key: ``fire`` runs once the key has been quiet for ``delay()``."""

    def __init__(self, delay: Callable[[], float], fire: Callable[[Hashable, Any, Any], Awaitable[None]]) -> None:
        self.delay = delay
        self.fire = fire
        self._pending: dict[Hashable, tuple[Any, Any, asyncio.Task[None]]] = {}

    def push(self, key: Hashable, value: Any, first: Any = None) -> None:
        """Record the latest ``value``; ``first`` is kept from the first push of a burst."""
        previous = self._pending.get(key)
        if previous is not None:
            first = previous[0]
            previous[2].cancel()
        self._pending[key] = (first, value, asyncio.create_task(self._wait(key)))

    def pending(self, key: Hashable) -> bool:
        return key in self._pending

    async def _wait(self, key: Hashable) -> None:
        await asyncio.sleep(self.delay())
        first, value, _ = self._pending.pop(key)
        try:
            await self.fire(key, value, first)
        except Exception:
            logging.getLogger(__name__).exception("Debounced handler for %s failed", key)

    async def close(self) -> None:
        tasks = [entry[2] for entry in self._pending.values()]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
