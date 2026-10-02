import base64
import io
import logging

import pytest
from aiohttp import web
from conftest import GROUP, make_message

from tg_full.backend.models import Media, MediaRef, WebPage
from tg_full.config import InboundSection, MediaSection, OutboundSection
from tg_full.ids import ChatTarget
from tg_full.inbound.codec import InboundCodec, urls_in
from tg_full.media import animation, link_preview
from tg_full.media.cache import MediaCache
from tg_full.media.link_preview import LinkPreviewer, _charset, _is_allowed, parse_html_meta
from tg_full.outbound.codec import build_plan
from tg_full.outbound.sender import OutboundSender
from tg_full.store import Store
from tg_full.text.entities import Entity

LOG = logging.getLogger("test")
STICKER = Media(
    "sticker", "document:77", mime_type="image/webp", emoji="😂", sticker_format="static",
    ref=MediaRef("document", 77, 1234, b"ref"), sticker_set=(5, 6),
)


class FakeDb:
    def __init__(self, descriptions=None):
        self.descriptions = descriptions or {}

    async def get(self, model_name, filters=None, limit=None, order_by=None, single_result=False):
        assert model_name == "Images"
        description = self.descriptions.get((filters["image_hash"], filters["image_type"]))
        return None if description is None else {"image_hash": filters["image_hash"], "description": description}


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    await s.open()
    yield s
    await s.close()


def codec_for(store, db, media_settings=None, payload=b"RIFF\x00\x00\x00\x00WEBPsticker", links=None):
    calls = []

    async def download(message, thumb):
        calls.append(thumb)
        return payload

    async def known(chat_id, msg_id):
        return False

    codec = InboundCodec(
        me=make_message().sender, settings=InboundSection, media_settings=media_settings or MediaSection,
        download=download, is_known=known, cache=MediaCache(store, db, LOG), links=links, logger=LOG,
    )
    return codec, calls


# ---- recognition cache + stickers ------------------------------------------------------------


async def test_sticker_hint_then_cached_without_download(store):
    db = FakeDb()
    codec, downloads = codec_for(store, db)
    first = await codec.build(make_message("", media=STICKER))
    hint, emoji = first["raw_message"]
    assert hint == {"type": "text", "data": "[贴纸 😂]"}
    assert emoji["type"] == "emoji" and emoji["binary_data_base64"]
    assert downloads == [False]

    # MaiBot recognized it in the meantime; the next copy (any chat) reuses that description.
    db.descriptions[(emoji["hash"], "emoji")] = "一只大笑的猫"
    second = await codec.build(make_message("", id=11, media=STICKER))
    assert second["raw_message"][1] == {"type": "emoji", "data": "[表情包: 一只大笑的猫]", "hash": emoji["hash"]}
    assert downloads == [False]  # no second download


async def test_cache_disabled_always_downloads(store):
    db = FakeDb()
    codec, downloads = codec_for(store, db, media_settings=lambda: MediaSection(recognition_cache=False))
    await codec.build(make_message("", media=STICKER))
    sha = (await store.get_media("document:77", "full"))["sha256"]
    db.descriptions[(sha, "emoji")] = "desc"
    await codec.build(make_message("", id=11, media=STICKER))
    assert downloads == [False, False]


async def test_animation_modes(store):
    gif_media = Media("animation", "document:9", has_thumb=True, ref=MediaRef("document", 9, 1, b""))
    codec, downloads = codec_for(store, FakeDb(), media_settings=lambda: MediaSection(animation="thumbnail"),
                                 payload=b"\xff\xd8\xffthumb")
    payload = await codec.build(make_message("", media=gif_media))
    assert payload["raw_message"][0]["type"] == "emoji" and downloads == [True]

    # gif (the default): a file that cannot be converted falls back to the thumbnail.
    other = Media("animation", "document:8", has_thumb=True, ref=MediaRef("document", 8, 1, b""))
    codec, downloads = codec_for(store, FakeDb(), payload=b"\xff\xd8\xffnot a video")
    payload = await codec.build(make_message("", media=other))
    assert payload["raw_message"][0]["type"] == "emoji" and downloads == [False, True]

    codec, downloads = codec_for(store, FakeDb(), media_settings=lambda: MediaSection(animation="drop"))
    payload = await codec.build(make_message("", media=gif_media))
    assert payload["raw_message"] == [{"type": "text", "data": "[动图]"}] and downloads == []


def _tiny_mp4() -> bytes:
    av = pytest.importorskip("av")
    from PIL import Image

    out = io.BytesIO()
    with av.open(out, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(20):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (i * 12, 0, 255 - i * 12)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return out.getvalue()


def test_video_to_gif():
    gif = animation.video_to_gif(_tiny_mp4())
    assert gif is not None and gif[:4] == b"GIF8"
    from PIL import Image

    with Image.open(io.BytesIO(gif)) as image:
        assert image.n_frames > 1


async def test_gif_mode_converts(store):
    mp4 = _tiny_mp4()
    gif_media = Media("animation", "document:10", size=len(mp4), has_thumb=True, ref=MediaRef("document", 10, 1, b""))
    codec, downloads = codec_for(store, FakeDb(), media_settings=lambda: MediaSection(animation="gif"), payload=mp4)
    payload = await codec.build(make_message("", media=gif_media))
    segment = payload["raw_message"][0]
    assert base64.b64decode(segment["binary_data_base64"])[:4] == b"GIF8"
    assert downloads == [False]
    assert (await store.get_media("document:10", "gif")) is not None


# ---- link previews ---------------------------------------------------------------------------


def test_parse_html_meta_prefers_open_graph():
    html = """<html><head><title> Fallback </title>
      <meta property="og:title" content="OG Title"><meta name="description" content="Plain desc">
      <meta property="og:site_name" content="Site"></head><body><meta property="og:title" content="late"></body>"""
    info = parse_html_meta("https://x", html)
    assert (info.title, info.description, info.site_name) == ("OG Title", "Plain desc", "Site")
    assert parse_html_meta("https://x", "<title>Only title</title>").title == "Only title"


def test_charset_helper():
    assert _charset(None, b'<meta charset="gbk">') == "gbk"
    assert _charset("utf-8", b'<meta charset="gbk">') == "utf-8"
    assert _charset(None, b"<html>") == "utf-8"


def test_urls_in_message():
    text = "see example.com and docs"
    message = make_message(text, entities=[Entity("url", 4, 11), Entity("text_url", 20, 4, url="https://d.io/x")])
    assert urls_in(message) == ["http://example.com", "https://d.io/x"]


async def test_link_previewer_only_fetches_http(store):
    previewer = LinkPreviewer(store, MediaSection, LOG)
    assert await previewer.describe("file:///etc/passwd") is None
    assert await store.get_link("file:///etc/passwd", 60) == {}  # negative result cached


def test_only_public_and_fake_ip_addresses_are_allowed():
    for private in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1",
                    "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "nope"):
        assert not _is_allowed(private), private
    for public in ("1.1.1.1", "93.184.215.14", "2606:4700:4700::1111"):
        assert _is_allowed(public), public
    for fake_ip in ("198.18.0.1", "198.18.0.29", "198.19.255.254", "::ffff:198.18.0.5"):
        assert _is_allowed(fake_ip), fake_ip


async def test_link_previewer_refuses_private_hosts(store):
    previewer = LinkPreviewer(store, MediaSection, LOG)
    for url in ("http://127.0.0.1:9/", "http://[::1]:9/", "http://169.254.169.254/latest/meta-data/",
                "http://localhost:9/"):
        assert await previewer.describe(url) is None, url
        assert await store.get_link(url, 60) is None, url  # not cached: allowing private addresses applies at once

    # Allowed: fetched (and failing, as nothing listens on port 9), so the negative result is cached.
    previewer = LinkPreviewer(store, lambda: MediaSection(link_allow_private=True, link_user_agents=["default"]), LOG)
    assert await previewer.describe("http://127.0.0.1:9/") is None
    assert await store.get_link("http://127.0.0.1:9/", 60) == {}


@pytest.fixture
async def local_site():
    """A site on 127.0.0.1 (treated as public by the tests) that redirects within itself and elsewhere."""
    hits = []

    async def handle(request):
        hits.append(request.path)
        if request.path == "/hop":
            raise web.HTTPFound("/page")
        if request.path == "/away":
            raise web.HTTPFound(f"http://127.0.0.2:{request.url.port}/secret")
        return web.Response(text='<head><meta property="og:title" content="Local"></head>', content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


async def test_link_previewer_checks_every_redirect(store, local_site, monkeypatch):
    base, hits = local_site
    monkeypatch.setattr(link_preview, "_is_allowed", lambda address: address == "127.0.0.1")
    previewer = LinkPreviewer(store, lambda: MediaSection(link_user_agents=["default"]), LOG)
    info = await previewer.describe(f"{base}/hop")
    assert info is not None and info.title == "Local" and hits == ["/hop", "/page"]

    assert await previewer.describe(f"{base}/away") is None
    assert hits[-1] == "/away"  # the redirect to 127.0.0.2 was not followed


async def test_link_segments_telegram_preview_and_fetch(store):
    previewer = LinkPreviewer(store, MediaSection, LOG)
    await store.set_link("http://example.com", {"url": "http://example.com", "title": "Example", "description": "D",
                                                 "site_name": None})
    codec, _ = codec_for(store, FakeDb(), links=previewer)
    message = make_message("go example.com", entities=[Entity("url", 3, 11)])
    payload = await codec.build(message)
    assert payload["raw_message"][-1]["data"] == "\n[链接预览: Example — D]"

    page = WebPage("https://t.me/x", site_name="Telegram", title="T", description="From Telegram")
    payload = await codec.build(make_message("https://t.me/x", webpage=page))
    assert payload["raw_message"][-1]["data"] == "\n[链接预览: Telegram | T — From Telegram]"


# ---- native resend ---------------------------------------------------------------------------


class FakeBackend:
    me = GROUP

    def __init__(self):
        self.sent = []

    async def send_ref(self, target, ref, *, reply_to=None, refresh=None):
        self.sent.append(("ref", ref.id))
        return 500

    async def send_media(self, target, kind, data, **kwargs):
        self.sent.append(("upload", kind))
        return 501

    async def send_text(self, *args, **kwargs):
        self.sent.append(("text",))
        return 502


async def test_native_resend_of_known_sticker(store):
    cache = MediaCache(store, FakeDb(), LOG)
    webp = b"RIFF\x00\x00\x00\x00WEBPsticker"
    import hashlib

    sha = hashlib.sha256(webp).hexdigest()
    await cache.remember(STICKER, "full", sha, GROUP.id, 3)
    backend = FakeBackend()
    sender = OutboundSender(backend=backend, store=store, settings=OutboundSection, cache=cache, logger=LOG)
    message = {
        "message_info": {"additional_config": {"platform_io_target_group_id": str(GROUP.id)}},
        "raw_message": [{"type": "emoji", "hash": sha, "binary_data_base64": base64.b64encode(webp).decode()}],
    }
    result = await sender.send(message)
    assert result["success"] and backend.sent == [("ref", 77)]

    # Unknown hashes are uploaded as usual.
    plan = build_plan({**message, "raw_message": [{"type": "emoji", "hash": "other",
                                                   "binary_data_base64": base64.b64encode(webp).decode()}]},
                      OutboundSection())
    assert plan.items[0].source_hash == "other"
    assert plan.target == ChatTarget(GROUP.id)
