import base64

import pytest
from conftest import ALICE, GROUP, ME, OTHER_BOT, make_message

from tg_full.backend.models import Forward, Media, Peer
from tg_full.config import InboundSection, OutboundSection
from tg_full.ids import ChatTarget, decode_group_id, decode_message_id, encode_group_id, encode_message_id
from tg_full.inbound.codec import InboundCodec, sender_identity
from tg_full.inbound.filters import drop_reason
from tg_full.outbound.codec import OutboundError, build_plan
from tg_full.text.entities import Entity

# ---- ids ---------------------------------------------------------------------------------


def test_group_id_roundtrip_matches_original_adapter_scheme():
    assert encode_group_id(-100123, 7) == "-100123::tg-topic::mt=7"
    assert decode_group_id("-100123::tg-topic::mt=7") == ChatTarget(-100123, 7)
    assert decode_group_id("-100123") == ChatTarget(-100123)


def test_message_id_roundtrip():
    assert decode_message_id(encode_message_id(-100123, 5)) == (-100123, 5)
    assert decode_message_id("5", default_chat_id=42) == (42, 5)
    assert decode_message_id("nope") is None


# ---- filters -----------------------------------------------------------------------------


def test_filters():
    settings = InboundSection()
    assert drop_reason(make_message(), ME, settings) is None
    assert drop_reason(make_message(sender=OTHER_BOT), ME, settings) == "bot sender"
    assert drop_reason(make_message("/start@otherbot"), ME, settings) == "command for another bot"
    assert drop_reason(make_message("/start@rbevbot"), ME, settings) is None
    assert drop_reason(make_message("/start"), ME, settings) is None
    assert drop_reason(make_message("/start"), ME, InboundSection(command_filter="all")) == "command"
    assert drop_reason(make_message(outgoing=True), ME, settings) == "own message"


# ---- inbound -----------------------------------------------------------------------------


def make_codec(known: bool = False, download: bytes | None = b"\x89PNGdata") -> InboundCodec:
    async def fake_download(message, thumb):
        return download

    async def fake_known(chat_id, msg_id):
        return known

    import logging

    return InboundCodec(
        me=ME, settings=InboundSection, download=fake_download, is_known=fake_known, logger=logging.getLogger("t")
    )


async def test_inbound_text_and_self_mention():
    message = make_message("@rbevbot hello", entities=[Entity("mention", 0, 8), Entity("bold", 9, 5)])
    payload = await make_codec().build(message)
    assert payload["message_id"] == "-1001214803045:10"
    assert payload["raw_message"] == [
        {"type": "at", "data": {"target_user_id": str(ME.id)}},
        {"type": "text", "data": "**hello**"},
    ]
    assert payload["is_at"] is True
    info = payload["message_info"]
    assert info["group_info"] == {"group_id": "-1001214803045", "group_name": "Test Group"}
    assert info["user_info"]["user_nickname"] == "Alice"


async def test_inbound_reply_preview_only_when_unknown():
    replied = make_message("original text", id=9, sender=OTHER_BOT)
    message = make_message("answer", reply_to_id=9, reply_message=replied)
    unknown = await make_codec(known=False).build(message)
    assert unknown["raw_message"][0]["type"] == "reply"
    assert unknown["raw_message"][0]["data"]["target_message_id"] == "-1001214803045:9"
    assert unknown["raw_message"][1]["data"].startswith("[回复 Other Bot: original text]")
    known = await make_codec(known=True).build(message)
    assert [s["type"] for s in known["raw_message"]] == ["reply", "text"]
    assert known["reply_to"] == "-1001214803045:9"


async def test_inbound_forward_and_channel_identity():
    channel = Peer(-1009, "channel", "News")
    forwarded = make_message("x", forward=Forward(sender=Peer(55, "user", "Bob")))
    payload = await make_codec().build(forwarded)
    assert payload["raw_message"][0]["data"] == "[转发自 Bob]\n"
    # Linked channel auto-forward: attributed to the channel, no forward marker.
    autopost = make_message("post", sender=channel, forward=Forward(sender=channel))
    payload = await make_codec().build(autopost)
    assert payload["raw_message"] == [{"type": "text", "data": "post"}]
    assert payload["message_info"]["user_info"] == {"user_id": "-1009", "user_nickname": "News（频道）", "user_cardname": None}
    assert sender_identity(make_message(sender=GROUP, post_author="Boss"))[1] == "Test Group（匿名管理员: Boss）"


async def test_inbound_photo_and_private_chat():
    message = make_message("", chat=ALICE, media=Media("photo"))
    payload = await make_codec().build(message)
    segment = payload["raw_message"][0]
    assert segment["type"] == "image"
    assert base64.b64decode(segment["binary_data_base64"]) == b"\x89PNGdata"
    assert "group_info" not in payload["message_info"]
    assert payload["message_info"]["additional_config"]["platform_io_target_user_id"] == str(ALICE.id)


async def test_inbound_empty_message_is_skipped():
    assert await make_codec().build(make_message("")) is None


# ---- outbound ----------------------------------------------------------------------------


def outbound(raw, group="-1001214803045::tg-topic::mt=3", **additional):
    return {
        "message_info": {"additional_config": {"platform_io_target_group_id": group, **additional}},
        "raw_message": raw,
    }


def test_outbound_plan_merges_text_and_mentions():
    plan = build_plan(
        outbound([
            {"type": "reply", "data": {"target_message_id": "-1001214803045:77"}},
            {"type": "at", "data": {"target_user_id": "1001", "target_user_nickname": "Alice"}},
            {"type": "text", "data": " 你好 **世界** $x^2$"},
        ]),
        OutboundSection(),
    )
    assert plan.target == ChatTarget(-1001214803045, 3)
    assert plan.reply_to == 77
    (item,) = plan.items
    assert item.text.text == "@Alice 你好 世界 x²"
    assert [e.type for e in item.text.entities] == ["mention_name", "bold"]


def test_outbound_media_split_and_emoji_kinds():
    gif = base64.b64encode(b"GIF89a....").decode()
    plan = build_plan(
        outbound([
            {"type": "text", "data": "look"},
            {"type": "emoji", "binary_data_base64": gif},
            {"type": "text", "data": "done"},
        ]),
        OutboundSection(),
    )
    assert [i.kind for i in plan.items] == ["text", "animation", "text"]


def test_outbound_errors():
    with pytest.raises(OutboundError):
        build_plan({"message_info": {}, "raw_message": [{"type": "text", "data": "x"}]}, OutboundSection())
    with pytest.raises(ValueError, match="too long"):
        build_plan(outbound([{"type": "text", "data": "x" * 5000}]), OutboundSection())
