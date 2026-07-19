"""Arbitrary native Word fields inserted with ``\\texwordfield``."""

from __future__ import annotations

from conftest import NS, document_root

from tex2word import convert_source, ir
from tex2word.backend.latex_writer import write_latex
from tex2word.frontend import parse_document
from tex2word.frontend.docx_reader import read_docx
from tex2word.frontend.macros import expand_macros


def _field(src: str) -> ir.WordField:
    doc, _ = parse_document(r"\begin{document}" + src + r"\end{document}")
    para = next(block for block in doc.blocks if isinstance(block, ir.Paragraph))
    return next(node for node in para.inlines if isinstance(node, ir.WordField))


def test_parses_custom_field_code_and_optional_cached_result():
    node = _field(r'Before \texwordfield[2026-07-19]{DATE \@ "yyyy-MM-dd"} after')
    assert node.code == r'DATE \@ "yyyy-MM-dd"'
    assert node.cached == "2026-07-19"


def test_custom_field_without_cached_result_defaults_to_empty():
    node = _field(r"\texwordfield{DOCPROPERTY Title}")
    assert node == ir.WordField("DOCPROPERTY Title", "")


def test_empty_compatibility_stub_does_not_expand_away_directive():
    source = (
        r"\providecommand{\texwordfield}[2][]{}"
        r"Value: \texwordfield[pending]{DOCPROPERTY Status}"
    )
    expanded = expand_macros(source)
    assert r"\providecommand" not in expanded
    assert r"\texwordfield[pending]{DOCPROPERTY Status}" in expanded


def test_emits_schema_shaped_complex_word_field():
    src = r'\begin{document}Date: \texwordfield[pending \& safe]{DATE \@ "yyyy"}.\end{document}'
    root = document_root(convert_source(src).docx)
    para = root.xpath("//w:p[.//w:instrText]", namespaces=NS)[0]
    sequence: list[str] = []
    for element in para:
        local = element.tag.rsplit("}", 1)[-1]
        if local != "r":
            continue
        fld = element.find(f"{{{NS['w']}}}fldChar")
        instr = element.find(f"{{{NS['w']}}}instrText")
        text = element.find(f"{{{NS['w']}}}t")
        if fld is not None:
            sequence.append("field:" + (fld.get(f"{{{NS['w']}}}fldCharType") or ""))
        elif instr is not None:
            sequence.append("instruction:" + (instr.text or ""))
        elif text is not None and text.text == "pending & safe":
            sequence.append("cached:" + text.text)

    start = sequence.index("field:begin")
    assert sequence[start : start + 5] == [
        "field:begin",
        r'instruction:DATE \@ "yyyy"',
        "field:separate",
        "cached:pending & safe",
        "field:end",
    ]


def test_foreign_docx_and_latex_writer_preserve_custom_field():
    src = r"\begin{document}\texwordfield[Draft]{DOCPROPERTY Status}\end{document}"
    doc = read_docx(convert_source(src, embed_manifest=False).docx)
    para = next(block for block in doc.blocks if isinstance(block, ir.Paragraph))
    node = next(inline for inline in para.inlines if isinstance(inline, ir.WordField))
    assert node == ir.WordField("DOCPROPERTY Status", "Draft")

    latex = write_latex(doc)
    assert r"\providecommand{\texwordfield}[2][]{}" in latex
    assert r"\texwordfield[{Draft}]{DOCPROPERTY Status}" in latex


def test_latex_writer_protects_specials_in_cached_result():
    original = ir.Document(
        [ir.Paragraph([ir.WordField("DOCPROPERTY Status", "A] & {B}")])]
    )
    latex = write_latex(original)
    reparsed, _ = parse_document(latex)
    para = next(block for block in reparsed.blocks if isinstance(block, ir.Paragraph))
    assert next(node for node in para.inlines if isinstance(node, ir.WordField)) == (
        ir.WordField("DOCPROPERTY Status", "A] & {B}")
    )
