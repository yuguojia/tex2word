"""Live EndNote ``EN.CITE`` Word fields.

Each field embeds the complete bibliographic record, so it remains editable by
EndNote without requiring a pre-existing EndNote library.  Record numbers are
local to a document: a citekey receives the next number on first use and keeps
that number for every later citation in the same document.
"""

from __future__ import annotations

from lxml import etree

from .. import ir
from ..backend import fields

_Element = etree._Element

# EndNote reference names/numbers used by its XML format.  The numeric value is
# the interoperable part; the English name is deliberately locale-independent.
_TYPE_MAP: dict[str, tuple[str, int]] = {
    "article-journal": ("Journal Article", 17),
    "article-magazine": ("Magazine Article", 19),
    "article-newspaper": ("Newspaper Article", 23),
    "article": ("Electronic Article", 43),
    "book": ("Book", 6),
    "chapter": ("Book Section", 5),
    "paper-conference": ("Conference Proceedings", 10),
    "thesis": ("Thesis", 32),
    "report": ("Report", 27),
    "webpage": ("Web Page", 12),
    "manuscript": ("Manuscript", 36),
    "pamphlet": ("Pamphlet", 24),
    "personal_communication": ("Personal Communication", 26),
    "legal_case": ("Case", 7),
    "legislation": ("Statute", 31),
    "map": ("Map", 20),
    "motion_picture": ("Film or Broadcast", 21),
    "song": ("Music", 61),
    "software": ("Computer Program", 9),
    "document": ("Generic", 13),
}

_record_numbers: dict[str, int] = {}


def reset_ids() -> None:
    """Reset the citekey-to-record-number table for a new document."""
    _record_numbers.clear()


def _record_number(key: str) -> int:
    number = _record_numbers.get(key)
    if number is None:
        number = len(_record_numbers) + 1
        _record_numbers[key] = number
    return number


def _child(parent: _Element, tag: str, value: object | None = None) -> _Element:
    child = etree.SubElement(parent, tag)
    if value is not None:
        child.text = str(value)
    return child


def _name(person: dict) -> str:
    """Render a CSL name in EndNote's ``family, given`` representation."""
    literal = str(person.get("literal") or "").strip()
    if literal:
        return literal
    family = str(person.get("family") or "").strip()
    given = str(person.get("given") or "").strip()
    suffix = str(person.get("suffix") or "").strip()
    if suffix:
        family = f"{family} {suffix}".strip()
    return f"{family}, {given}" if family and given else family or given


def _date_parts(item: ir.CSLItem) -> list[object]:
    issued = item.csl_fields.get("issued") or {}
    parts = issued.get("date-parts") if isinstance(issued, dict) else None
    if isinstance(parts, list) and parts and isinstance(parts[0], list):
        return parts[0]
    return []


def _year(item: ir.CSLItem) -> str:
    parts = _date_parts(item)
    return str(parts[0]) if parts else ""


def _first_author(item: ir.CSLItem) -> str:
    authors = item.csl_fields.get("author") or []
    if not authors:
        return ""
    person = authors[0]
    if not isinstance(person, dict):
        return str(person)
    return str(person.get("family") or person.get("literal") or person.get("given") or "")


def _contributors(record: _Element, item: ir.CSLItem) -> None:
    groups = (("author", "authors"), ("editor", "secondary-authors"))
    contributors: _Element | None = None
    for csl_field, endnote_field in groups:
        people = item.csl_fields.get(csl_field) or []
        names = [_name(p) if isinstance(p, dict) else str(p) for p in people]
        names = [name for name in names if name]
        if not names:
            continue
        if contributors is None:
            contributors = _child(record, "contributors")
        group = _child(contributors, endnote_field)
        for name in names:
            _child(group, "author", name)


def _titles(record: _Element, item: ir.CSLItem) -> None:
    data = item.csl_fields
    titles = _child(record, "titles")
    _child(titles, "title", data.get("title") or "")
    if data.get("container-title"):
        _child(titles, "secondary-title", data["container-title"])
    if data.get("collection-title"):
        _child(titles, "tertiary-title", data["collection-title"])
    if data.get("title-short"):
        _child(titles, "short-title", data["title-short"])

    if data.get("container-title") or data.get("container-title-short"):
        periodical = _child(record, "periodical")
        if data.get("container-title"):
            _child(periodical, "full-title", data["container-title"])
        if data.get("container-title-short"):
            _child(periodical, "abbr-1", data["container-title-short"])


def _dates(record: _Element, item: ir.CSLItem) -> None:
    parts = _date_parts(item)
    if not parts:
        return
    dates = _child(record, "dates")
    _child(dates, "year", parts[0])
    if len(parts) > 1:
        date = "-".join(
            [str(parts[0]), str(parts[1]).zfill(2)]
            + ([str(parts[2]).zfill(2)] if len(parts) > 2 else [])
        )
        _child(_child(dates, "pub-dates"), "date", date)


def _record(key: str, item: ir.CSLItem) -> _Element:
    data = item.csl_fields
    record = etree.Element("record")
    type_name, type_number = _TYPE_MAP.get(item.type, _TYPE_MAP["document"])
    ref_type = _child(record, "ref-type", type_number)
    ref_type.set("name", type_name)
    _contributors(record, item)
    _titles(record, item)

    simple_fields = (
        ("page", "pages"),
        ("volume", "volume"),
        ("issue", "number"),
        ("edition", "edition"),
        ("chapter-number", "section"),
        ("publisher-place", "pub-location"),
        ("publisher", "publisher"),
        ("DOI", "electronic-resource-num"),
        ("abstract", "abstract"),
        ("note", "notes"),
        ("language", "language"),
    )
    for csl_field, endnote_field in simple_fields:
        value = data.get(csl_field)
        if value not in (None, ""):
            _child(record, endnote_field, value)

    identifier = data.get("ISBN") or data.get("ISSN")
    if identifier:
        _child(record, "isbn", identifier)
    _dates(record, item)
    if data.get("URL"):
        _child(_child(_child(record, "urls"), "web-urls"), "url", data["URL"])

    # The label is tex2word's lossless round-trip key.  RecNum is intentionally
    # document-local and is used when reading third-party fields without a label.
    _child(record, "label", key)
    return record


def _cite_element(key: str, item: ir.CSLItem, display_text: str) -> _Element:
    rec_num = _record_number(key)
    cite = etree.Element("Cite")
    _child(cite, "Author", _first_author(item))
    _child(cite, "Year", _year(item))
    _child(cite, "RecNum", rec_num)
    _child(cite, "DisplayText", display_text)
    cite.append(_record(key, item))
    return cite


def citation_field(
    cite: ir.Cite, items: dict[str, ir.CSLItem], rendered: str
) -> list[_Element]:
    """Build one self-contained ``ADDIN EN.CITE`` complex Word field."""
    root = etree.Element("EndNote")
    available = [(key, items[key]) for key in cite.keys if key in items]
    for key, item in available:
        # For a single record this exactly matches the cached Word result.  In a
        # multi-record field EndNote regenerates the group from the embedded
        # records; a compact per-record display value avoids duplicating the
        # whole group in every <Cite>.
        display = rendered if len(available) == 1 else " ".join(
            part for part in (_first_author(item), _year(item)) if part
        )
        root.append(_cite_element(key, item, display))
    payload = etree.tostring(root, encoding="unicode", with_tail=False)
    return fields.field(f" ADDIN EN.CITE {payload}", rendered or " ")


def bibliography_field_begin() -> list[_Element]:
    """Open an EndNote reference-list field around the rendered entries."""
    return fields.field_begin(" ADDIN EN.REFLIST ")


def bibliography_field_end() -> _Element:
    """Close a reference-list field opened by :func:`bibliography_field_begin`."""
    return fields.field_end()
