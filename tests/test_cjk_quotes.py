"""Directly-typed curly quotes get an East-Asian font hint (so Word renders them
with the CJK font) in a Chinese document; quotes from LaTeX commands (``\\`\\` ''``,
\\enquote) render curly but keep the default (Latin) font."""

from __future__ import annotations

import io
import re
import zipfile

from tex2word import convert_source, ir
from tex2word.frontend import parse_document

_HINT = '<w:rFonts w:hint="eastAsia"/>'


def _document(docx: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()


def _quote_runs(doc_xml: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"<w:r>.*?</w:r>", doc_xml)
            if any(q in m.group(0) for q in "“”‘’")]


CJK_PREAMBLE = r"\documentclass{article}\usepackage{xeCJK}\setCJKmainfont{SimSun}"


def test_typed_quote_tagged_in_parser():
    src = CJK_PREAMBLE + r"\begin{document}他说“你好”\end{document}"
    doc, _ = parse_document(src)
    para = next(b for b in doc.blocks if isinstance(b, ir.Paragraph))
    quotes = [n for n in para.inlines if isinstance(n, ir.Text) and n.value in "“”"]
    assert quotes and all(n.cjk_quote for n in quotes)


def test_latex_ligature_quotes_curly_and_untagged():
    # ``...'' renders as curly “...” but is a LaTeX command -> never CJK-tagged.
    src = CJK_PREAMBLE + r"\begin{document}``hello''\end{document}"
    doc, _ = parse_document(src)
    para = next(b for b in doc.blocks if isinstance(b, ir.Paragraph))
    texts = [n for n in para.inlines if isinstance(n, ir.Text)]
    assert any("“hello”" in n.value for n in texts)
    assert not any(n.cjk_quote for n in texts)


def test_latex_single_quote_syntax_curly_and_untagged():
    # `x' (LaTeX single-quote syntax, delivered as plain chars) -> ‘x’ but English.
    src = CJK_PREAMBLE + r"\begin{document}`x' don't\end{document}"
    doc, _ = parse_document(src)
    para = next(b for b in doc.blocks if isinstance(b, ir.Paragraph))
    texts = [n for n in para.inlines if isinstance(n, ir.Text)]
    joined = "".join(n.value for n in texts)
    assert "‘x’" in joined and "don’t" in joined  # ` -> ‘, ' -> ’
    assert "`" not in joined and "'" not in joined  # no ASCII quotes left
    assert not any(n.cjk_quote for n in texts)  # LaTeX syntax -> English font


def test_latex_single_quotes_stay_english_in_docx():
    src = CJK_PREAMBLE + r"\begin{document}`x'\end{document}"
    for run in _quote_runs(_document(convert_source(src).docx)):
        assert _HINT not in run


def test_enquote_quotes_not_tagged():
    src = CJK_PREAMBLE + r"\begin{document}\enquote{你好}\end{document}"
    doc, _ = parse_document(src)
    para = next(b for b in doc.blocks if isinstance(b, ir.Paragraph))
    texts = [n for n in para.inlines if isinstance(n, ir.Text)]
    assert any("“" in n.value for n in texts)  # the quotes are present
    assert not any(n.cjk_quote for n in texts)  # but none CJK-tagged


def test_typed_quotes_get_eastasia_font_hint():
    src = CJK_PREAMBLE + r"\begin{document}他说“你好”\end{document}"
    runs = _quote_runs(_document(convert_source(src).docx))
    assert runs and all(_HINT in r for r in runs)


def test_latex_command_quotes_stay_english():
    # \enquote and the ``...'' ligature form must not carry the East-Asian hint.
    src = CJK_PREAMBLE + r"\begin{document}\enquote{你好} 和 ``hello''\end{document}"
    doc_xml = _document(convert_source(src).docx)
    for run in _quote_runs(doc_xml):
        assert _HINT not in run


def test_no_font_hint_in_english_document():
    src = r"\documentclass{article}\begin{document}He said “hi”\end{document}"
    doc_xml = _document(convert_source(src, caption_locale="en").docx)
    assert _HINT not in doc_xml


def test_forced_chinese_locale_hints_typed_quotes():
    src = r"\documentclass{article}\begin{document}“你好”\end{document}"
    runs = _quote_runs(_document(convert_source(src, caption_locale="zh-CN").docx))
    assert runs and all(_HINT in r for r in runs)
