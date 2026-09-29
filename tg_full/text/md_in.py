"""Telegram text + entities -> markdown, for messages passed to MaiBot.

Only formatted ranges get markdown syntax; the rest of the text is left as typed (no
escaping), since the reader is an LLM rather than a markdown renderer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .entities import INLINE_TYPES, Entity, units_to_str, utf16_units

_WRAPPERS: dict[str, tuple[str, str]] = {
    "bold": ("**", "**"),
    "italic": ("*", "*"),
    "strike": ("~~", "~~"),
    "spoiler": ("||", "||"),
    "underline": ("<u>", "</u>"),
}

# Returns replacement markdown for an entity, or None to use the default rendering.
EntityHook = Callable[[Entity, str], "str | None"]


@dataclass
class _Node:
    entity: Entity
    children: list[_Node] = field(default_factory=list)


def _build_tree(entities: list[Entity]) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[_Node] = []
    for entity in sorted(entities, key=lambda e: (e.offset, -e.length)):
        if entity.length <= 0:
            continue
        node = _Node(entity)
        while stack and entity.offset >= stack[-1].entity.end:
            stack.pop()
        if stack and entity.end > stack[-1].entity.end:
            # Partial overlap is not allowed by Telegram; clip to the parent to stay well-formed.
            node.entity = entity.moved(entity.offset, stack[-1].entity.end - entity.offset)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


def _split_ws(text: str) -> tuple[str, str, str]:
    stripped = text.strip()
    if not stripped:
        return text, "", ""
    start = text.index(stripped)
    return text[:start], stripped, text[start + len(stripped) :]


class _Renderer:
    def __init__(self, text: str, hook: EntityHook | None) -> None:
        self.units = utf16_units(text)
        self.hook = hook

    def range(self, start: int, end: int, nodes: list[_Node]) -> str:
        out: list[str] = []
        pos = start
        for node in nodes:
            out.append(units_to_str(self.units, pos, node.entity.offset))
            rendered = self.node(node)
            if node.entity.type in ("pre", "blockquote"):
                # Block constructs must start and end on their own lines.
                preceding = "".join(out)
                if preceding and not preceding.endswith("\n"):
                    rendered = "\n" + rendered
                following = units_to_str(self.units, node.entity.end, node.entity.end + 1)
                if following and following != "\n":
                    rendered += "\n"
            out.append(rendered)
            pos = node.entity.end
        out.append(units_to_str(self.units, pos, end))
        return "".join(out)

    def node(self, node: _Node) -> str:
        entity = node.entity
        if entity.type in ("code", "pre"):
            inner = units_to_str(self.units, entity.offset, entity.end)
        else:
            inner = self.range(entity.offset, entity.end, node.children)
        if self.hook is not None:
            replaced = self.hook(entity, inner)
            if replaced is not None:
                return replaced
        return self.wrap(entity, inner)

    @staticmethod
    def wrap(entity: Entity, inner: str) -> str:
        kind = entity.type
        if kind == "pre":
            body = inner[:-1] if inner.endswith("\n") else inner
            return f"```{entity.language or ''}\n{body}\n```"
        if kind == "blockquote":
            return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
        if kind not in INLINE_TYPES:
            return inner
        lead, core, trail = _split_ws(inner)
        if not core:
            return inner
        if kind == "code":
            fence = "``" if "`" in core else "`"
            pad = " " if core.startswith("`") or core.endswith("`") else ""
            core = f"{fence}{pad}{core}{pad}{fence}"
        elif kind == "text_url" and entity.url:
            core = f"[{core.replace(']', chr(92) + ']')}]({entity.url})"
        elif kind == "mention_name" and entity.user_id is not None:
            core = f"[{core.replace(']', chr(92) + ']')}](tg://user?id={entity.user_id})"
        elif kind in _WRAPPERS:
            left, right = _WRAPPERS[kind]
            core = f"{left}{core}{right}"
        return f"{lead}{core}{trail}"


def to_markdown(text: str, entities: list[Entity] | None, hook: EntityHook | None = None) -> str:
    if not entities:
        return text
    renderer = _Renderer(text, hook)
    return renderer.range(0, len(renderer.units) // 2, _build_tree(list(entities)))
