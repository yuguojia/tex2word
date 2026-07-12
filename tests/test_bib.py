from __future__ import annotations

from conftest import NS, document_root

from tex2word import convert_file, convert_source
from tex2word.bib.bibtex import parse_bibtex
from tex2word.bib.render import build_bibliography, format_reference, style_family

BIB = r"""
@article{e1905,
  author = {Einstein, Albert},
  title = {Zur Elektrodynamik bewegter K{\"o}rper},
  journal = {Annalen der Physik},
  volume = {17}, number = {10}, pages = {891--921}, year = {1905},
  doi = {10.1002/andp.19053221004}
}
@book{k1984,
  author = {Knuth, Donald E. and Lamport, Leslie},
  title = {The {TeX}book}, publisher = {Addison-Wesley}, year = {1984}
}
"""

THESES = r"""
@thesis{generic2024,
  author = {Doe, Jane},
  title = {A Generic BibLaTeX Thesis},
  type = {PhD thesis},
  institution = {Example University},
  location = {Taipei},
  date = {2024-06-15}
}
@thesis{untyped2023,
  author = {Roe, Richard}, title = {An Untyped Thesis}, year = {2023}
}
@phdthesis{legacyphd,
  author = {Smith, Alice}, title = {A Legacy PhD Thesis}, year = {2022}
}
@mastersthesis{legacyma,
  author = {Jones, Bob}, title = {A Legacy Master's Thesis}, year = {2021}
}
"""


def test_parse_bibtex_fields():
    items = parse_bibtex(BIB)
    assert set(items) == {"e1905", "k1984"}
    e = items["e1905"]
    assert e.type == "article-journal"
    assert e.csl_fields["container-title"] == "Annalen der Physik"
    assert e.csl_fields["author"][0]["family"] == "Einstein"
    assert e.csl_fields["issued"]["date-parts"] == [[1905]]


def test_biblatex_thesis_type_and_fields():
    thesis = parse_bibtex(THESES)["generic2024"]
    assert thesis.type == "thesis"
    assert thesis.csl_fields["genre"] == "PhD thesis"
    assert thesis.csl_fields["publisher"] == "Example University"
    assert thesis.csl_fields["publisher-place"] == "Taipei"
    assert thesis.csl_fields["issued"] == {"date-parts": [[2024, 6, 15]]}


def test_biblatex_thesis_without_type_omits_genre():
    thesis = parse_bibtex(THESES)["untyped2023"]
    assert thesis.type == "thesis"
    assert "genre" not in thesis.csl_fields


def test_legacy_thesis_entry_types_still_map_to_thesis():
    items = parse_bibtex(THESES)
    assert items["legacyphd"].type == "thesis"
    assert items["legacyma"].type == "thesis"


def test_accent_decoding():
    items = parse_bibtex(BIB)
    assert "Körper" in format_reference(items["e1905"])


def test_two_authors_joined():
    items = parse_bibtex(BIB)
    ref = format_reference(items["k1984"])
    assert "Knuth" in ref and "Lamport" in ref and "&" in ref


def test_style_family_mapping():
    assert style_family("plain") == "numeric"
    assert style_family("plainnat") == "author-year"
    assert style_family("unknownstyle") == "numeric"


def test_build_bibliography_numeric_order():
    items = parse_bibtex(BIB)
    biblio = build_bibliography(items, ["k1984", "e1905"], "numeric")
    assert biblio.labels == {"k1984": "1", "e1905": "2"}


def test_build_bibliography_author_year_sorted():
    items = parse_bibtex(BIB)
    biblio = build_bibliography(items, ["k1984", "e1905"], "author-year")
    # Einstein sorts before Knuth regardless of citation order
    assert [e.id for e in biblio.entries] == ["e1905", "k1984"]


def test_end_to_end_bibliography(tmp_path):
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    tex = tmp_path / "p.tex"
    tex.write_text(
        r"\begin{document}Text~\cite{e1905} and~\cite{k1984}."
        r"\bibliographystyle{plain}\bibliography{refs}\end{document}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex))
    root = document_root(result.docx)
    texts = " ".join(t.text or "" for t in root.xpath("//w:t", namespaces=NS))
    assert "[1]" in texts and "[2]" in texts
    assert "References" in texts
    assert "Annalen der Physik" in texts
    # a bookmark per cited entry
    names = {e.get(f"{{{NS['w']}}}name") for e in root.xpath("//w:bookmarkStart", namespaces=NS)}
    assert "bib_e1905" in names


def test_unknown_cite_key_warns():
    result = convert_source(
        r"\begin{document}\cite{ghost}\bibliography{none}\end{document}", base_dir="/tmp"
    )
    assert any("ghost" in w.message or "none" in w.message for w in result.report.warnings)


def _cite_text(tex_body: str, style: str, tmp_path) -> str:
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    tex = tmp_path / "c.tex"
    tex.write_text(
        rf"\begin{{document}}{tex_body}"
        rf"\bibliographystyle{{{style}}}\bibliography{{refs}}\end{{document}}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex))
    root = document_root(result.docx)
    return " ".join(t.text or "" for t in root.xpath("//w:t", namespaces=NS))


def test_citation_locators_numeric(tmp_path):
    text = _cite_text(r"\citep[p.~5]{e1905} and \citep[see][ch.~2]{k1984}.", "plain", tmp_path)
    assert "[1, p" in text  # suffix locator
    assert "[see 2, ch" in text  # prefix + suffix


def test_citation_locators_author_year(tmp_path):
    text = _cite_text(r"\citep[p.~5]{e1905}.", "plainnat", tmp_path)
    assert "Einstein, 1905, p" in text


def test_citeauthor_and_citeyear(tmp_path):
    text = _cite_text(r"\citeauthor{e1905} in \citeyear{e1905}.", "plainnat", tmp_path)
    assert "Einstein" in text
    assert "1905" in text


def test_citet_author_year(tmp_path):
    text = _cite_text(r"\citet{e1905} showed.", "plainnat", tmp_path)
    assert "Einstein (1905)" in text


def test_cite_inside_table_cell_resolves(tmp_path):
    # A \cite that appears only inside a tabular cell must still be collected by
    # the citation pass -> numbered [1] and added to the reference list, not left
    # as the raw key. (Regression: _walk_cites skipped ir.Table.)
    body = (
        r"\begin{table}\caption{T}\begin{tabular}{ll}"
        r"A & \cite{e1905} \\ B & \cite{k1984} \\"
        r"\end{tabular}\end{table}"
    )
    text = _cite_text(body, "plain", tmp_path)
    assert "[1]" in text and "[2]" in text
    assert "e1905" not in text and "k1984" not in text  # raw keys must not leak
    assert "Einstein" in text  # reference list generated even without \bibliography body


def test_thebibliography():
    src = (
        r"\begin{document}See \cite{a}."
        r"\begin{thebibliography}{9}\bibitem{a} Some Author, Title, 2020."
        r"\end{thebibliography}\end{document}"
    )
    result = convert_source(src)
    root = document_root(result.docx)
    texts = " ".join(t.text or "" for t in root.xpath("//w:t", namespaces=NS))
    assert "[1]" in texts
    assert "Some Author" in texts
