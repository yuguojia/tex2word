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
from .rich_text import parse_rich_text

_Element = etree._Element

# EndNote reference names/numbers used by its XML format.  The numeric value is
# the interoperable part; the English name is deliberately locale-independent.
_TYPE_MAP: dict[str, tuple[str, int]] = {
    "bill": ("Bill", 4),
    "broadcast": ("Film or Broadcast", 21),
    "article-journal": ("Journal Article", 17),
    "article-magazine": ("Magazine Article", 19),
    "article-newspaper": ("Newspaper Article", 23),
    "article": ("Electronic Article", 43),
    "book": ("Book", 6),
    "chapter": ("Book Section", 5),
    "classic": ("Classical Work", 49),
    "dataset": ("Dataset", 59),
    "entry-dictionary": ("Dictionary", 52),
    "entry-encyclopedia": ("Encyclopedia", 53),
    "figure": ("Figure", 37),
    "graphic": ("Artwork", 2),
    "hearing": ("Hearing", 14),
    "interview": ("Personal Communication", 26),
    "paper-conference": ("Conference Proceedings", 10),
    "patent": ("Patent", 25),
    "periodical": ("Serial", 57),
    "post": ("Web Page", 12),
    "post-weblog": ("Blog", 56),
    "regulation": ("Legal Rule or Regulation", 50),
    "speech": ("Conference Paper", 47),
    "standard": ("Standard", 58),
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
    "musical_score": ("Music", 61),
    "song": ("Music", 61),
    "software": ("Computer Program", 9),
    "document": ("Generic", 13),
}

_ARTICLE_TYPES = frozenset(
    {"article", "article-journal", "article-magazine", "article-newspaper", "periodical"}
)
_PERIODICAL_TYPES = frozenset(
    {"article-journal", "article-magazine", "article-newspaper", "periodical"}
)

# Fields inside EndNote's <titles> container.  Most CSL container titles are
# meaningful as a secondary title, but books and conference papers need the
# type-specific distinctions below.
_DEFAULT_TITLE_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("title", "title"),
    ("container-title", "secondary-title"),
    ("collection-title", "tertiary-title"),
    ("title-short", "short-title"),
)
_TITLE_FIELD_MAP_BY_TYPE: dict[str, tuple[tuple[str, str], ...]] = {
    "book": (
        ("title", "title"),
        ("collection-title", "secondary-title"),
        ("container-title", "tertiary-title"),
        ("title-short", "short-title"),
    ),
    "collection": (
        ("title", "title"),
        ("collection-title", "secondary-title"),
        ("container-title", "tertiary-title"),
        ("title-short", "short-title"),
    ),
    "paper-conference": (
        ("title", "title"),
        ("event-title", "secondary-title"),
        ("collection-title", "tertiary-title"),
        ("title-short", "short-title"),
    ),
    "bill": (
        ("title", "title"),
        ("container-title", "secondary-title"),
        ("authority", "tertiary-title"),
        ("title-short", "short-title"),
    ),
    "hearing": (
        ("title", "title"),
        ("container-title", "secondary-title"),
        ("authority", "tertiary-title"),
        ("title-short", "short-title"),
    ),
}

# Direct children of <record>.  Entries that may converge on the same EndNote
# field are emitted by priority and only the first non-empty value wins.
_DEFAULT_FIELD_MAP: dict[str, str] = {
    "page": "pages",
    "number-of-pages": "pages",
    "volume": "volume",
    "number": "number",
    "issue": "number",
    "collection-number": "number",
    "number-of-volumes": "num-vols",
    "edition": "edition",
    "version": "edition",
    "section": "section",
    "chapter-number": "section",
    "publisher-place": "pub-location",
    "publisher": "publisher",
    "DOI": "electronic-resource-num",
    "abstract": "abstract",
    "note": "notes",
    "language": "language",
    "genre": "work-type",
    "event-place": "meeting-place",
    "dimensions": "size",
    "status": "reprint-status",
    "original-publisher": "orig-pub",
    "call-number": "call-num",
    "source": "remote-database-name",
}
_FIELD_OVERRIDES_BY_TYPE: dict[str, dict[str, str]] = {
    "bill": {"number": "misc1"},
    "book": {"collection-number": "misc1"},
    "legal_case": {"authority": "publisher"},
    "legislation": {"number": "misc1"},
    "paper-conference": {"container-title": "custom3"},
    "patent": {"number": "isbn"},
    "report": {"number": "isbn"},
    "chapter": {"number-of-volumes": "issue"},
}
_DEFAULT_FIELD_PRIORITY: tuple[str, ...] = tuple(_DEFAULT_FIELD_MAP)

_DEFAULT_CONTRIBUTOR_MAP: dict[str, str] = {
    "author": "authors",
    "editor": "secondary-authors",
    "container-author": "secondary-authors",
    "collection-editor": "tertiary-authors",
    "translator": "subsidiary-authors",
    "contributor": "subsidiary-authors",
}
_CONTRIBUTOR_OVERRIDES_BY_TYPE: dict[str, dict[str, str]] = {
    "book": {
        "editor": "tertiary-authors",
        "collection-editor": "secondary-authors",
    },
    "report": {"collection-editor": "secondary-authors"},
    "song": {"composer": "authors", "performer": "secondary-authors"},
    "motion_picture": {
        "director": "authors",
        "producer": "tertiary-authors",
        "executive-producer": "tertiary-authors",
        "performer": "subsidiary-authors",
        "cast-member": "subsidiary-authors",
    },
    "broadcast": {
        "director": "authors",
        "producer": "tertiary-authors",
        "executive-producer": "tertiary-authors",
        "performer": "subsidiary-authors",
        "cast-member": "subsidiary-authors",
        "host": "secondary-authors",
        "narrator": "secondary-authors",
    },
    "interview": {"interviewer": "secondary-authors"},
    "personal_communication": {"recipient": "secondary-authors"},
    "speech": {"chair": "secondary-authors", "organizer": "secondary-authors"},
}
_CONTRIBUTOR_GROUPS = (
    "authors",
    "secondary-authors",
    "tertiary-authors",
    "subsidiary-authors",
)

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


def _rich_child(parent: _Element, tag: str, value: object | None = None) -> _Element:
    """Add an EndNote XML field, translating CSL rich-text markup."""
    child = etree.SubElement(parent, tag)
    if value is None:
        return child
    spans = parse_rich_text(value)
    if len(spans) == 1 and not spans[0].faces:
        child.text = spans[0].text
        return child
    for span in spans:
        style = etree.SubElement(child, "style")
        style.set("face", " ".join(span.faces) or "normal")
        style.text = span.text
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
    role_map = dict(_DEFAULT_CONTRIBUTOR_MAP)
    role_map.update(_CONTRIBUTOR_OVERRIDES_BY_TYPE.get(item.type, {}))
    grouped_names: dict[str, list[str]] = {group: [] for group in _CONTRIBUTOR_GROUPS}

    for csl_field, endnote_group in role_map.items():
        people = item.csl_fields.get(csl_field) or []
        if not isinstance(people, list):
            people = [people]
        names = [_name(person) if isinstance(person, dict) else str(person) for person in people]
        grouped_names[endnote_group].extend(name for name in names if name)

    if not any(grouped_names.values()):
        return
    contributors = _child(record, "contributors")
    for endnote_group in _CONTRIBUTOR_GROUPS:
        names = grouped_names[endnote_group]
        if not names:
            continue
        group = _child(contributors, endnote_group)
        for name in names:
            _rich_child(group, "author", name)


def _titles(record: _Element, item: ir.CSLItem) -> None:
    data = item.csl_fields
    titles = _child(record, "titles")
    title_map = _TITLE_FIELD_MAP_BY_TYPE.get(item.type, _DEFAULT_TITLE_FIELD_MAP)
    emitted: set[str] = set()
    for csl_field, endnote_field in title_map:
        value = data.get(csl_field)
        if csl_field == "title":
            _rich_child(titles, endnote_field, value or "")
            emitted.add(endnote_field)
            continue
        if value in (None, "") or endnote_field in emitted:
            continue
        _rich_child(titles, endnote_field, value)
        emitted.add(endnote_field)

    if item.type in _PERIODICAL_TYPES and (
        data.get("container-title") or data.get("container-title-short")
    ):
        periodical = _child(record, "periodical")
        if data.get("container-title"):
            _rich_child(periodical, "full-title", data["container-title"])
        if data.get("container-title-short"):
            _rich_child(periodical, "abbr-1", data["container-title-short"])


def _field_map(item_type: str) -> dict[str, str]:
    mapping = dict(_DEFAULT_FIELD_MAP)
    mapping.update(_FIELD_OVERRIDES_BY_TYPE.get(item_type, {}))
    return mapping


def _field_priority(item_type: str, mapping: dict[str, str]) -> list[str]:
    priority = list(_DEFAULT_FIELD_PRIORITY)
    if item_type in _ARTICLE_TYPES:
        # CSL issue is the periodical issue number; a generic number is only a
        # fallback when issue is absent.
        number_fields = ["issue", "number", "collection-number"]
        priority = [field for field in priority if field not in number_fields]
        volume_index = priority.index("volume") + 1
        priority[volume_index:volume_index] = number_fields
    if item_type == "legal_case":
        # A CSL authority is the court and is a better EndNote publisher value
        # than a generic publisher when both are supplied.
        priority.remove("publisher")
        priority.insert(priority.index("publisher-place") + 1, "authority")
        priority.insert(priority.index("authority") + 1, "publisher")
    priority.extend(field for field in mapping if field not in priority)
    return priority


def _simple_fields(record: _Element, item: ir.CSLItem) -> set[str]:
    data = item.csl_fields
    mapping = _field_map(item.type)
    emitted: set[str] = set()
    for csl_field in _field_priority(item.type, mapping):
        value = data.get(csl_field)
        endnote_field = mapping[csl_field]
        if value in (None, "") or endnote_field in emitted:
            continue
        _rich_child(record, endnote_field, value)
        emitted.add(endnote_field)
    return emitted


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
    emitted = _simple_fields(record, item)

    identifier = data.get("ISBN") or data.get("ISSN")
    if identifier and "isbn" not in emitted:
        _rich_child(record, "isbn", identifier)
    _dates(record, item)
    if data.get("URL"):
        _child(_child(_child(record, "urls"), "web-urls"), "url", data["URL"])

    # The label is tex2word's lossless round-trip key.  RecNum is intentionally
    # document-local and is used when reading third-party fields without a label.
    _child(record, "label", key)
    return record


def _cite_element(
    key: str,
    item: ir.CSLItem,
    display_text: str,
    *,
    hidden: bool = False,
    author_year: bool = False,
) -> _Element:
    rec_num = _record_number(key)
    cite = etree.Element("Cite")
    if hidden:
        cite.set("Hidden", "1")
    if author_year:
        cite.set("AuthorYear", "1")
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
    keys = list(items) if "*" in cite.keys else cite.keys
    available = [(key, items[key]) for key in keys if key in items]
    for key, item in available:
        # For a single record this exactly matches the cached Word result.  In a
        # multi-record field EndNote regenerates the group from the embedded
        # records; a compact per-record display value avoids duplicating the
        # whole group in every <Cite>.
        display = rendered if len(available) == 1 else " ".join(
            part for part in (_first_author(item), _year(item)) if part
        )
        root.append(
            _cite_element(
                key,
                item,
                display,
                hidden=cite.hidden,
                author_year=cite.mode == "text",
            )
        )
    payload = etree.tostring(root, encoding="unicode", with_tail=False)
    return fields.field(f" ADDIN EN.CITE {payload}", rendered or " ")


def bibliography_field_begin() -> list[_Element]:
    """Open an EndNote reference-list field around the rendered entries."""
    return fields.field_begin(" ADDIN EN.REFLIST ")


def bibliography_field_end() -> _Element:
    """Close a reference-list field opened by :func:`bibliography_field_begin`."""
    return fields.field_end()
