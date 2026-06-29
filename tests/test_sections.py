from __future__ import annotations

from conftest import NS, document_root

from tex2word import convert_source, ir
from tex2word.backend.numbering import HEADING_NUM_ID
from tex2word.frontend import parse_document
from tex2word.roundtrip import to_latex
from tex2word.validate import validate_docx


def _headings(src: str) -> list[ir.Heading]:
    doc, _ = parse_document(src)
    return [b for b in doc.blocks if isinstance(b, ir.Heading)]


def test_section_numbered_by_default():
    [h] = _headings(r"\begin{document}\section{Intro}\end{document}")
    assert h.numbered is True


def test_starred_section_unnumbered():
    [h] = _headings(r"\begin{document}\section*{Preface}\end{document}")
    assert h.numbered is False


def test_paragraph_level_unnumbered():
    [h] = _headings(r"\begin{document}\paragraph{Run in}\end{document}")
    assert h.level == 4
    assert h.numbered is False


def _heading_paras(docx):
    root = document_root(docx)
    out = []
    for p in root.xpath("//w:p", namespaces=NS):
        style = p.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
        if style and style[0].startswith("Heading"):
            numid = p.xpath(".//w:numPr/w:numId/@w:val", namespaces=NS)
            out.append((style[0], numid[0] if numid else None))
    return out


def test_backend_applies_heading_numbering():
    docx = convert_source(r"\begin{document}\section{A}\subsection{B}\end{document}").docx
    paras = _heading_paras(docx)
    assert ("Heading1", str(HEADING_NUM_ID)) in paras
    assert ("Heading2", str(HEADING_NUM_ID)) in paras


def test_backend_omits_numbering_for_starred():
    docx = convert_source(r"\begin{document}\section*{A}\end{document}").docx
    assert _heading_paras(docx) == [("Heading1", None)]


def test_section_ref_uses_paragraph_number_switch():
    src = r"\begin{document}\section{A}\label{sec:a}See \ref{sec:a}.\end{document}"
    root = document_root(convert_source(src).docx)
    instrs = "".join(t.text or "" for t in root.xpath("//w:instrText", namespaces=NS))
    assert "REF sec_a \\r \\h" in instrs


def test_numbering_part_defines_heading_scheme():
    from tex2word.backend.numbering import numbering_xml

    xml = numbering_xml().decode()
    assert 'w:numId="3"' in xml
    assert "%1.%2.%3" in xml  # three-level decimal scheme


def test_page_break_macros_become_blocks():
    doc, _ = parse_document(
        r"\begin{document}Before\newpage After\clearpage More\pagebreak[4] Done\end{document}"
    )
    assert [type(b) for b in doc.blocks] == [
        ir.Paragraph, ir.PageBreak, ir.Paragraph, ir.PageBreak, ir.Paragraph,
        ir.PageBreak, ir.Paragraph,
    ]
    assert [b.command for b in doc.blocks if isinstance(b, ir.PageBreak)] == [
        "newpage", "clearpage", "pagebreak",
    ]


def test_backend_emits_hard_page_breaks():
    src = r"\begin{document}Before\newpage After\clearpage More\pagebreak[4] Done\end{document}"
    docx = convert_source(src).docx
    root = document_root(docx)
    assert root.xpath("count(//w:br[@w:type='page'])", namespaces=NS) == 3
    assert validate_docx(docx) == []


def test_page_breaks_round_trip_from_manifest():
    src = r"\begin{document}Before\newpage After\clearpage Done\end{document}"
    latex = to_latex(convert_source(src).docx, reconcile=False)
    assert latex is not None
    assert r"\newpage" in latex and r"\clearpage" in latex
