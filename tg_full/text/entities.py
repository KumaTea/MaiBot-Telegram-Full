"""Backend-neutral message entity model.

Offsets and lengths are in UTF-16 code units, exactly as Telegram defines them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Entity types that wrap inline text and may be trimmed of surrounding whitespace.
INLINE_TYPES = frozenset({"bold", "italic", "underline", "strike", "spoiler", "code", "text_url", "mention_name"})


@dataclass(frozen=True)
class Entity:
    type: str
    offset: int
    length: int
    url: str | None = None
    user_id: int | None = None
    language: str | None = None
    custom_emoji_id: int | None = None
    collapsed: bool = False

    @property
    def end(self) -> int:
        return self.offset + self.length

    def moved(self, offset: int, length: int) -> Entity:
        return replace(self, offset=offset, length=length)


def utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def utf16_units(text: str) -> bytes:
    return text.encode("utf-16-le")


def units_to_str(units: bytes, start: int, end: int) -> str:
    return units[start * 2 : end * 2].decode("utf-16-le", errors="replace")
