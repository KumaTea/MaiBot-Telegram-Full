"""Telegram animations (MP4 "GIFs", WEBM video stickers) -> small real GIFs (requirement R17).

MaiBot understands GIF emoji: it stitches up to 15 distinct frames into one image for its
vision model. Decoding video needs PyAV, which bundles ffmpeg in its wheels, so no system
package is required. PyAV is optional: it is only installed when the user enables it.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import shutil
import sys

_MAX_FRAMES = 12
_MAX_SIDE = 320
_MAX_SECONDS = 6.0

_install_lock = asyncio.Lock()
_install_failed = False


def pyav_available() -> bool:
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


async def install_pyav(logger: logging.Logger) -> bool:
    """Install PyAV into the running interpreter's environment (once per process)."""
    global _install_failed
    if pyav_available():
        return True
    if _install_failed:
        return False
    async with _install_lock:
        if pyav_available():
            return True
        if shutil.which("uv"):
            command = ["uv", "pip", "install", "--python", sys.executable, "av"]
        else:
            command = [sys.executable, "-m", "pip", "install", "av"]
        logger.warning("Installing PyAV for GIF conversion: %s", " ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
        except (OSError, asyncio.TimeoutError) as exc:
            _install_failed = True
            logger.error("PyAV installation failed: %r. Animations use thumbnails instead.", exc)
            return False
        if process.returncode != 0:
            _install_failed = True
            logger.error("PyAV installation failed: %s", stderr.decode(errors="replace")[-500:])
            return False
        importlib.invalidate_caches()
        ok = pyav_available()
        logger.warning("PyAV installed%s", "" if ok else ", but it cannot be imported until MaiBot restarts")
        return ok


def video_to_gif(data: bytes) -> bytes | None:
    """Sample up to 12 frames from the first 6 s into a GIF at most 320 px wide. CPU bound."""
    import av
    from PIL import Image

    frames: list[Image.Image] = []
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.video:
            return None
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = 0.0
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = container.duration / 1_000_000
        span = min(duration, _MAX_SECONDS) if duration > 0 else _MAX_SECONDS
        step = span / _MAX_FRAMES
        next_time = 0.0
        for frame in container.decode(stream):
            timestamp = float(frame.pts * stream.time_base) if frame.pts is not None and stream.time_base else next_time
            if timestamp > span:
                break
            if timestamp + 1e-6 < next_time:
                continue
            image = frame.to_image()
            image.thumbnail((_MAX_SIDE, _MAX_SIDE))
            frames.append(image.convert("RGB"))
            next_time += step
            if len(frames) >= _MAX_FRAMES:
                break
    if not frames:
        return None
    out = io.BytesIO()
    frames[0].save(
        out, "GIF", save_all=True, append_images=frames[1:], duration=max(int(step * 1000), 50), loop=0, optimize=True
    )
    return out.getvalue()
