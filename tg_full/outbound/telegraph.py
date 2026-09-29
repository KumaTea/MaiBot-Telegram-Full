"""Publishing long texts to Telegra.ph (requirements R26.1–R26.3).

Telegraph pages are public to anyone with the link and cannot be deleted. They can be edited, but
only with the token of the account that created them, so every published page is recorded with
its token. "Clearing" overwrites a page with a placeholder, the closest thing to deleting it.

* Markdown is rendered with the same parser as outgoing messages, then mapped onto Telegraph's
  node format and its small tag set (headings become h3/h4, tables become ``a | b`` lines,
  unsupported tags are unwrapped).
* One Telegraph account is created on first use and reused.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote

import aiohttp
from markdown_it import MarkdownIt

from ..config import OutboundSection
from ..store import Store
from ..text.latex import latex_to_plain
from ..text.md_out import _spoiler_rule, render

API_URL = "https://api.telegra.ph"
_ACCOUNT_KEY = "telegraph_account"
_PAGES_KEY = "telegraph_pages"  # [{path, url, title, token, time}], for editing and clearing
_MAX_PAGES = 200
CLEARED_TITLE = "（已清空）"
CLEARED_TEXT = "此页面的内容已被作者清空。"
_MAX_CONTENT_BYTES = 64 * 1024
_TAGS = {
    "h1": "h3", "h2": "h3", "h3": "h4", "h4": "h4", "h5": "h4", "h6": "h4",
    "p": "p", "br": "br", "hr": "hr", "blockquote": "blockquote", "pre": "pre", "code": "code",
    "strong": "strong", "b": "b", "em": "em", "i": "i", "s": "s", "del": "s", "strike": "s", "u": "u", "ins": "u",
    "a": "a", "img": "img", "ul": "ul", "ol": "ol", "li": "li",
    "figure": "figure", "figcaption": "figcaption", "aside": "aside", "iframe": "iframe", "video": "video",
    "tr": "p",  # table rows become paragraphs, cells are joined with " | "
}
_VOID = {"br", "hr", "img"}


class TelegraphError(RuntimeError):
    pass


def page_path(page: str) -> str:
    """``https://telegra.ph/Some-Title-09-30`` (percent-encoded or not) or a bare path -> the path."""
    value = unquote(page.strip())
    for prefix in ("https://telegra.ph/", "http://telegra.ph/", "https://graph.org/", "telegra.ph/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.split("?", 1)[0].split("#", 1)[0].strip("/")


class _NodeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[Any] = []
        self._stack: list[tuple[str, list[Any] | None]] = []  # (html tag, children list or None if unwrapped)
        self._cells = 0

    def _children(self) -> list[Any]:
        for _, children in reversed(self._stack):
            if children is not None:
                return children
        return self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells = 0
        if tag in ("td", "th"):
            if self._cells:
                self._children().append(" | ")
            self._cells += 1
        mapped = _TAGS.get(tag)
        if mapped is None:
            if tag not in _VOID:
                self._stack.append((tag, None))
            return
        node: dict[str, Any] = {"tag": mapped}
        kept = {k: v for k, v in attrs if k in ("href", "src") and v}
        if kept:
            node["attrs"] = kept
        self._children().append(node)
        if tag not in _VOID:
            node["children"] = []
            self._stack.append((tag, node["children"]))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._stack and not data.strip():
            return  # whitespace between top-level blocks
        self._children().append(data)


_CONTAINERS = {"ul", "ol", "blockquote", "figure", "aside", None}  # None: the page itself


def _prune(nodes: list[Any], parent: str | None = None) -> list[Any]:
    """Drop the formatting whitespace markdown-it puts between block tags; keep code verbatim."""
    result: list[Any] = []
    for node in nodes:
        if isinstance(node, str):
            if parent in ("pre", "code"):
                result.append(node)
                continue
            if not node.strip() and ("\n" in node or parent in _CONTAINERS):
                continue
            text = node.lstrip("\n").replace("\n", " ")
            if text:
                result.append(text)
            continue
        if "children" in node:
            node["children"] = _prune(node["children"], node["tag"])
            if not node["children"]:
                del node["children"]
        result.append(node)
    return result


@lru_cache(maxsize=1)
def _html_parser() -> MarkdownIt:
    # Like outgoing messages, but single newlines become <br> (LLM text relies on them).
    md = MarkdownIt("commonmark", {"html": True, "breaks": True}).enable(["strikethrough", "table"])
    md.core.ruler.after("inline", "tg_spoiler", _spoiler_rule)
    return md


def markdown_to_nodes(markdown: str) -> list[Any]:
    html = _html_parser().render(latex_to_plain(markdown))
    builder = _NodeBuilder()
    builder.feed(html)
    builder.close()
    return _prune(builder.root)


def plain_text(markdown: str) -> str:
    return render(latex_to_plain(markdown), keep_entities=False).text


def summary_excerpt(markdown: str, limit: int = 4000) -> str:
    """Plain text of the article cut to fit one Telegram message (``limit`` UTF-16 units)."""
    text = plain_text(markdown)
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def summary_language(text: str) -> str | None:
    """Ask for a Chinese summary of mostly-Chinese text (Telegram otherwise may answer in English)."""
    letters = [c for c in text if c.isalpha()]
    han = sum(1 for c in letters if "一" <= c <= "鿿")
    return "zh" if letters and han / len(letters) > 0.3 else None


def derive_title(markdown: str, limit: int = 64) -> str:
    for line in plain_text(markdown).splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return "Untitled"


class TelegraphClient:
    def __init__(self, store: Store, settings: Callable[[], OutboundSection], logger: logging.Logger) -> None:
        self.store = store
        self.settings = settings
        self.logger = logger

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        form = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))
                for k, v in params.items() if v is not None}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"{API_URL}/{method}", data=form) as response:
                data = await response.json(content_type=None)
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegraphError(str(data.get("error") if isinstance(data, dict) else data))
        return data["result"]

    async def _account(self, author_name: str, author_url: str, renew: bool = False) -> dict[str, Any]:
        account = None if renew else await self.store.get_json(_ACCOUNT_KEY)
        if account is None:
            short_name = re.sub(r"\s+", "", author_name)[:32] or "MaiBot"
            account = await self._call("createAccount", short_name=short_name, author_name=author_name[:128],
                                       author_url=author_url[:512])
            await self.store.set_json(_ACCOUNT_KEY, account)
            self.logger.info("Created Telegraph account %r", short_name)
        return account

    def _prepare(self, markdown: str) -> list[Any]:
        nodes = markdown_to_nodes(markdown)
        if not nodes:
            raise TelegraphError("The text is empty")
        size = len(json.dumps(nodes, ensure_ascii=False).encode())
        if size > _MAX_CONTENT_BYTES:
            raise TelegraphError(f"The text is too long for one Telegraph page ({size} > {_MAX_CONTENT_BYTES} bytes)")
        return nodes

    def _author(self, author_name: str, author_url: str) -> tuple[str, str]:
        settings = self.settings()
        return (settings.telegraph_author_name or author_name)[:128], (settings.telegraph_author_url or author_url)[:512]

    async def publish(self, title: str, markdown: str, author_name: str, author_url: str) -> dict[str, Any]:
        """Create a page; returns Telegraph's Page object (``url``, ``path``, ``title``…)."""
        nodes = self._prepare(markdown)
        author_name, author_url = self._author(author_name, author_url)
        for attempt in range(2):
            account = await self._account(author_name, author_url, renew=attempt == 1)
            try:
                page = await self._call(
                    "createPage", access_token=account["access_token"], title=title[:256],
                    author_name=author_name, author_url=author_url, content=nodes,
                )
            except TelegraphError as exc:
                if "ACCESS_TOKEN_INVALID" not in str(exc) or attempt:
                    raise
                continue
            await self._remember(page, account["access_token"])
            return page
        raise TelegraphError("unreachable")

    async def _remember(self, page: dict[str, Any], token: str) -> None:
        pages = [p for p in await self.store.get_json(_PAGES_KEY, []) if p["path"] != page["path"]]
        pages.append({"path": page["path"], "url": page["url"], "title": page.get("title", ""),
                      "token": token, "time": int(time.time())})
        await self.store.set_json(_PAGES_KEY, pages[-_MAX_PAGES:])

    async def pages(self) -> list[dict[str, Any]]:
        """Pages this adapter published, newest first (without their tokens)."""
        return [{k: v for k, v in p.items() if k != "token"} for p in reversed(await self.store.get_json(_PAGES_KEY, []))]

    async def _own_page(self, page: str) -> dict[str, Any]:
        path = page_path(page)
        if not path:
            raise TelegraphError("page must be a telegra.ph link or path")
        for record in await self.store.get_json(_PAGES_KEY, []):
            if record["path"] == path:
                return record
        # Not recorded (e.g. published before pages were tracked): try the current account;
        # Telegraph itself refuses pages of other accounts.
        account = await self.store.get_json(_ACCOUNT_KEY)
        if account is None:
            raise TelegraphError("No Telegraph account yet, so there is no page to edit")
        return {"path": path, "url": f"https://telegra.ph/{path}", "title": "", "token": account["access_token"]}

    async def _edit_page(self, record: dict[str, Any], **params: Any) -> dict[str, Any]:
        try:
            result = await self._call("editPage", access_token=record["token"], path=record["path"], **params)
        except TelegraphError as exc:
            if "PAGE_ACCESS_DENIED" in str(exc) or "PAGE_NOT_FOUND" in str(exc):
                recent = ", ".join(p["url"] for p in (await self.pages())[:5]) or "none"
                raise TelegraphError(
                    f"{record['url']} was not published by this adapter's Telegraph account, so it cannot be "
                    f"edited ({exc}). Pages it can edit: {recent}"
                ) from exc
            raise
        await self._remember(result, record["token"])
        return result

    async def edit(self, page: str, title: str, markdown: str, author_name: str, author_url: str) -> dict[str, Any]:
        """Replace the title and content of a page this adapter published (the link stays the same)."""
        record = await self._own_page(page)
        nodes = self._prepare(markdown)
        author_name, author_url = self._author(author_name, author_url)
        return await self._edit_page(
            record, title=(title or record["title"] or derive_title(markdown))[:256],
            author_name=author_name, author_url=author_url, content=nodes,
        )

    async def clear(self, page: str) -> dict[str, Any]:
        """Telegraph cannot delete pages: overwrite title and content with a placeholder instead."""
        record = await self._own_page(page)
        return await self._edit_page(
            record, title=CLEARED_TITLE, author_name="", author_url="",
            content=[{"tag": "p", "children": [CLEARED_TEXT]}],
        )
