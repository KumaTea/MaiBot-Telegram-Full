"""Link metadata for messages without a Telegram web page preview (requirement R18).

Pages are fetched with a configurable chain of User-Agents (default GoogleBot -> desktop browser
-> curl -> HTTP library default); the next one is tried when a request fails or yields no
metadata. Only http(s) links are fetched, and only the first 512 KB of HTML is read. Results,
including "nothing found", are cached for a day.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp

from ..config import MediaSection
from ..store import Store

UA_PRESETS: dict[str, str | None] = {
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "browser": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "curl": "curl/8.9.1",
    "default": None,  # aiohttp's own User-Agent
    "none": "",  # send no User-Agent header at all
}
_MAX_BYTES = 512 * 1024
_MAX_REDIRECTS = 4
_CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class LinkInfo:
    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            values = {k.lower(): (v or "") for k, v in attrs}
            key = (values.get("property") or values.get("name") or "").lower()
            if key and values.get("content") and key not in self.meta:
                self.meta[key] = values["content"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.done:
            self.title += data


def parse_html_meta(url: str, html: str) -> LinkInfo:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: keep whatever was parsed
        pass
    meta = parser.meta

    def first(*keys: str) -> str | None:
        for key in keys:
            value = " ".join((meta.get(key) or "").split())
            if value:
                return value
        return None

    title = first("og:title", "twitter:title") or (" ".join(parser.title.split()) or None)
    return LinkInfo(
        url=url,
        title=title,
        description=first("og:description", "twitter:description", "description"),
        site_name=first("og:site_name", "application-name"),
    )


class LinkPreviewer:
    def __init__(self, store: Store, settings: Callable[[], MediaSection], logger: logging.Logger) -> None:
        self.store = store
        self.settings = settings
        self.logger = logger

    async def describe(self, url: str) -> LinkInfo | None:
        cached = await self.store.get_link(url, _CACHE_SECONDS)
        if cached is not None:
            return LinkInfo(**cached) if cached else None
        settings = self.settings()
        try:
            # Bound the whole User-Agent chain, not just each attempt.
            info = await asyncio.wait_for(self._fetch(url, settings), timeout=settings.link_timeout * 2 + 1)
        except asyncio.TimeoutError:
            info = None
        await self.store.set_link(url, asdict(info) if info else {})
        return info

    async def _fetch(self, url: str, settings: MediaSection) -> LinkInfo | None:
        for agent in settings.link_user_agents or ["default"]:
            user_agent = UA_PRESETS.get(agent.strip().lower(), agent.strip())
            try:
                info = await self._fetch_once(url, user_agent, settings.link_timeout)
            except _NotPage:
                return None  # not a web page: other User-Agents will not help
            except Exception as exc:
                self.logger.debug("Link fetch %s with %r failed: %r", url, agent, exc)
                continue
            if info is not None and (info.title or info.description):
                return info
        return None

    async def _fetch_once(self, url: str, user_agent: str | None, timeout: float) -> LinkInfo | None:
        if urlparse(url).scheme not in ("http", "https"):
            raise _NotPage
        headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        skip: list[str] = []
        if user_agent:
            headers["User-Agent"] = user_agent
        elif user_agent == "":
            skip.append("User-Agent")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers=headers, skip_auto_headers=skip
        ) as session:
            async with session.get(url, allow_redirects=True, max_redirects=_MAX_REDIRECTS) as response:
                response.raise_for_status()
                if "html" not in response.headers.get("Content-Type", "html").lower():
                    raise _NotPage
                body = await response.content.read(_MAX_BYTES)
                return parse_html_meta(url, body.decode(_charset(response.charset, body), errors="replace"))


class _NotPage(Exception):
    pass


_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", re.I)


def _charset(header_charset: str | None, body: bytes) -> str:
    """Charset from the Content-Type header, else from a ``<meta charset>`` tag, else UTF-8."""
    candidates = [header_charset]
    match = _META_CHARSET.search(body[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii"))
    for candidate in candidates:
        if candidate:
            try:
                codecs.lookup(candidate)
                return candidate
            except LookupError:
                continue
    return "utf-8"
