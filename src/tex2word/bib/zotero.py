"""Live Zotero/Mendeley citation fields (PRD §D).

Emits ``ADDIN ZOTERO_ITEM CSL_CITATION {…CSL-JSON…}`` field codes (and a
matching ``CSL_BIBLIOGRAPHY`` field) so citations are live and editable by
reference managers in Word, rather than flattened text. The CSL-JSON is built
from the same ``.bib``→CSL-JSON parse used for the static renderer.
"""

from __future__ import annotations

import json
import zlib
from itertools import count

from lxml import etree

from .. import ir
from ..backend import fields

_Element = etree._Element
_CSL_SCHEMA = (
    "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
)
_citation_seq = count(1)


def reset_ids() -> None:
    global _citation_seq
    _citation_seq = count(1)


def _numeric_id(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF


def _item_data(item: ir.CSLItem) -> dict:
    data: dict = {"id": item.id, "type": item.type}
    data.update(item.csl_fields)
    return data


def _citation_item(cite: ir.Cite, key: str, item: ir.CSLItem) -> dict:
    out: dict = {
        "id": _numeric_id(key),
        "uris": [f"http://zotero.org/users/local/tex2word/items/{key}"],
        "itemData": _item_data(item),
    }
    if cite.suffix:
        out["locator"] = cite.suffix
    if cite.prefix:
        out["prefix"] = cite.prefix
    return out


def citation_field(
    cite: ir.Cite, items: dict[str, ir.CSLItem], rendered: str
) -> list[_Element]:
    """Build the runs for one ``ADDIN ZOTERO_ITEM CSL_CITATION`` field."""
    citation = {
        "citationID": f"l2w{next(_citation_seq)}",
        "properties": {"formattedCitation": rendered, "plainCitation": rendered, "noteIndex": 0},
        "citationItems": [
            _citation_item(cite, k, items[k]) for k in cite.keys if k in items
        ],
        "schema": _CSL_SCHEMA,
    }
    payload = json.dumps(citation, ensure_ascii=False, separators=(",", ":"))
    return fields.field(f"ADDIN ZOTERO_ITEM CSL_CITATION {payload}", rendered or " ")


_BIBL_CODE = ' ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY '


def bibliography_field_begin() -> list[_Element]:
    """Runs that *open* the ``CSL_BIBLIOGRAPHY`` field (begin/instr/separate).

    The formatted reference paragraphs must follow as the field *result* and the
    field must be closed with :func:`bibliography_field_end`. Emitting the list
    between ``separate`` and ``end`` is what lets Zotero refresh it in place — if
    the references sit *outside* the field, a refresh duplicates them (the field
    regenerates its own copy) and "update field" has nothing to recompute.
    """
    return fields.field_begin(_BIBL_CODE)


def bibliography_field_end() -> _Element:
    """The run that *closes* the ``CSL_BIBLIOGRAPHY`` field (``fldChar end``)."""
    return fields.field_end()
