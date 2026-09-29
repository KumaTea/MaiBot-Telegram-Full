"""Markdown -> Telegram text + entities, for messages coming from MaiBot.

Uses markdown-it-py (CommonMark + strikethrough + tables) plus two Telegram-specific
extensions: ``||spoiler||`` and a few inline HTML tags (``<u>``, ``<b>``, ``<br>``, ...).
Constructs Telegram cannot display are flattened: headings become bold lines, list items get
``•`` / ``1.`` prefixes, tables become ``a | b`` rows, rules become a line of dashes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token

from .entities import INLINE_TYPES, Entity, utf16_len


@dataclass(frozen=True)
class FormattedText:
    text: str
    entities: tuple[Entity, ...] = ()


def _spoiler_rule(state: StateCore) -> None:
    """Split ``||`` markers in text tokens into spoiler open/close tokens.

    Runs after inline parsing, so code spans (separate token type) are never touched. An
    unmatched trailing ``||`` stays literal.
    """
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        children: list[Token] = []
        open_index: int | None = None
        for child in block.children:
            if child.type != "text" or "||" not in child.content:
                children.append(child)
                continue
            for index, part in enumerate(child.content.split("||")):
                if index:
                    if open_index is None:
                        open_index = len(children)
                        children.append(Token("spoiler_open", "tg-spoiler", 1))
                    else:
                        children.append(Token("spoiler_close", "tg-spoiler", -1))
                        open_index = None
                if part:
                    text = Token("text", "", 0)
                    text.content = part
                    children.append(text)
        if open_index is not None:
            literal = Token("text", "", 0)
            literal.content = "||"
            children[open_index] = literal
        block.children = children


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True}).enable(["strikethrough", "table"])
    md.core.ruler.after("inline", "tg_spoiler", _spoiler_rule)
    return md


_HTML_TAG = re.compile(r"^<\s*(/?)\s*([a-zA-Z][\w-]*)[^>]*?(/?)\s*>$")
_HTML_TYPES = {
    "u": "underline", "ins": "underline",
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic",
    "s": "strike", "del": "strike", "strike": "strike",
    "code": "code",
    "tg-spoiler": "spoiler", "spoiler": "spoiler",
}
_TG_USER_LINK = re.compile(r"^tg://user\?id=(\d+)$")


class _Builder:
    def __init__(self, keep_entities: bool) -> None:
        self.keep_entities = keep_entities
        self.parts: list[str] = []
        self.pos = 0
        self.entities: list[Entity] = []
        self.open_stack: list[tuple[str, int, dict]] = []
        self.pending_break = 0
        self.lists: list[bool] = []  # one entry per open list: is it ordered?
        self.links: list[str] = []
        self.table_cell = 0

    # ---- primitives --------------------------------------------------------------------

    def _flush_break(self) -> None:
        if self.pending_break and self.parts:
            self._emit("\n" * self.pending_break)
        self.pending_break = 0

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        self.pos += utf16_len(text)

    def text(self, text: str) -> None:
        if not text:
            return
        self._flush_break()
        self._emit(text)

    def block_break(self, count: int) -> None:
        self.pending_break = max(self.pending_break, count)

    def open(self, kind: str, **extra) -> None:
        self._flush_break()
        self.open_stack.append((kind, self.pos, extra))

    def close(self, kind: str) -> None:
        for index in range(len(self.open_stack) - 1, -1, -1):
            if self.open_stack[index][0] == kind:
                _, start, extra = self.open_stack.pop(index)
                if self.keep_entities and self.pos > start:
                    self.entities.append(Entity(kind, start, self.pos - start, **extra))
                return

    # ---- token walking -----------------------------------------------------------------

    def blocks(self, tokens: list[Token]) -> None:
        for token in tokens:
            handler = getattr(self, f"_b_{token.type}", None)
            if handler is not None:
                handler(token)

    def _b_paragraph_open(self, token: Token) -> None:
        del token

    def _b_paragraph_close(self, token: Token) -> None:
        # Paragraphs of tight list items are "hidden" and only need a line break.
        self.block_break(1 if token.hidden else 2)

    def _b_heading_open(self, token: Token) -> None:
        self.open("bold")

    def _b_heading_close(self, token: Token) -> None:
        self.close("bold")
        self.block_break(2)

    def _b_inline(self, token: Token) -> None:
        if self.table_cell:
            if self.table_cell > 1:
                self.text(" | ")
        self.inline(token.children or [])

    def _b_fence(self, token: Token) -> None:
        language = (token.info or "").strip().split(" ")[0] or None
        self._code_block(token.content, language)

    def _b_code_block(self, token: Token) -> None:
        self._code_block(token.content, None)

    def _code_block(self, content: str, language: str | None) -> None:
        self.open("pre", language=language)
        self.text(content.rstrip("\n") or " ")
        self.close("pre")
        self.block_break(2)

    def _b_hr(self, token: Token) -> None:
        self.text("——————")
        self.block_break(2)

    def _b_html_block(self, token: Token) -> None:
        self.text(token.content.rstrip("\n"))
        self.block_break(2)

    def _b_blockquote_open(self, token: Token) -> None:
        self.open("blockquote")

    def _b_blockquote_close(self, token: Token) -> None:
        self.close("blockquote")
        self.block_break(2)

    def _b_bullet_list_open(self, token: Token) -> None:
        self._list_open(ordered=False)

    def _b_ordered_list_open(self, token: Token) -> None:
        self._list_open(ordered=True)

    def _list_open(self, ordered: bool) -> None:
        if self.lists:
            self.block_break(1)
        self.lists.append(ordered)

    def _b_bullet_list_close(self, token: Token) -> None:
        self._list_close()

    def _b_ordered_list_close(self, token: Token) -> None:
        self._list_close()

    def _list_close(self) -> None:
        self.lists.pop()
        self.block_break(1 if self.lists else 2)

    def _b_list_item_open(self, token: Token) -> None:
        self.block_break(1)
        indent = "  " * (len(self.lists) - 1)
        marker = f"{token.info or '1'}{token.markup or '.'} " if self.lists[-1] else "• "
        self.text(indent + marker)

    def _b_list_item_close(self, token: Token) -> None:
        self.block_break(1)

    def _b_table_close(self, token: Token) -> None:
        self.block_break(2)

    def _b_tr_open(self, token: Token) -> None:
        self.block_break(1)
        self.table_cell = 0

    def _b_th_open(self, token: Token) -> None:
        self.table_cell += 1

    _b_td_open = _b_th_open

    def _b_tr_close(self, token: Token) -> None:
        self.table_cell = 0
        self.block_break(1)

    def inline(self, children: list[Token]) -> None:
        for child in children:
            kind = child.type
            if kind == "text":
                self.text(child.content)
            elif kind in ("softbreak", "hardbreak"):
                self.text("\n")
            elif kind == "code_inline":
                self.open("code")
                self.text(child.content)
                self.close("code")
            elif kind in ("strong_open", "em_open", "s_open", "spoiler_open"):
                self.open({"strong_open": "bold", "em_open": "italic", "s_open": "strike"}.get(kind, "spoiler"))
            elif kind in ("strong_close", "em_close", "s_close", "spoiler_close"):
                self.close({"strong_close": "bold", "em_close": "italic", "s_close": "strike"}.get(kind, "spoiler"))
            elif kind == "link_open":
                self._link_open(child)
            elif kind == "link_close" and self.links:
                self.close(self.links.pop())
            elif kind == "image":
                src = child.attrGet("src") or ""
                self.open("text_url", url=str(src))
                self.text(child.content or "image")
                self.close("text_url")
            elif kind == "html_inline":
                self._html_inline(child.content)

    def _link_open(self, token: Token) -> None:
        href = str(token.attrGet("href") or "")
        match = _TG_USER_LINK.match(href)
        if token.markup == "autolink" or not href:
            # Telegram links bare URLs by itself; "_" kinds are dropped from the result.
            kind, extra = "_autolink", {}
        elif match:
            kind, extra = "mention_name", {"user_id": int(match.group(1))}
        else:
            kind, extra = "text_url", {"url": href}
        self.links.append(kind)
        self.open(kind, **extra)

    def _html_inline(self, raw: str) -> None:
        match = _HTML_TAG.match(raw.strip())
        if match is None:
            self.text(raw)
            return
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if name == "br":
            self.text("\n")
        elif name in _HTML_TYPES:
            if closing:
                self.close(_HTML_TYPES[name])
            elif not self_closing:
                self.open(_HTML_TYPES[name])
        else:
            self.text(raw)

    def result(self) -> FormattedText:
        while self.open_stack:
            self.close(self.open_stack[-1][0])
        text = "".join(self.parts)
        entities = [e for e in self.entities if not e.type.startswith("_")]
        return _trim(text, entities)


def _trim(text: str, entities: list[Entity]) -> FormattedText:
    """Strip outer whitespace and pull inline entity bounds in from whitespace."""
    stripped_left = text.lstrip()
    shift = utf16_len(text) - utf16_len(stripped_left)
    text = stripped_left.rstrip()
    total = utf16_len(text)
    units = text.encode("utf-16-le")

    def is_space(index: int) -> bool:
        return units[index * 2 : index * 2 + 2].decode("utf-16-le", errors="ignore").isspace()

    result: list[Entity] = []
    for entity in entities:
        start = max(entity.offset - shift, 0)
        end = min(entity.end - shift, total)
        if entity.type in INLINE_TYPES:
            while start < end and is_space(start):
                start += 1
            while end > start and is_space(end - 1):
                end -= 1
        if end > start:
            result.append(entity.moved(start, end - start))
    result.sort(key=lambda e: (e.offset, -e.length))
    return FormattedText(text, tuple(result))


def render(markdown: str, keep_entities: bool = True) -> FormattedText:
    """Render markdown. With ``keep_entities=False`` the result is plain text."""
    builder = _Builder(keep_entities)
    builder.blocks(_parser().parse(markdown))
    return builder.result()
