from __future__ import annotations

import json

import pytest

from tex2word import convert_source, ir
from tex2word.bib.csl_json import parse_csl_json

CSL_ITEMS = [
    {
        "id": "smith2020",
        "type": "article-journal",
        "title": "An Example",
        "author": [{"family": "Smith", "given": "Jane"}],
        "issued": {"date-parts": [[2020]]},
        "custom": {"nested": [1, "two"]},
    }
]


def test_parse_csl_json_preserves_fields():
    items = parse_csl_json(json.dumps(CSL_ITEMS))

    assert list(items) == ["smith2020"]
    item = items["smith2020"]
    assert item.id == "smith2020"
    assert item.type == "article-journal"
    assert item.csl_fields == {
        key: value for key, value in CSL_ITEMS[0].items() if key not in {"id", "type"}
    }


@pytest.mark.parametrize(
    ("declaration", "bibliography_command"),
    [
        (r"\addbibresource{refs.json}", r"\printbibliography"),
        ("", r"\bibliography{refs.json}"),
    ],
)
def test_csl_json_bibliography_entry_points(
    tmp_path, declaration: str, bibliography_command: str
):
    (tmp_path / "refs.json").write_text(json.dumps(CSL_ITEMS), encoding="utf-8")
    source = (
        rf"\documentclass{{article}}{declaration}"
        rf"\begin{{document}}See \cite{{smith2020}}.{bibliography_command}\end{{document}}"
    )

    result = convert_source(source, base_dir=str(tmp_path), embed_manifest=False)
    bibliography = next(
        block for block in result.document.blocks if isinstance(block, ir.Bibliography)
    )

    assert [item.id for item in bibliography.entries] == ["smith2020"]
    assert bibliography.entries[0].csl_fields["title"] == "An Example"


@pytest.mark.parametrize(
    "source",
    [
        "{}",
        "[42]",
        '[{"type": "book"}]',
        '[{"id": "book"}]',
    ],
)
def test_parse_csl_json_rejects_nonstandard_items(source: str):
    with pytest.raises(ValueError):
        parse_csl_json(source)
