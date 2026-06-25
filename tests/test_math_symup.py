"""unicode-math alphabet commands inside math: ``\\symup{d}``, ``\\symbf{F}`` etc.

``\\symup`` (and its siblings ``\\symrm``/``\\symbf``/``\\symbb``/...) used to
raise MathUnsupported, dropping the whole formula to raw ``\\[ … \\]`` text. It
is the unicode-math way of selecting an upright alphabet and is very common on
Greek letters, e.g. ``\\symup\\pi`` for an upright pi.
"""

from __future__ import annotations

from tex2word.mathml.latex_math import Frac, Group, Lit, Row, parse


def _only_lit(latex: str) -> Lit:
    """Parse ``latex`` that wraps a single Lit and return that Lit."""
    row = parse(latex)
    assert isinstance(row, Row) and len(row.items) == 1
    group = row.items[0]
    assert isinstance(group, Group)
    inner = group.row.items
    assert len(inner) == 1 and isinstance(inner[0], Lit)
    return inner[0]


def test_symup_makes_upright():
    lit = _only_lit(r"\symup{d}")
    assert lit.text == "d" and lit.upright and not lit.bold


def test_symup_on_greek_letter():
    # the most common real use: an upright pi
    lit = _only_lit(r"\symup\pi")
    assert lit.text == "π" and lit.upright


def test_symbf_is_bold():
    lit = _only_lit(r"\symbf{F}")
    assert lit.text == "F" and lit.bold


def test_symbfup_is_bold_upright():
    lit = _only_lit(r"\symbfup{v}")
    assert lit.text == "v" and lit.bold and lit.upright


def test_symbb_is_double_struck():
    lit = _only_lit(r"\symbb{R}")
    assert lit.script == "double-struck" and lit.upright


def test_symcal_is_script():
    lit = _only_lit(r"\symcal{L}")
    assert lit.script == "script"


def test_symfrak_is_fraktur():
    lit = _only_lit(r"\symfrak{g}")
    assert lit.script == "fraktur"


def test_symbfit_is_bold():
    lit = _only_lit(r"\symbfit{a}")
    assert lit.bold


def test_upgreek_lowercase_is_upright():
    # upgreek package: \uppi -> upright π
    row = parse(r"\uppi")
    assert isinstance(row, Row) and len(row.items) == 1
    lit = row.items[0]
    assert isinstance(lit, Lit) and lit.text == "π" and lit.upright


def test_upgreek_uppercase_is_upright():
    # \Up + lowercase name spells the uppercase letter: \Uppi -> upright Π
    row = parse(r"\Uppi")
    lit = row.items[0]
    assert isinstance(lit, Lit) and lit.text == "Π" and lit.upright


def test_uppi_and_symup_in_same_fraction():
    # the real regression: \frac{1}{2 \uppi \symup\pi} must not fall back to raw
    row = parse(r"\frac{1}{2 \uppi \symup\pi}")
    assert isinstance(row.items[0], Frac)
