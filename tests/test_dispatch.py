import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from conftest import ALICE, GROUP, ME, make_message

from tg_full.config import DispatchSection, InboundSection, MediaSection, TelegramFullConfig
from tg_full.inbound.codec import InboundCodec
from tg_full.inbound.dispatcher import ChatState, Debouncer, Dispatcher, Item, adaptive_threshold, next_action
from tg_full.inbound.pipeline import InboundPipeline
from tg_full.store import Store

LOG = logging.getLogger("test")


def state_with(*arrivals, triggers=True, urgent=False, **kwargs) -> ChatState:
    state = ChatState(**kwargs)
    state.items = [Item(GROUP.id, message=make_message(id=i), triggers=triggers, urgent=urgent, arrived=t)
                   for i, t in enumerate(arrivals)]
    state.last_activity = max(arrivals, default=0.0)
    return state


# ---- timing rules ------------------------------------------------------------------------


def test_defaults_flush_immediately():
    assert next_action(state_with(100.0), 100.0, DispatchSection()) == ("flush", 0.0)
    assert next_action(ChatState(), 100.0, DispatchSection()) == ("wait", None)


def test_silence_window_waits_for_quiet():
    settings = DispatchSection(silence_window=5)
    state = state_with(100.0, 103.0)
    assert next_action(state, 104.0, settings) == ("wait", 4.0)
    assert next_action(state, 108.0, settings) == ("flush", 0.0)


def test_typing_hold_is_capped():
    settings = DispatchSection(typing_max_hold=20)
    state = state_with(100.0)
    state.typing = {ALICE.id: 106.0}
    assert next_action(state, 101.0, settings) == ("wait", 5.0)
    state.typing = {ALICE.id: 200.0}  # keeps typing forever
    assert next_action(state, 101.0, settings) == ("wait", 19.0)
    assert next_action(state, 101.0, DispatchSection(typing_hold=False)) == ("flush", 0.0)


def test_urgent_skips_silence_and_lazy_but_not_typing():
    settings = DispatchSection(silence_window=10, lazy_mode="count", lazy_count=5)
    state = state_with(100.0, urgent=True)
    assert next_action(state, 100.0, settings) == ("flush", 0.0)
    state.typing = {ALICE.id: 103.0}
    assert next_action(state, 100.0, settings) == ("wait", 3.0)
    assert next_action(state, 100.0, settings.model_copy(update={"urgent_bypass": False}))[0] == "wait"


def test_lazy_count_interval_and_max_wait():
    count = DispatchSection(lazy_mode="count", lazy_count=3, max_wait=60)
    assert next_action(state_with(100.0, 101.0), 102.0, count) == ("wait", 58.0)
    assert next_action(state_with(100.0, 101.0, 102.0), 102.0, count) == ("flush", 0.0)
    assert next_action(state_with(100.0), 161.0, count) == ("flush", 0.0)  # max_wait wins

    interval = DispatchSection(lazy_mode="interval", lazy_interval=10)
    assert next_action(state_with(100.0, last_flush=95.0), 100.0, interval) == ("wait", 5.0)


def test_adaptive_threshold_follows_chat_pace():
    settings = DispatchSection(lazy_mode="adaptive", lazy_count=3, lazy_max_count=8)
    assert adaptive_threshold(ChatState(), settings) == 3  # nothing known yet
    assert adaptive_threshold(ChatState(interval_ema=30.0), settings) == 1  # 2 msg/min
    assert adaptive_threshold(ChatState(interval_ema=2.0), settings) == 5  # 30 msg/min
    assert adaptive_threshold(ChatState(interval_ema=0.1), settings) == 8  # capped


def test_stacked_items_wait_for_a_real_message_then_go_stale():
    settings = DispatchSection(max_wait=60)
    stacked = state_with(100.0, triggers=False)
    assert next_action(stacked, 110.0, settings) == ("wait", 50.0)
    assert next_action(stacked, 161.0, settings) == ("stale", 0.0)
    stacked.items.append(Item(GROUP.id, message=make_message(id=9), arrived=110.0))
    assert next_action(stacked, 110.0, settings) == ("flush", 0.0)


# ---- dispatcher (real event loop) --------------------------------------------------------


async def test_burst_is_delivered_once_in_order():
    delivered: list[tuple[int, bool]] = []

    async def deliver(item, context_only):
        delivered.append((item.message.id, context_only))

    dispatcher = Dispatcher(lambda: DispatchSection(silence_window=0.15), deliver, LOG)
    for i in range(3):
        dispatcher.add(Item(GROUP.id, message=make_message(id=i)))
        await asyncio.sleep(0.05)
    assert delivered == []
    edited = replace(make_message(id=1), text="edited")
    assert dispatcher.replace_pending(edited)
    assert dispatcher.remove_pending(GROUP.id, {2}) == {2}
    await asyncio.sleep(0.3)
    assert delivered == [(0, False), (1, False)]
    await dispatcher.close()


async def test_debouncer_collapses_bursts_and_keeps_first():
    fired = []

    async def fire(key, value, first):
        fired.append((key, value, first))

    debouncer = Debouncer(lambda: 0.05, fire)
    debouncer.push("k", "v1", first="old")
    debouncer.push("k", "v2", first="ignored")
    assert debouncer.pending("k")
    await asyncio.sleep(0.1)
    assert fired == [("k", "v2", "old")]


# ---- pipeline ------------------------------------------------------------------------------


class FakeContext:
    def __init__(self):
        self.lines = []

    async def append(self, stream_id, segments, **kwargs):
        self.lines.append((stream_id, segments[0]["data"]))
        return {"success": True}


class FakeMaisaka:
    def __init__(self):
        self.context = FakeContext()


class FakeChat:
    async def get_all_streams(self, platform):
        return [{"stream_id": "s-group", "group_id": str(GROUP.id), "account_id": str(ME.id), "is_group_session": True}]


class FakeHost:
    plugin_id = "kumatea.telegram-full"

    def __init__(self):
        self.routed = []
        self.chat = FakeChat()
        self.maisaka = FakeMaisaka()

    async def call_host_method(self, method, payload=None, **kwargs):
        self.routed.append(payload["message"])
        return {"accepted": True}


class FakeBackend:
    def __init__(self, polled=None):
        self.polled = polled or {}

    async def peer(self, chat_id):
        return GROUP if chat_id == GROUP.id else None

    async def get_messages(self, chat_id, ids):
        return [self.polled.get(i) for i in ids]


async def make_pipeline(tmp_path, dispatch=None, polled=None):
    store = Store(tmp_path / "s.sqlite3")
    await store.open()

    async def download(message, thumb):
        return None

    async def known(chat_id, msg_id):
        return True

    codec = InboundCodec(me=ME, settings=InboundSection, media_settings=MediaSection, download=download,
                         is_known=known, logger=LOG)
    config = TelegramFullConfig(dispatch=dispatch or DispatchSection(edit_debounce=0.05))
    host = FakeHost()
    pipeline = InboundPipeline(ctx=host, me=ME, backend=FakeBackend(polled), store=store, codec=codec,
                               config=lambda: config, logger=LOG)
    return pipeline, host, store


async def test_edit_of_delivered_message_becomes_debounced_notice(tmp_path):
    pipeline, host, store = await make_pipeline(tmp_path)
    await pipeline.on_message(make_message("hello", id=5))
    await asyncio.sleep(0.05)
    assert [m["message_id"] for m in host.routed] == [f"{GROUP.id}:5"]

    await pipeline.on_edit(make_message("hello", id=5))  # same content (e.g. a reaction): ignored
    await pipeline.on_edit(make_message("hello wor", id=5))
    await pipeline.on_edit(make_message("hello world", id=5))
    await asyncio.sleep(0.2)
    notice = host.routed[-1]
    assert notice["is_notify"] is True
    assert notice["processed_plain_text"] == "Alice 编辑了消息: 「hello」→「hello world」"
    assert len(host.routed) == 2
    await pipeline.close()
    await store.close()


async def test_deletions_and_stacked_commands(tmp_path):
    pipeline, host, store = await make_pipeline(tmp_path)
    await pipeline.on_message(make_message("bye", id=7))
    await asyncio.sleep(0.05)
    await pipeline.on_delete(GROUP.id, [7])
    await asyncio.sleep(0.05)
    assert host.routed[-1]["processed_plain_text"] == "Alice发送的一条消息被删除了: 「bye」"
    await pipeline.close()
    await store.close()

    stack = DispatchSection(max_wait=60)
    pipeline, host, store = await make_pipeline(tmp_path / "b", dispatch=stack)
    pipeline.config().inbound.command_filter_mode = "stack"
    await pipeline.on_message(make_message("/start@otherbot", id=1))
    await asyncio.sleep(0.05)
    assert host.routed == []  # stacked: waits for a real message
    await pipeline.on_message(make_message("real", id=2))
    await asyncio.sleep(0.05)
    assert [m["message_id"] for m in host.routed] == [f"{GROUP.id}:1", f"{GROUP.id}:2"]
    await pipeline.close()
    await store.close()


async def test_poll_reports_changes_as_context_only(tmp_path):
    now = datetime.now(timezone.utc)  # polling only looks at the last hour
    pipeline, host, store = await make_pipeline(tmp_path, polled={3: make_message("changed", id=3, date=now), 4: None})
    await pipeline.on_message(make_message("orig", id=3, date=now))
    await pipeline.on_message(make_message("gone", id=4, date=now))
    await asyncio.sleep(0.05)
    routed_before = len(host.routed)
    await pipeline.poll_once()
    assert len(host.routed) == routed_before  # nothing routed: no reply can be triggered
    assert sorted(host.maisaka.context.lines) == [
        ("s-group", "[消息删除] Alice发送的一条消息被删除了: 「gone」"),
        ("s-group", "[消息编辑] Alice编辑了消息: 「orig」→「changed」"),
    ]
    await pipeline.close()
    await store.close()


@pytest.mark.parametrize("typing_active", [True, False])
async def test_typing_is_ignored_for_own_account(tmp_path, typing_active):
    pipeline, host, store = await make_pipeline(tmp_path)
    await pipeline.on_typing(GROUP.id, ME.id, typing_active)  # must not create state or hold anything
    assert pipeline.dispatcher._chats == {}
    await pipeline.close()
    await store.close()
