"""Generation of ``word/numbering.xml``.

For V1 Sprint 1 we ship a minimal-but-valid numbering part defining a bullet
list (abstractNumId 0) and a decimal list (abstractNumId 1), with concrete
``num`` instances 1 (bullet) and 2 (decimal). Sprint 3 extends this for nested
``itemize``/``enumerate``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from lxml import etree

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(name: str) -> str:
    return f"{{{_W}}}{name}"


_NUMBERING_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#9702;"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="1440" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:hint="default"/></w:rPr>
    </w:lvl>
    <w:lvl w:ilvl="2">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#9642;"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="2160" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Wingdings" w:hAnsi="Wingdings" w:hint="default"/></w:rPr>
    </w:lvl>
    <w:lvl w:ilvl="3">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="2880" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>
    </w:lvl>
    <w:lvl w:ilvl="4">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#9702;"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="3600" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:hint="default"/></w:rPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%2)"/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="1440" w:hanging="360"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="2">
      <w:start w:val="1"/><w:numFmt w:val="lowerRoman"/><w:lvlText w:val="%3."/>
      <w:lvlJc w:val="right"/><w:pPr><w:ind w:left="2160" w:hanging="180"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="3">
      <w:start w:val="1"/><w:numFmt w:val="upperLetter"/><w:lvlText w:val="%4."/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="2880" w:hanging="360"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="4">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%5."/>
      <w:lvlJc w:val="left"/><w:pPr><w:ind w:left="3600" w:hanging="360"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="2">
    <w:multiLevelType w:val="multilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading1"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading2"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="2">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading3"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="3">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3.%4"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading4"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="3">
    <w:multiLevelType w:val="multilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="upperLetter"/><w:lvlText w:val="%1"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading1"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading2"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="2">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading3"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
    <w:lvl w:ilvl="3">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3.%4"/>
      <w:lvlJc w:val="left"/><w:pStyle w:val="Heading4"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="4">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="upperRoman"/><w:lvlText w:val="Part %1"/>
      <w:lvlJc w:val="left"/><w:suff w:val="space"/>
      <w:pPr><w:ind w:left="0" w:hanging="0"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>
  <w:num w:numId="4"><w:abstractNumId w:val="3"/></w:num>
  <w:num w:numId="5"><w:abstractNumId w:val="4"/></w:num>
</w:numbering>
"""

#: numId for bullet (itemize) and decimal (enumerate) lists.
BULLET_NUM_ID = 1
DECIMAL_NUM_ID = 2
#: numId for the multilevel heading (section) numbering scheme.
HEADING_NUM_ID = 3
#: numId for appendix headings (top level lettered A, B, ...; then A.1, A.1.1).
HEADING_APPENDIX_NUM_ID = 4
#: numId for \part headings ("Part I", upper-roman, counter independent of sections).
PART_NUM_ID = 5

#: floor for the body's role numIds when a reference template is used. The body
#: must not reuse a numId the template already defines (its numbering.xml is
#: carried verbatim), so we lift our five role ids clear of the template's range.
#: A high floor keeps them predictable while still clearing any realistic template.
_REFERENCE_NUM_ID_BASE = 1000


@dataclass(frozen=True)
class NumIds:
    """The numIds the body writer stamps onto each kind of auto-numbered block.

    Defaults to our bundled scheme (1-5, matched by :func:`numbering_xml`). With a
    ``--reference-doc`` the template's numbering.xml is carried verbatim, so
    :func:`reference_num_ids` shifts these clear of the template's own numIds --
    leaving the template's lists (and the styles that reference them) untouched.
    """

    bullet: int = BULLET_NUM_ID
    decimal: int = DECIMAL_NUM_ID
    heading: int = HEADING_NUM_ID
    appendix: int = HEADING_APPENDIX_NUM_ID
    part: int = PART_NUM_ID


def reference_num_ids(ref_numbering: bytes) -> NumIds:
    """Allocate the five role numIds in a range clear of *ref_numbering*'s own.

    Returns ids starting just above both :data:`_REFERENCE_NUM_ID_BASE` and the
    template's highest numId, so :func:`reference_numbering` can keep the
    template's ``w:num`` entries verbatim while adding ours without collision.
    Falls back to the bundled :class:`NumIds` (1-5) if the numbering is unreadable.
    """
    try:
        root = etree.fromstring(ref_numbering)
    except Exception:
        return NumIds()
    ids = [
        int(v) for v in (n.get(_w("numId")) for n in root.findall(_w("num")))
        if v and v.lstrip("-").isdigit()
    ]
    base = max([_REFERENCE_NUM_ID_BASE - 1, *ids]) + 1
    return NumIds(base, base + 1, base + 2, base + 3, base + 4)


def numbering_xml() -> bytes:
    return _NUMBERING_XML.encode("utf-8")


# numFmt values that count (an *ordered* list), as opposed to a bullet.
_ORDERED_FMTS = {
    "decimal", "decimalzero", "lowerletter", "upperletter", "lowerroman",
    "upperroman", "ordinal", "ordinaltext", "cardinaltext", "chinesecounting",
    "chinesecountingthousand", "ideographdigital", "japanesecounting",
    "taiwanesecounting", "koreancounting", "koreandigital", "arabicabjad",
}
#: the heading styleIds our writer emits, which a template's heading-linked
#: multilevel list points its levels at via ``w:pStyle``.
_HEADING_IDS = {"Heading1", "Heading2", "Heading3", "Heading4", "Heading5"}


def reference_numbering(
    ref_numbering: bytes,
    heading_rename: dict[str, str],
    num_ids: NumIds,
    *,
    appendix_ids: list[str | None] | None = None,
    part_id: str | None = None,
) -> bytes | None:
    """Carry a reference template's numbering verbatim, plus our role numIds.

    With a ``--reference-doc`` we keep the template's ``w:abstractNum`` *and*
    ``w:num`` definitions unchanged -- so every carried style that references one
    (heading styles' "第%1章", bullet/list styles, ...) keeps working. On top of
    that we add five ``w:num`` entries under *num_ids* (allocated by
    :func:`reference_num_ids` to clear the template's range), pointing our body's
    bullet / decimal / heading / appendix / part roles at the template lists we
    detect -- so itemize, enumerate and section numbering adopt the template look.

    We detect the template's bullet list, ordered list and the heading-linked
    multilevel list (and, via *appendix_ids* / *part_id*, the lists linked to the
    ``\\texwordstyle``-bound appendix / part styles). Any role the template does
    not define falls back to our bundled :func:`numbering_xml` definition.

    The template's heading-linked levels reference its own heading styleIds via
    ``w:pStyle``; those are rewritten through *heading_rename* so the link
    survives the styleId normalization the styles merge applies. (Appendix/part
    styleIds are not built-in, so they need no such rewrite.)

    Returns ``None`` if the numbering is unreadable (caller keeps ours).
    """
    try:
        root = etree.fromstring(ref_numbering)
    except Exception:
        return None
    _rewrite_pstyle(root, heading_rename)

    role_anum = _detect_role_anums(root, appendix_ids, part_id)

    # numIdMacAtCleanup must trail every w:num; drop it so the role w:num we append
    # stay valid (it is an optional Mac-compat hint, safe to omit).
    for stale in root.findall(_w("numIdMacAtCleanup")):
        root.remove(stale)

    # w:abstractNum must precede every w:num; new fallback abstractNums go before
    # the template's first w:num, the role w:num we add trail all existing ones.
    first_num = next(
        (c for c in root if isinstance(c.tag, str) and etree.QName(c).localname == "num"),
        None,
    )

    existing = {a.get(_w("abstractNumId")) for a in root.findall(_w("abstractNum"))}
    next_id = max((int(i) for i in existing if i and i.lstrip("-").isdigit()), default=-1) + 1

    # our bundled abstractNumId for each role, read from our own num->abstractNum map.
    our_root = etree.fromstring(numbering_xml())
    our_fallback = {
        num.get(_w("numId")): num.find(_w("abstractNumId")).get(_w("val"))
        for num in our_root.findall(_w("num"))
    }
    our_anums = {a.get(_w("abstractNumId")): a for a in our_root.findall(_w("abstractNum"))}

    def fallback(default_num_id: int) -> str:
        """Carry our bundled abstractNum for *default_num_id* under a fresh id."""
        nonlocal next_id
        new_aid = str(next_id)
        next_id += 1
        anum = deepcopy(our_anums[our_fallback[str(default_num_id)]])
        anum.set(_w("abstractNumId"), new_aid)
        if first_num is not None:
            first_num.addprevious(anum)  # keep all abstractNum ahead of every num
        else:
            root.append(anum)
        return new_aid

    roles = [
        (num_ids.bullet, role_anum["bullet"] or fallback(BULLET_NUM_ID)),
        (num_ids.decimal, role_anum["decimal"] or fallback(DECIMAL_NUM_ID)),
        (num_ids.heading, role_anum["heading"] or fallback(HEADING_NUM_ID)),
        (num_ids.appendix, role_anum["appendix"] or fallback(HEADING_APPENDIX_NUM_ID)),
        (num_ids.part, role_anum["part"] or fallback(PART_NUM_ID)),
    ]
    for num_id, aid in roles:
        num = etree.SubElement(root, _w("num"))
        num.set(_w("numId"), str(num_id))
        etree.SubElement(num, _w("abstractNumId")).set(_w("val"), aid)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _rewrite_pstyle(root: etree._Element, heading_rename: dict[str, str]) -> None:
    """Rewrite ``w:pStyle`` links through *heading_rename* (template id -> our id).

    The template's heading-linked numbering levels reference its own heading
    styleIds; this keeps the link valid after the styles merge normalizes those
    ids to the ones our writer emits (``Heading1``...).
    """
    for ps in root.iter(_w("pStyle")):
        val = ps.get(_w("val"))
        if val in heading_rename:
            ps.set(_w("val"), heading_rename[val])


def _detect_role_anums(
    root: etree._Element,
    appendix_ids: list[str | None] | None,
    part_id: str | None,
) -> dict[str, str | None]:
    """{role name -> the template abstractNumId we adopt for that role}.

    Roles are ``"bullet"``, ``"decimal"``, ``"heading"``, ``"appendix"`` and
    ``"part"``. ``w:pStyle`` links must already be rewritten (see
    :func:`_rewrite_pstyle`). A role with no match in the template maps to None.
    """
    appendix_ids = appendix_ids or []
    anums = root.findall(_w("abstractNum"))
    heading_id = _detect_linked_anum(anums, _HEADING_IDS, prefer="Heading1")
    appendix_id = _detect_linked_anum(
        anums, [i for i in appendix_ids if i], prefer=next((i for i in appendix_ids if i), None)
    )
    part_anum = _detect_linked_anum(anums, [part_id] if part_id else [])
    used = {heading_id, appendix_id, part_anum}
    bullet_id = _detect_list_anum(anums, used, want_bullet=True)
    ordered_id = _detect_list_anum(anums, used, want_bullet=False)
    return {
        "bullet": bullet_id,
        "decimal": ordered_id,
        "heading": heading_id,
        "appendix": appendix_id,
        "part": part_anum,
    }


def _detect_linked_anum(
    anums: list[etree._Element], target_ids, prefer: str | None = None
) -> str | None:
    """abstractNumId of the list whose levels link (via ``w:pStyle``) to *target_ids*.

    Used to find the multilevel list a template attaches to a set of styles --
    the heading styles, or the ``\\texwordstyle``-bound appendix / part styles.
    Prefer the candidate whose ilvl-0 links to *prefer*; among the rest, the one
    linking the most of the target styles. Returns ``None`` if none qualifies.
    """
    target = {t for t in target_ids if t}
    if not target:
        return None
    best: str | None = None
    best_score = 0
    for a in anums:
        score = 0
        lvl0_pref = False
        for lvl in a.findall(_w("lvl")):
            ps = lvl.find(_w("pStyle"))
            val = ps.get(_w("val")) if ps is not None else None
            if val in target:
                score += 1
                if prefer and lvl.get(_w("ilvl")) == "0" and val == prefer:
                    lvl0_pref = True
        if score == 0:
            continue
        eff = score + (1000 if lvl0_pref else 0)
        if eff > best_score:
            best_score, best = eff, a.get(_w("abstractNumId"))
    return best


def _detect_list_anum(
    anums: list[etree._Element], exclude_ids, *, want_bullet: bool
) -> str | None:
    """abstractNumId of the first bullet (or ordered) list, excluding *exclude_ids*."""
    exclude = {i for i in exclude_ids if i}
    for a in anums:
        aid = a.get(_w("abstractNumId"))
        if aid in exclude:
            continue
        lvl0 = next((l for l in a.findall(_w("lvl")) if l.get(_w("ilvl")) == "0"), None)
        if lvl0 is None:
            continue
        fmt_el = lvl0.find(_w("numFmt"))
        fmt = (fmt_el.get(_w("val")) if fmt_el is not None else "").lower()
        if want_bullet and fmt == "bullet":
            return aid
        if not want_bullet and fmt in _ORDERED_FMTS:
            return aid
    return None
