"""Word field codes and bookmarks (SEQ / REF / PAGEREF).

These are the primitives ``python-docx`` cannot create and that the PRD
identifies as the universal failure point of existing tools. A complex field is
a sequence of runs: ``fldChar begin`` -> ``instrText`` (the field code) ->
``fldChar separate`` -> a cached result run -> ``fldChar end``. Word recomputes
the result on field-refresh, giving live numbering.
"""

from __future__ import annotations

import itertools

from lxml import etree

from .ooxml import el, preserve_space, sub, text_el

_Element = etree._Element
_bookmark_ids = itertools.count(1)


def bookmark_start(name: str) -> _Element:
    bid = next(_bookmark_ids)
    return el("w:bookmarkStart", **{"w:id": bid, "w:name": name})


def bookmark_end_for(start: _Element) -> _Element:
    bid = start.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id")
    return el("w:bookmarkEnd", **{"w:id": bid})


def reset_bookmark_ids() -> None:
    """Reset the bookmark-id counter (call per-document for deterministic ids)."""
    global _bookmark_ids
    _bookmark_ids = itertools.count(1)


def _instr_run(code: str) -> _Element:
    r = el("w:r")
    instr = sub(r, "w:instrText")
    preserve_space(instr)
    instr.text = code
    return r


def _fldchar(kind: str) -> _Element:
    r = el("w:r")
    sub(r, "w:fldChar", **{"w:fldCharType": kind})
    return r


def field(code: str, cached: str = "") -> list[_Element]:
    """Build a complex field as a list of runs.

    ``code`` is the field instruction (e.g. ``SEQ Equation \\* ARABIC``);
    ``cached`` is the placeholder result shown until Word refreshes fields.
    """
    runs = [_fldchar("begin"), _instr_run(code), _fldchar("separate")]
    result = el("w:r")
    t = text_el("w:t", cached or " ")
    preserve_space(t)
    result.append(t)
    runs.append(result)
    runs.append(_fldchar("end"))
    return runs


def field_begin(code: str) -> list[_Element]:
    """Runs that *open* a complex field: ``begin`` -> ``instrText`` -> ``separate``.

    Pair with :func:`field_end` to wrap a multi-paragraph field *result* (e.g. a
    bibliography) so Word/Zotero recomputes the whole span in place on refresh,
    rather than treating the content as ordinary text outside the field.
    """
    return [_fldchar("begin"), _instr_run(code), _fldchar("separate")]


def field_end() -> _Element:
    """The run that *closes* a complex field opened with :func:`field_begin`."""
    return _fldchar("end")


def index_entry(term: str) -> list[_Element]:
    """A hidden ``{ XE "term" }`` index-entry field (no visible result)."""
    return [_fldchar("begin"), _instr_run(f'XE "{term}"'), _fldchar("end")]


def seq_field(counter: str, cached: str = "") -> list[_Element]:
    return field(f"SEQ {counter} \\* ARABIC", cached)


def number_field(
    counter: str, by_section: bool = False, section_sep: str = "."
) -> list[_Element]:
    """Runs for a live number: flat ``SEQ`` or per-section ``N{sep}M``.

    With ``by_section`` the number is ``STYLEREF 1 \\s`` (the nearest numbered
    Heading 1) + ``section_sep`` + ``SEQ counter \\s 1`` (a counter that resets at
    each Heading 1) -- the standard Word "include chapter number" caption scheme.
    ``section_sep`` is ``.`` for English ("1.1") or e.g. ``-`` for Chinese ("1-1").
    """
    if not by_section:
        return seq_field(counter, "1")
    runs = field("STYLEREF 1 \\s", "1")
    sep = el("w:r")
    sep_t = text_el("w:t", section_sep)
    preserve_space(sep_t)
    sep.append(sep_t)
    runs.append(sep)
    runs += field(f"SEQ {counter} \\s 1 \\* ARABIC", "1")
    return runs


# --------------------------------------------------------------------------- #
# Math-zone fields
#
# A field embedded *inside* an equation (m:oMath) cannot use w:instrText -- the
# math content model only takes math runs (m:r). Word represents such a field
# with m:r runs that carry the fldChar / instruction (in m:t) / cached result,
# each marked m:nor so the field plumbing renders as upright normal text. This
# is what a numbered equation's "(SEQ Equation)" looks like once Word saves it.
# --------------------------------------------------------------------------- #


def _m_run(*, nor: bool = True) -> _Element:
    r = el("m:r")
    if nor:
        sub(sub(r, "m:rPr"), "m:nor")
    return r


def math_text_run(text: str, *, nor: bool = True) -> _Element:
    """A literal math run holding ``text`` (optionally as upright normal text)."""
    r = _m_run(nor=nor)
    t = sub(r, "m:t")
    t.text = text
    preserve_space(t)
    return r


def _m_fldchar(kind: str) -> _Element:
    r = _m_run(nor=True)
    sub(r, "w:fldChar", **{"w:fldCharType": kind})
    return r


def _m_result(cached: str) -> _Element:
    r = _m_run(nor=True)
    sub(sub(r, "w:rPr"), "w:noProof")
    t = sub(r, "m:t")
    t.text = cached
    preserve_space(t)
    return r


def math_field(code: str, cached: str = "") -> list[_Element]:
    """A complex field as math runs (the m:r analogue of :func:`field`)."""
    return [
        _m_fldchar("begin"),
        math_text_run(code, nor=True),  # the instruction lives in m:t in math
        _m_fldchar("separate"),
        _m_result(cached or " "),
        _m_fldchar("end"),
    ]


def math_number_field(
    counter: str, by_section: bool = False, section_sep: str = "."
) -> list[_Element]:
    """The math-zone counterpart of :func:`number_field` (SEQ / per-section)."""
    if not by_section:
        return math_field(f"SEQ {counter} \\* ARABIC", "1")
    runs = math_field("STYLEREF 1 \\s", "1")
    runs.append(math_text_run(section_sep, nor=True))
    runs += math_field(f"SEQ {counter} \\s 1 \\* ARABIC", "1")
    return runs


def ref_field(bookmark: str, cached: str = "", *, paragraph_number: bool = False) -> list[_Element]:
    # \h = hyperlink to the bookmark. \r inserts the paragraph (list) number of
    # the bookmark in relative context -- used for numbered-section references.
    switches = "\\r \\h" if paragraph_number else "\\h"
    return field(f"REF {bookmark} {switches}", cached)


def pageref_field(bookmark: str, cached: str = "") -> list[_Element]:
    return field(f"PAGEREF {bookmark} \\h", cached)
