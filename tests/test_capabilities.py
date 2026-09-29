import json

import pytest
from telethon.tl import functions, types

from tg_full.backend.models import Media, MediaRef
from tg_full.backend.raw_api import RawApiError, build_request, is_allowed, resolve_method, result_to_json
from tg_full.config import DEFAULT_RAW_DENY
from tg_full.media.cache import MediaCache
from tg_full.runtime import _plain_emoji, _substitute_chat
from tg_full.store import Store

DENY = list(DEFAULT_RAW_DENY)


@pytest.mark.parametrize(
    "name", ["messages.getHistory", "messages.GetHistoryRequest", "functions.messages.GetHistoryRequest"]
)
def test_resolve_method_spellings(name):
    cls, canonical = resolve_method(name)
    assert cls is functions.messages.GetHistoryRequest and canonical == "messages.getHistory"


def test_resolve_unknown_method():
    with pytest.raises(RawApiError, match="tl.telethon.dev"):
        resolve_method("messages.doesNotExist")


def test_default_deny_list():
    for dangerous in ("messages.deleteMessages", "channels.leaveChannel", "account.updateProfile",
                      "auth.logOut", "payments.sendStarsForm", "channels.editAdmin", "contacts.block"):
        assert not is_allowed(dangerous, ["*"], DENY), dangerous
    for harmless in ("messages.getHistory", "users.getFullUser", "messages.sendReaction"):
        assert is_allowed(harmless, ["*"], DENY), harmless
    assert not is_allowed("users.getFullUser", ["messages.*"], DENY)


def test_build_request_with_camel_case_and_nested_objects():
    request, canonical = build_request(
        "messages.sendReaction",
        {"peer": -100123, "msgId": 5, "reaction": [{"_": "ReactionEmoji", "emoticon": "👍"}], "big": True},
        ["*"], DENY,
    )
    assert canonical == "messages.sendReaction"
    assert request.msg_id == 5 and request.big is True
    assert isinstance(request.reaction[0], types.ReactionEmoji) and request.reaction[0].emoticon == "👍"


def test_build_request_errors_are_helpful():
    with pytest.raises(RawApiError, match="Parameters: .*peer"):
        build_request("messages.getHistory", {"nope": 1}, ["*"], DENY)
    with pytest.raises(RawApiError, match="blocked"):
        build_request("messages.deleteMessages", {"id": [1]}, ["*"], DENY)
    with pytest.raises(RawApiError, match="Unknown TL type"):
        build_request("messages.sendReaction", {"peer": 1, "msg_id": 1, "reaction": [{"_": "Nope"}]}, ["*"], DENY)


def test_result_to_json_handles_bytes_dates_and_truncation():
    photo = types.Photo(id=1, access_hash=2, file_reference=b"\x00\x01", date=None, sizes=[], dc_id=1)
    text, truncated = result_to_json(photo, 10_000)
    data = json.loads(text)
    assert data["_"] == "Photo" and data["file_reference"] == {"_": "bytes", "base64": "AAE="}
    assert not truncated
    assert result_to_json(photo, 20)[1] is True


def test_chat_placeholder_and_emoji_normalizing():
    assert _substitute_chat({"peer": "$chat", "ids": ["$chat", 3]}, -100) == {"peer": -100, "ids": [-100, 3]}
    assert _plain_emoji("❤️") == _plain_emoji("❤")


async def test_recent_stickers_are_unique(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    await store.open()
    cache = MediaCache(store, db=None, logger=None)
    for media_id, emoji in ((1, "😂"), (2, "❤️"), (1, "😂")):
        media = Media("sticker", f"document:{media_id}", emoji=emoji, ref=MediaRef("document", media_id, 9, b""))
        await cache.remember(media, "full", f"sha{media_id}", -100, media_id)
    stickers = await store.recent_stickers()
    assert sorted(s["file_key"] for s in stickers) == ["document:1", "document:2"]  # one row per sticker
    assert (await store.media_by_key("document:2"))["emoji"] == "❤️"
    await store.close()
