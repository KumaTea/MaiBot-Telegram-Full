"""Convert LaTeX math in outgoing markdown to plain text (requirement R22.1).

Telegram cannot render LaTeX, so ``$a^2 + b^2 = c^2$`` becomes ``a² + b² = c²``. Where no
Unicode form exists the result falls back to readable ASCII (``a^(n+1)``, ``(a)/(b)``).
Code spans and fenced code blocks are left untouched.
"""

from __future__ import annotations

import re

_SUPERSCRIPT = dict(zip("0123456789+-=()niabcdefghjklmoprstuvwxyzABDEGHIJKLMNOPRTUVW",
                        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ", strict=True))
_SUBSCRIPT = dict(zip("0123456789+-=()aehijklmnoprstuvx",
                      "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ", strict=True))

_SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Operators and relations
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆", "circ": "∘",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡",
    "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "implies": "⇒", "mapsto": "↦", "uparrow": "↑",
    "downarrow": "↓",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬",
    "lnot": "¬", "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "oplus": "⊕", "otimes": "⊗",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "prime": "′", "hbar": "ℏ", "ell": "ℓ",
    "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "therefore": "∴", "because": "∵",
    "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "\\": "\n", "mid": "|", "vert": "|", "Vert": "‖", "backslash": "\\", "%": "%", "$": "$", "{": "{", "}": "}", "_": "_",
    "#": "#", "&": "&",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ", ":": " ", "!": "", " ": " ",
    "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "R": "ℝ", "C": "ℂ",
}
_BLACKBOARD = {"N": "ℕ", "Z": "ℤ", "Q": "ℚ", "R": "ℝ", "C": "ℂ", "P": "ℙ", "H": "ℍ"}
# Commands whose single argument is kept as-is.
_PASSTHROUGH = {"text", "textrm", "textbf", "textit", "mathrm", "mathbf", "mathit", "mathsf", "mathtt",
                "operatorname", "boldsymbol", "bm", "mbox", "displaystyle", "textstyle", "hat", "bar", "vec",
                "tilde", "overline", "underline", "dot", "ddot"}
_ACCENTS = {"hat": "̂", "bar": "̄", "vec": "⃗", "tilde": "̃", "dot": "̇",
            "ddot": "̈", "overline": "̅"}

_CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.S)
_DISPLAY = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
_INLINE_PAREN = re.compile(r"\\\((.+?)\\\)", re.S)
# Pandoc's rule: no space right inside the dollars, closing $ not followed by a digit.
_INLINE_DOLLAR = re.compile(r"(?<![\\$\w])\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)")


def _read_group(src: str, pos: int) -> tuple[str, int]:
    """Read a ``{...}`` group or a single token starting at ``pos``."""
    while pos < len(src) and src[pos] == " ":
        pos += 1
    if pos >= len(src):
        return "", pos
    if src[pos] == "{":
        depth, start = 0, pos
        while pos < len(src):
            if src[pos] == "{":
                depth += 1
            elif src[pos] == "}":
                depth -= 1
                if depth == 0:
                    return src[start + 1 : pos], pos + 1
            pos += 1
        return src[start + 1 :], len(src)
    if src[pos] == "\\":
        match = re.match(r"\\([A-Za-z]+|.)", src[pos:])
        if match:
            return match.group(0), pos + len(match.group(0))
    return src[pos], pos + 1


def _script(content: str, table: dict[str, str], marker: str) -> str:
    plain = _convert(content)
    if plain and all(ch in table for ch in plain):
        return "".join(table[ch] for ch in plain)
    return f"{marker}{plain}" if len(plain) == 1 else f"{marker}({plain})"


def _atom(text: str) -> str:
    return text if re.fullmatch(r"[\w.′]+|\S", text) else f"({text})"


def _convert(src: str) -> str:
    out: list[str] = []
    pos = 0
    while pos < len(src):
        ch = src[pos]
        if ch == "\\":
            match = re.match(r"\\([A-Za-z]+|.)", src[pos:])
            name = match.group(1) if match else ""
            pos += len(match.group(0)) if match else 1
            if name == "frac" or name == "dfrac" or name == "tfrac":
                num, pos = _read_group(src, pos)
                den, pos = _read_group(src, pos)
                out.append(f"{_atom(_convert(num))}/{_atom(_convert(den))}")
            elif name == "sqrt":
                index = ""
                if pos < len(src) and src[pos] == "[":
                    close = src.find("]", pos)
                    index, pos = src[pos + 1 : close], close + 1
                body, pos = _read_group(src, pos)
                root = {"": "√", "3": "∛", "4": "∜"}.get(index.strip(), _script(index, _SUPERSCRIPT, "^") + "√")
                out.append(root + _atom(_convert(body)))
            elif name == "mathbb":
                body, pos = _read_group(src, pos)
                out.append("".join(_BLACKBOARD.get(c, c) for c in _convert(body)))
            elif name in ("left", "right", "big", "Big", "bigg", "Bigg", "limits", "nolimits"):
                continue
            elif name in _ACCENTS:
                body, pos = _read_group(src, pos)
                plain = _convert(body)
                out.append(plain + _ACCENTS[name] if len(plain) == 1 else plain)
            elif name in _PASSTHROUGH:
                body, pos = _read_group(src, pos)
                out.append(_convert(body))
            elif name in _SYMBOLS:
                out.append(_SYMBOLS[name])
            else:
                out.append(name)  # \sin, \log, \max, unknown commands: keep the word
        elif ch in "^_":
            body, pos = _read_group(src, pos + 1)
            if ch == "^" and body in ("\\circ", "{\\circ}", "\\degree"):
                out.append("°")
            else:
                out.append(_script(body, _SUPERSCRIPT if ch == "^" else _SUBSCRIPT, ch))
        elif ch in "{}":
            pos += 1
        elif ch == "~":
            out.append(" ")
            pos += 1
        elif ch == "&":
            pos += 1
        else:
            out.append(ch)
            pos += 1
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def latex_to_plain(markdown: str) -> str:
    """Replace every math span in ``markdown`` with its plain-text rendering."""
    if "$" not in markdown and "\\(" not in markdown and "\\[" not in markdown:
        return markdown
    pieces = _CODE.split(markdown)
    for index in range(0, len(pieces), 2):  # odd indexes are code spans / blocks
        piece = pieces[index]
        piece = _DISPLAY.sub(lambda m: _convert(m.group(1) or m.group(2)), piece)
        piece = _INLINE_PAREN.sub(lambda m: _convert(m.group(1)), piece)
        piece = _INLINE_DOLLAR.sub(lambda m: _convert(m.group(1)), piece)
        pieces[index] = piece
    return "".join(pieces)
