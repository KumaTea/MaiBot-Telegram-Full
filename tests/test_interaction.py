import asyncio

import pytest
from conftest import ALICE, GROUP, ME, make_message
from test_dispatch import make_pipeline

from tg_full.backend.models import CallbackPress, ReactionChange
from tg_full.config import DispatchSection
from tg_full.runtime import ActionError, normalize_buttons


def test_inline_markup_builds_valid_telethon_objects():
    from tg_full.backend.client import inline_markup

    markup = inline_markup(normalize_buttons([[{"text": "赞成", "data": "vote:yes"}, {"text": "Docs", "url": "https://x"}]]))
    assert bytes(markup)  # serializes with the installed Telegram layer
    first, second = markup.rows[0].buttons
    assert first.type.data == b"vote:yes" and second.type.url == "https://x"


def test_normalize_buttons():
    assert normalize_buttons([{"text": "Yes"}, {"text": "Docs", "url": "https://x"}]) == [
        [{"text": "Yes", "data": "Yes"}, {"text": "Docs", "url": "https://x"}]
    ]
    assert normalize_buttons([[{"text": "A", "data": "a"}], [{"text": "B"}]]) == [
        [{"text": "A", "data": "a"}], [{"text": "B", "data": "B"}]
    ]
    for bad in (None, [], [[]], [[{"text": ""}]], [[{"text": "x", "data": "y" * 65}]]):
        with pytest.raises(ActionError):
            normalize_buttons(bad)


async def _routed(pipeline, store, *messages):
    for message in messages:
        await pipeline.on_message(message)
    await asyncio.sleep(0.05)


async def test_reactions_smart_mode(tmp_path):
    pipeline, host, store = await make_pipeline(tmp_path, dispatch=DispatchSection())
    await _routed(pipeline, store, make_message("alice says", id=1))
    await store.record_message(GROUP.id, 2, ME.id, True, is_outgoing=True, routed=True, text="my reply")
    routed = len(host.routed)

    await pipeline.on_reaction(ReactionChange(GROUP.id, 1, "👍", actor=ALICE))  # someone else's message: context
    await pipeline.on_reaction(ReactionChange(GROUP.id, 2, "❤️", actor=ALICE))  # our message: notice
    await pipeline.on_reaction(ReactionChange(GROUP.id, 1, "😭", actor=ALICE, big=True))  # big: notice
    await pipeline.on_reaction(ReactionChange(GROUP.id, 99, "👍", actor=ALICE))  # unknown message: ignored
    await pipeline.on_reaction(ReactionChange(GROUP.id, 2, "👍", actor=ME))  # our own reaction: ignored
    await asyncio.sleep(0.05)

    assert host.maisaka.context.lines == [("s-group", "[表情回应] Alice回应了Alice的消息「alice says」: 👍")]
    notices = [m["processed_plain_text"] for m in host.routed[routed:]]
    assert notices == ["Alice回应了你的消息「my reply」: ❤️", "Alice长按大表情（强烈情绪）回应了Alice的消息「alice says」: 😭"]
    assert all(m["is_notify"] for m in host.routed[routed:])
    await pipeline.close()
    await store.close()


async def test_callbacks_need_registration(tmp_path):
    pipeline, host, store = await make_pipeline(tmp_path, dispatch=DispatchSection())
    press = CallbackPress(query_id=1, chat=GROUP, msg_id=5, data="vote:yes", sender=ALICE, label="赞成")
    await pipeline.on_callback(press)
    await asyncio.sleep(0.05)
    assert host.routed == []  # dropped by default

    await pipeline.register_callbacks(exact={"vote:yes": "赞成"})
    await pipeline.on_callback(press)
    await pipeline.register_callbacks(pattern=r"^menu:")
    await pipeline.on_callback(CallbackPress(2, GROUP, 5, "menu:open", ALICE))
    await asyncio.sleep(0.05)
    texts = [(m["processed_plain_text"], m["is_notify"]) for m in host.routed]
    assert texts == [("[点击了按钮「赞成」（数据: vote:yes）]", False), ("[点击了按钮「menu:open」]", False)]
    assert await pipeline.unregister_callback_pattern(r"^menu:")
    assert r"^menu:" not in await pipeline.callback_patterns()
    await pipeline.close()
    await store.close()
