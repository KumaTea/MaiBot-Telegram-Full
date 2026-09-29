"""Image format sniffing and sticker conversion."""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def detect_format(data: bytes) -> str:
    """Return ``gif``, ``png``, ``jpeg``, ``webp`` or ``unknown`` from magic bytes."""
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def to_sticker_webp(data: bytes) -> tuple[bytes, int, int] | None:
    """Convert a static image into a Telegram sticker: WEBP with its longest side at 512 px.

    Returns ``None`` when Pillow is unavailable or the image cannot be decoded.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGBA")
            scale = 512 / max(image.size)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, "WEBP", quality=90)
            return out.getvalue(), size[0], size[1]
    except Exception as exc:
        logger.debug("Sticker conversion failed: %s", exc)
        return None
