"""Link metadata for messages without a Telegram web page preview (requirement R18).

Pages are fetched with a configurable chain of User-Agents (default GoogleBot -> desktop browser
-> curl -> HTTP library default); the next one is tried when a request fails or yields no
metadata. Only http(s) links are fetched, and only the first 512 KB of HTML is read. Results,
including "nothing found", are cached for a day.

Anyone in a chat can post a link, so by default hosts that resolve to loopback, LAN, link-local
(cloud metadata) or other non-public addresses are refused, including on every redirect hop. The
check runs in the connector's resolver, so the connection uses exactly the addresses checked.
Fake-ip placeholders (198.18.0.0/15) are allowed: behind such a proxy every host resolves there,
and the proxy resolves the real host itself. ``media.link_allow_private`` turns the check off.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

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
_REDIRECTS = (301, 302, 303, 307, 308)
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
        except _Blocked as exc:
            self.logger.debug("Link %s not fetched: %s", url, exc)
            return None  # not cached, so turning on media.link_allow_private applies at once
        await self.store.set_link(url, asdict(info) if info else {})
        return info

    async def _fetch(self, url: str, settings: MediaSection) -> LinkInfo | None:
        for agent in settings.link_user_agents or ["default"]:
            user_agent = UA_PRESETS.get(agent.strip().lower(), agent.strip())
            try:
                info = await self._fetch_once(url, user_agent, settings.link_timeout, settings.link_allow_private)
            except _NotPage:
                return None  # not a web page: other User-Agents will not help
            except _Blocked:
                raise
            except Exception as exc:
                self.logger.debug("Link fetch %s with %r failed: %r", url, agent, exc)
                continue
            if info is not None and (info.title or info.description):
                return info
        return None

    async def _fetch_once(
        self, url: str, user_agent: str | None, timeout: float, allow_private: bool
    ) -> LinkInfo | None:
        headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        skip: list[str] = []
        if user_agent:
            headers["User-Agent"] = user_agent
        elif user_agent == "":
            skip.append("User-Agent")
        connector = None if allow_private else aiohttp.TCPConnector(resolver=_PublicResolver())
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers=headers, skip_auto_headers=skip, connector=connector
        ) as session:
            target = URL(url)
            # Redirects are followed here so that every hop gets the same checks.
            for _ in range(_MAX_REDIRECTS + 1):
                _check_target(target, allow_private)
                try:
                    async with session.get(target, allow_redirects=False) as response:
                        location = response.headers.get("Location")
                        if response.status in _REDIRECTS and location:
                            target = response.url.join(URL(location))
                            continue
                        response.raise_for_status()
                        if "html" not in response.headers.get("Content-Type", "html").lower():
                            raise _NotPage
                        body = await response.content.read(_MAX_BYTES)
                        return parse_html_meta(url, body.decode(_charset(response.charset, body), errors="replace"))
                except aiohttp.ClientConnectorError as exc:
                    if isinstance(exc.os_error, _Blocked):
                        raise exc.os_error from None
                    raise
            raise aiohttp.ClientError(f"more than {_MAX_REDIRECTS} redirects")


class _NotPage(Exception):
    pass


class _Blocked(OSError):
    """The link points at a non-public address and ``media.link_allow_private`` is off."""


# Placeholders handed out by proxies in fake-ip DNS mode (Clash / mihomo / sing-box). The range is
# reserved for benchmarking (RFC 2544), so it is not where LAN hosts live, but Python does not count
# it as global.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def _is_allowed(address: str) -> bool:
    """A public address or a fake-ip placeholder: not loopback, LAN, link-local, multicast, …"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip in _FAKE_IP or (ip.is_global and not ip.is_multicast)


def _check_target(url: URL, allow_private: bool) -> None:
    if url.scheme not in ("http", "https") or not url.host:
        raise _NotPage
    if allow_private:
        return
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        return  # a host name: checked by _PublicResolver when connecting
    if not _is_allowed(url.host):
        raise _Blocked(f"{url.host} is not a public address")


class _PublicResolver(AbstractResolver):
    """Resolves with getaddrinfo like aiohttp's default resolver, but drops disallowed addresses."""

    def __init__(self) -> None:
        self._resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list:
        addresses = await self._resolver.resolve(host, port, family)
        allowed = [address for address in addresses if _is_allowed(address["host"])]
        if not allowed:
            raise _Blocked(f"{host} resolves to non-public addresses only")
        return allowed

    async def close(self) -> None:
        await self._resolver.close()


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
