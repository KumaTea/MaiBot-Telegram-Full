"""Text length metrics (requirement R26).

* ``utf16_len`` is what Telegram enforces (4096 per message, 1024 per caption).
* ``display_width`` is a deliberately greedy estimate of how much screen a text takes, used
  for the soft "this is getting long" warning: wide CJK characters count 2, emoji count 3.
"""

from __future__ import annotations

import unicodedata

from ..constants import MAX_MESSAGE_LENGTH
from .entities import utf16_len

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # mahjong ... symbols & pictographs extended-A
    (0x2600, 0x27BF),    # misc symbols, dingbats
    (0x2B00, 0x2BFF),    # arrows / stars (⭐)
    (0x2190, 0x21FF),    # arrows often shown as emoji
    (0x3030, 0x303D),
    (0x3297, 0x3299),
)
_ZERO_WIDTH = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


def _is_emoji(code: int) -> bool:
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        code = ord(ch)
        if code in _ZERO_WIDTH or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F:
            continue  # joiners, variation selectors, skin tones, tag characters
        if unicodedata.combining(ch):
            continue
        if _is_emoji(code):
            width += 3
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


class TextTooLongError(ValueError):
    pass


def check_length(text: str, soft_limit: int, hard_limit: int = MAX_MESSAGE_LENGTH) -> str | None:
    """Raise when over Telegram's hard limit; return a warning string when over ``soft_limit``."""
    units = utf16_len(text)
    if units > hard_limit:
        raise TextTooLongError(
            f"Text is too long for one Telegram message ({units} > {hard_limit} UTF-16 units). "
            "Split it into several shorter messages, or publish it with the Telegraph tool and send the link."
        )
    if soft_limit > 0:
        width = display_width(text)
        if width > soft_limit:
            return (
                f"Long message (display width {width} > {soft_limit}). Consider splitting it, "
                "or publishing it to Telegraph and sending a link with a short summary."
            )
    return None
