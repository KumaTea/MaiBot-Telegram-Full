import logging

import pytest

from tg_full.config import OutboundSection
from tg_full.outbound.telegraph import (
    CLEARED_TITLE,
    TelegraphClient,
    TelegraphError,
    derive_title,
    markdown_to_nodes,
    page_path,
)
from tg_full.store import Store


def test_markdown_maps_onto_telegraph_tags():
    nodes = markdown_to_nodes(
        "# 标题\n\n第一段**加粗**和 [链接](https://x.io)。\n第二行。\n\n- a\n- b\n\n"
        "```py\nx = 1\ny = 2\n```\n\n|h1|h2|\n|-|-|\n|1|2|\n\n$a^2$ <u>u</u> ||spoiler||"
    )
    assert nodes == [
        {"tag": "h3", "children": ["标题"]},
        {"tag": "p", "children": ["第一段", {"tag": "strong", "children": ["加粗"]}, "和 ",
                                  {"tag": "a", "attrs": {"href": "https://x.io"}, "children": ["链接"]}, "。",
                                  {"tag": "br"}, "第二行。"]},
        {"tag": "ul", "children": [{"tag": "li", "children": ["a"]}, {"tag": "li", "children": ["b"]}]},
        {"tag": "pre", "children": [{"tag": "code", "children": ["x = 1\ny = 2\n"]}]},
        {"tag": "p", "children": ["h1", " | ", "h2"]},
        {"tag": "p", "children": ["1", " | ", "2"]},
        {"tag": "p", "children": ["a² ", {"tag": "u", "children": ["u"]}, " ", "spoiler"]},
    ]


def test_summary_excerpt_fits_one_message_and_language():
    from tg_full.outbound.telegraph import summary_excerpt, summary_language
    from tg_full.text.entities import utf16_len

    excerpt = summary_excerpt("**加粗**开头" + "😀字" * 3000)
    assert excerpt.startswith("加粗开头") and utf16_len(excerpt) <= 4000
    assert summary_language("Telegram 由杜罗夫兄弟创立，总部在迪拜。") == "zh"
    assert summary_language("Telegram was founded by the Durov brothers.") is None


def test_title_is_the_first_line():
    assert derive_title("# 长文标题\n\n这是第一句。") == "长文标题"
    assert derive_title("很长" * 50, limit=10) == "很长很长很长很长很…"
    assert derive_title("   ") == "Untitled"


class FakeTelegraph(TelegraphClient):
    def __init__(self, store, fail_token_once=False):
        super().__init__(store, OutboundSection, logging.getLogger("t"))
        self.calls = []
        self.fail_token_once = fail_token_once

    async def _call(self, method, **params):
        self.calls.append((method, params))
        if method == "createAccount":
            return {"short_name": params["short_name"], "access_token": f"tok{len(self.calls)}"}
        if self.fail_token_once:
            self.fail_token_once = False
            raise TelegraphError("ACCESS_TOKEN_INVALID")
        if method == "editPage":
            if params["path"] == "Someone-Elses-09-30":
                raise TelegraphError("PAGE_ACCESS_DENIED")
            return {"url": f"https://telegra.ph/{params['path']}", "path": params["path"], "title": params["title"]}
        return {"url": "https://telegra.ph/T-09-30", "path": "T-09-30", "title": params["title"]}


async def test_account_is_created_once_and_renewed_when_invalid(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    await store.open()
    client = FakeTelegraph(store)
    page = await client.publish("T", "hello", "Kuma AI", "https://t.me/Kuma_AI")
    assert page["url"] == "https://telegra.ph/T-09-30"
    await client.publish("T2", "again", "Kuma AI", "")
    assert [c[0] for c in client.calls] == ["createAccount", "createPage", "createPage"]
    assert client.calls[0][1]["short_name"] == "KumaAI"

    client = FakeTelegraph(store, fail_token_once=True)
    await client.publish("T3", "x", "Kuma AI", "")
    assert [c[0] for c in client.calls] == ["createPage", "createAccount", "createPage"]

    with pytest.raises(TelegraphError, match="empty"):
        await client.publish("T", "   ", "Kuma AI", "")
    await store.close()


def test_page_path_accepts_links_and_paths():
    assert page_path("https://telegra.ph/Telegram-%E7%9A%84%E5%8E%86%E5%8F%B2-09-29") == "Telegram-的历史-09-29"
    assert page_path("https://telegra.ph/T-09-30?x=1#top") == "T-09-30"
    assert page_path("T-09-30") == "T-09-30"
    assert page_path("https://graph.org/T-09-30/") == "T-09-30"


async def test_edit_clear_and_ownership(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    await store.open()
    client = FakeTelegraph(store)
    await client.publish("Original", "hello", "Kuma AI", "")
    token = (await store.get_json("telegraph_account"))["access_token"]

    edited = await client.edit("https://telegra.ph/T-09-30", "", "new body", "Kuma AI", "")
    method, params = client.calls[-1]
    assert method == "editPage" and params["access_token"] == token and params["title"] == "Original"
    assert edited["url"] == "https://telegra.ph/T-09-30"

    await client.clear("T-09-30")
    method, params = client.calls[-1]
    assert params["title"] == CLEARED_TITLE and params["content"][0]["tag"] == "p"
    assert (await client.pages())[0]["title"] == CLEARED_TITLE and "token" not in (await client.pages())[0]

    # A page published before tracking: tried with the current account, which Telegraph accepts.
    await client.edit("Older-Page-09-29", "T", "x", "Kuma AI", "")
    assert client.calls[-1][1]["path"] == "Older-Page-09-29"
    with pytest.raises(TelegraphError, match="not published by this adapter"):
        await client.clear("https://telegra.ph/Someone-Elses-09-30")
    await store.close()
