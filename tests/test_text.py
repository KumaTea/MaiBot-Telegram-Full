import pytest

from tg_full.text.entities import Entity, units_to_str, utf16_len, utf16_units
from tg_full.text.latex import latex_to_plain
from tg_full.text.length import TextTooLongError, check_length, display_width
from tg_full.text.md_in import to_markdown
from tg_full.text.md_out import render


def spans(result):
    units = utf16_units(result.text)
    return [(e.type, units_to_str(units, e.offset, e.end)) for e in result.entities]


# ---- entities -> markdown ------------------------------------------------------------


def test_md_in_plain_text_untouched():
    assert to_markdown("a*b_c", []) == "a*b_c"


def test_md_in_nested_and_utf16_offsets():
    text = "😀 bold italic end"
    # "😀" is 2 UTF-16 units, so "bold italic" starts at 3.
    entities = [Entity("bold", 3, 11), Entity("italic", 8, 6)]
    assert to_markdown(text, entities) == "😀 **bold *italic*** end"


def test_md_in_whitespace_moves_outside_markers():
    assert to_markdown("say hello now", [Entity("bold", 3, 7)]) == "say **hello** now"


def test_md_in_links_code_pre_quote():
    text = "see docs and x=1\nprint(1)\nquoted"
    entities = [
        Entity("text_url", 4, 4, url="https://d"),
        Entity("code", 13, 3),
        Entity("pre", 17, 8, language="py"),
        Entity("blockquote", 26, 6),
    ]
    assert to_markdown(text, entities) == "see [docs](https://d) and `x=1`\n```py\nprint(1)\n```\n> quoted"


def test_md_in_hook_replaces_entity():
    result = to_markdown("hi @me", [Entity("mention", 3, 3)], hook=lambda e, inner: "<AT>")
    assert result == "hi <AT>"


# ---- markdown -> entities ------------------------------------------------------------


def test_md_out_inline_styles():
    result = render("A **b** *c* ~~d~~ `e` [f](http://x) <u>g</u> ||h||")
    assert result.text == "A b c d e f g h"
    assert spans(result) == [
        ("bold", "b"), ("italic", "c"), ("strike", "d"), ("code", "e"),
        ("text_url", "f"), ("underline", "g"), ("spoiler", "h"),
    ]


def test_md_out_blocks():
    result = render("# Title\n\n- one\n- two\n\n> quote\n\n```py\nx = 1\n```")
    assert result.text == "Title\n\n• one\n• two\n\nquote\n\nx = 1"
    assert ("bold", "Title") in spans(result)
    assert ("blockquote", "quote") in spans(result)
    pre = [e for e in result.entities if e.type == "pre"][0]
    assert pre.language == "py"


def test_md_out_keeps_line_breaks_and_utf16():
    result = render("第一行\n**😀粗体**")
    assert result.text == "第一行\n😀粗体"
    bold = result.entities[0]
    assert (bold.offset, bold.length) == (4, 4)  # 3 CJK + newline; emoji is 2 units


def test_md_out_mention_link():
    result = render("[@Alice](tg://user?id=1001) hi")
    assert result.entities[0].type == "mention_name"
    assert result.entities[0].user_id == 1001


def test_md_out_plain_mode_drops_entities():
    result = render("**b** and *i*", keep_entities=False)
    assert result.text == "b and i"
    assert result.entities == ()


def test_md_out_unmatched_spoiler_is_literal():
    assert render("a || b").text == "a || b"


# ---- LaTeX ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("$a^2 + b^2 = c^2$", "a² + b² = c²"),
        ("costs $5 and $10", "costs $5 and $10"),
        (r"$$\frac{a+b}{2} \geq \sqrt{ab}$$", "(a+b)/2 ≥ √ab"),
        (r"\(x_1\)", "x₁"),
        (r"$\alpha \to \infty$", "α → ∞"),
        ("`$x^2$` stays", "`$x^2$` stays"),
        (r"$90^\circ$", "90°"),
    ],
)
def test_latex_to_plain(source, expected):
    assert latex_to_plain(source) == expected


# ---- length --------------------------------------------------------------------------


def test_lengths():
    assert utf16_len("a😀中") == 4
    assert display_width("a😀中") == 1 + 3 + 2
    assert display_width("👍🏻") == 3  # skin tone modifier is free


def test_check_length():
    assert check_length("x" * 10, soft_limit=5) is not None
    assert check_length("x" * 10, soft_limit=0) is None
    with pytest.raises(TextTooLongError):
        check_length("x" * 4097, soft_limit=0)
