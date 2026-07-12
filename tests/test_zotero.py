"""SPRINT-V2 A4: live Zotero/Mendeley citation fields."""

from __future__ import annotations

import json

from conftest import NS, document_root

from tex2word import convert_file

BIB = r"""
@article{e1905, author={Einstein, Albert}, title={Zur Elektrodynamik},
  journal={Ann. Phys.}, volume={17}, year={1905}}
@book{k1984, author={Knuth, Donald E.}, title={The TeXbook}, year={1984}}
"""

PREPRINT_BIB = r"""
@online{wangWellposedModelsMemristive2016,
  type = {xyzabc},
  title = {Well-Posed Models of Memristive Devices},
  author = {Wang, Tianshi and Roychowdhury, Jaijeet},
  date = {2016-05-15},
  eprint = {1605.04897},
  eprinttype = {arXiv},
  eprintclass = {cs},
  doi = {10.48550/arXiv.1605.04897},
  url = {http://arxiv.org/abs/1605.04897},
  urldate = {2025-12-12},
  abstract = {An abstract.},
  langid = {english},
  pubstate = {prepublished},
  annotation = {titleTranslation: 忆阻器件的适定模型}
}
"""

THESIS_BIB = r"""
@thesis{doeGenericThesis2024,
  author = {Doe, Jane},
  title = {A Generic BibLaTeX Thesis},
  type = {PhD thesis},
  institution = {Example University},
  location = {Taipei},
  date = {2024-06-15}
}
"""


def _convert(body: str, tmp_path, mode: str):
    (tmp_path / "z.bib").write_text(BIB, encoding="utf-8")
    tex = tmp_path / "z.tex"
    tex.write_text(
        rf"\begin{{document}}{body}\bibliographystyle{{plainnat}}"
        rf"\bibliography{{z}}\end{{document}}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex), citation_mode=mode)
    return result


def _instrs(docx):
    root = document_root(docx)
    return [t.text or "" for t in root.xpath("//w:instrText", namespaces=NS)]


def test_static_mode_has_no_zotero_fields(tmp_path):
    result = _convert(r"Text \citep{e1905}.", tmp_path, "static")
    assert not any("ZOTERO" in i for i in _instrs(result.docx))


def test_zotero_mode_emits_citation_field(tmp_path):
    result = _convert(r"Text \citep{e1905}.", tmp_path, "zotero")
    cits = [i for i in _instrs(result.docx) if "ZOTERO_ITEM CSL_CITATION" in i]
    assert len(cits) == 1


def test_zotero_citation_json_valid(tmp_path):
    result = _convert(r"\citep[p.~5]{e1905}.", tmp_path, "zotero")
    cit = next(i for i in _instrs(result.docx) if "ZOTERO_ITEM" in i)
    payload = json.loads(cit.split("CSL_CITATION ", 1)[1])
    item = payload["citationItems"][0]
    assert item["itemData"]["title"] == "Zur Elektrodynamik"
    assert item["itemData"]["type"] == "article-journal"
    assert item["locator"] == "p.5"
    assert payload["properties"]["formattedCitation"].startswith("(Einstein")


def test_zotero_bibliography_field(tmp_path):
    result = _convert(r"\citep{e1905}.", tmp_path, "zotero")
    assert any("ZOTERO_BIBL" in i and "CSL_BIBLIOGRAPHY" in i for i in _instrs(result.docx))


def test_zotero_bibliography_field_wraps_entries(tmp_path):
    # The reference list must sit *inside* the CSL_BIBLIOGRAPHY field (between
    # its fldChar separate and end), not as plain paragraphs after a closed,
    # empty field -- otherwise a Zotero refresh duplicates the list and Word's
    # "update field" has nothing to recompute in place.
    result = _convert(r"\citep{e1905} and \citet{k1984}.", tmp_path, "zotero")
    root = document_root(result.docx)
    # Walk the document order of the bibliography field markers and reference text.
    seq: list[str] = []
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag == "fldChar":
            seq.append("fld:" + el.get(f"{{{NS['w']}}}fldCharType"))
        elif tag == "instrText" and "ZOTERO_BIBL" in (el.text or ""):
            seq.append("bibl")
        elif tag == "t" and "Knuth" in (el.text or ""):
            seq.append("ref")
    # find the bibl instruction, its separate, the reference, then the end
    i = seq.index("bibl")
    after = seq[i:]
    assert "fld:separate" in after, after
    sep = after.index("fld:separate")
    end = after.index("fld:end")
    ref = after.index("ref")
    assert sep < ref < end, after  # reference is wrapped by separate..end


def test_zotero_cached_text_matches_static(tmp_path):
    # the field's cached result should be the same formatted text as static mode
    static = _convert(r"\citet{k1984}.", tmp_path, "static")
    zot = _convert(r"\citet{k1984}.", tmp_path, "zotero")
    static_text = " ".join(
        t.text or "" for t in document_root(static.docx).xpath("//w:t", namespaces=NS)
    )
    zot_text = " ".join(
        t.text or "" for t in document_root(zot.docx).xpath("//w:t", namespaces=NS)
    )
    assert "Knuth (1984)" in static_text
    assert "Knuth (1984)" in zot_text


def test_zotero_output_valid(tmp_path):
    from tex2word.validate import validate_docx

    result = _convert(r"\citep[see][p.~5]{e1905} and \citet{k1984}.", tmp_path, "zotero")
    assert validate_docx(result.docx) == []


def test_zotero_online_preprint_item_data(tmp_path):
    (tmp_path / "preprint.bib").write_text(PREPRINT_BIB, encoding="utf-8")
    tex = tmp_path / "preprint.tex"
    tex.write_text(
        r"\begin{document}\cite{wangWellposedModelsMemristive2016}"
        r"\bibliography{preprint}\end{document}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex), citation_mode="zotero")
    cit = next(i for i in _instrs(result.docx) if "ZOTERO_ITEM" in i)
    data = json.loads(cit.split("CSL_CITATION ", 1)[1])["citationItems"][0]["itemData"]

    assert data["type"] == "article"
    assert data["genre"] == "xyzabc"
    assert data["citation-key"] == "wangWellposedModelsMemristive2016"
    assert data["number"] == "arXiv:1605.04897"
    assert data["publisher"] == "arXiv"
    assert data["source"] == "arXiv.org"
    assert data["language"] == "en"
    assert data["issued"] == {"date-parts": [[2016, 5, 15]]}
    assert data["accessed"] == {"date-parts": [[2025, 12, 12]]}
    assert data["note"] == (
        "titleTranslation: 忆阻器件的适定模型\narXiv:1605.04897 [cs]"
    )


def test_zotero_online_preprint_without_biblatex_type_omits_genre(tmp_path):
    bib = PREPRINT_BIB.replace("  type = {xyzabc},\n", "")
    (tmp_path / "preprint.bib").write_text(bib, encoding="utf-8")
    tex = tmp_path / "preprint.tex"
    tex.write_text(
        r"\begin{document}\cite{wangWellposedModelsMemristive2016}"
        r"\bibliography{preprint}\end{document}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex), citation_mode="zotero")
    cit = next(i for i in _instrs(result.docx) if "ZOTERO_ITEM" in i)
    data = json.loads(cit.split("CSL_CITATION ", 1)[1])["citationItems"][0]["itemData"]

    assert "genre" not in data


def test_zotero_biblatex_thesis_item_data(tmp_path):
    (tmp_path / "thesis.bib").write_text(THESIS_BIB, encoding="utf-8")
    tex = tmp_path / "thesis.tex"
    tex.write_text(
        r"\begin{document}\cite{doeGenericThesis2024}"
        r"\bibliography{thesis}\end{document}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex), citation_mode="zotero")
    cit = next(i for i in _instrs(result.docx) if "ZOTERO_ITEM" in i)
    data = json.loads(cit.split("CSL_CITATION ", 1)[1])["citationItems"][0]["itemData"]

    assert data["type"] == "thesis"
    assert data["genre"] == "PhD thesis"
    assert data["publisher"] == "Example University"
    assert data["publisher-place"] == "Taipei"
    assert data["issued"] == {"date-parts": [[2024, 6, 15]]}
