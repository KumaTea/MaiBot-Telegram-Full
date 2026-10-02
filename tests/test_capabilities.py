import json

import pytest
from telethon.tl import functions, types

from tg_full.backend.models import Media, MediaRef
from tg_full.backend.raw_api import RawApiError, build_request, is_allowed, resolve_method, result_to_json
from tg_full.config import DEFAULT_RAW_ALLOW, DEFAULT_RAW_DENY
from tg_full.media.cache import MediaCache
from tg_full.runtime import _plain_emoji, _substitute_chat
from tg_full.store import Store

ALLOW = list(DEFAULT_RAW_ALLOW)
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
    assert not is_allowed("users.getFullUser", [], DENY)  # an empty allow list allows nothing


def test_default_allow_list_is_read_only():
    for read in ("messages.getHistory", "users.getFullUser", "channels.getParticipants", "messages.search",
                 "contacts.resolveUsername", "messages.checkChatInvite", "help.getConfig"):
        assert is_allowed(read, ALLOW, DENY), read
    for write in ("messages.sendMessage", "messages.sendReaction", "messages.editMessage", "channels.inviteToChannel",
                  "messages.exportChatInvite", "messages.getBotCallbackAnswer", "account.getPassword"):
        assert not is_allowed(write, ALLOW, DENY), write


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


# ---- user account helper ---------------------------------------------------------------------


class _FakeTL:
    def __init__(self, pages):
        self.pages = list(pages)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.GetWebPagePreviewRequest)
        return types.messages.WebPagePreview(media=types.MessageMediaWebPage(webpage=self.pages.pop(0)), chats=[],
                                             users=[])


async def test_web_preview_waits_for_pending_pages(tmp_path, monkeypatch):
    import logging

    from conftest import ALICE, ME

    from tg_full.backend import client as client_module
    from tg_full.backend.client import TelegramBackend

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    backend = TelegramBackend(session_path=tmp_path / "h.session", api_id=1, api_hash="x", receive_updates=False,
                              logger=logging.getLogger("t"))
    pending = types.WebPagePending(id=1, date=None, url="https://x.io")
    page = types.WebPage(id=1, url="https://x.io", display_url="x.io", hash=0, title="X", site_name="Site")
    backend.client = _FakeTL([pending, pending, page])
    backend.me = ALICE
    preview = await backend.web_preview("https://x.io")
    assert (preview.title, preview.site_name) == ("X", "Site")

    backend.client = _FakeTL([pending] * 4)
    assert await backend.web_preview("https://x.io") is None  # still pending after about 7 s
    backend.me = ME
    assert await backend.web_preview("https://x.io") is None  # bots cannot ask


def test_user_account_is_the_account_itself_or_the_bots_helper():
    import logging
    from types import SimpleNamespace

    from conftest import ALICE, ME

    from tg_full.config import TelegramFullConfig
    from tg_full.runtime import AdapterRuntime

    def account(me, connected=True):
        return SimpleNamespace(me=me, is_connected=lambda: connected)

    runtime = AdapterRuntime(None, TelegramFullConfig, logging.getLogger("t"))
    assert runtime._user_account() is None
    runtime.backend = account(ALICE)
    assert runtime._user_account() is runtime.backend  # a user account serves itself
    runtime.backend = account(ME)
    assert runtime._user_account() is None
    runtime.helper = account(ALICE, connected=False)
    assert runtime._user_account() is None  # helper offline: fall back as before
    runtime.helper = account(ALICE)
    assert runtime._user_account() is runtime.helper
