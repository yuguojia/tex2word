"""Localisable caption / cross-reference wording (--caption-locale, \\texwordcaption).

The SEQ counter names stay English ("SEQ Figure"/"SEQ Table"); only the displayed
label word, the chapter/number separator, the delimiter and the cleveref prefixes
localise -- so a Chinese document reads 图1-1 while the live fields keep working.
"""

from __future__ import annotations

import io
import re
import zipfile

from tex2word import convert_source
from tex2word.backend.caption_config import CaptionConfig

_FIG = (
    r"\documentclass{article}\usepackage{cleveref}\begin{document}"
    r"\section{S}"
    r"\begin{figure}\caption{Arch}\label{f}\end{figure}"
    r"\begin{table}\caption{Params}\label{t}\begin{tabular}{c}x\\\end{tabular}\end{table}"
    r"see \cref{f} and \cref{t}.\end{document}"
)


def _visible_text(docx: bytes) -> str:
    """Concatenate the visible run text (``<w:t>``), excluding field codes."""
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


def _field_codes(docx: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml))


def test_english_default_unchanged():
    docx = convert_source(_FIG, number_by_section=True).docx
    assert "Figure " in _visible_text(docx) and ": " in _visible_text(docx)
    assert "SEQ Figure" in _field_codes(docx)  # field counter name stays English
    assert "图" not in _visible_text(docx)


def test_zh_locale_labels_and_separator():
    docx = convert_source(_FIG, number_by_section=True, caption_locale="zh-CN").docx
    text = _visible_text(docx)
    assert "图1-1" in text and "表1-1" in text  # label + chapter-"-"-number
    assert "1.1" not in text  # the "." separator is not used
    assert "Figure " not in text  # English label gone from the visible text
    # the live SEQ counter names are unchanged so \ref and numbering keep matching
    assert "SEQ Figure" in _field_codes(docx) and "SEQ Table" in _field_codes(docx)


def test_zh_full_width_delimiter():
    text = _visible_text(
        convert_source(_FIG, number_by_section=True, caption_locale="zh-CN").docx
    )
    assert "　" in text  # full-width space delimiter before the caption text


def test_auto_picks_chinese_from_cjk_font():
    src = (
        r"\documentclass{article}\usepackage{xeCJK}\setCJKmainfont{SimSun}"
        r"\begin{document}\section{S}"
        r"\begin{figure}\caption{C}\label{f}\end{figure}\end{document}"
    )
    text = _visible_text(convert_source(src, number_by_section=True).docx)  # auto
    assert "图" in text


def test_auto_picks_chinese_from_ctex_class():
    src = (
        r"\documentclass{ctexart}\begin{document}\section{S}"
        r"\begin{figure}\caption{C}\label{f}\end{figure}\end{document}"
    )
    text = _visible_text(convert_source(src, number_by_section=True).docx)  # auto
    assert "图1-1" in text


def test_texwordcaption_overrides():
    src = (
        r"\documentclass{article}"
        r"\texwordcaption{tablelabel}{Tab}"
        r"\texwordcaption{delim}{ - }"
        r"\begin{document}\section{S}"
        r"\begin{table}\caption{P}\label{t}\begin{tabular}{c}x\\\end{tabular}\end{table}"
        r"\end{document}"
    )
    text = _visible_text(
        convert_source(src, number_by_section=True, caption_locale="en").docx
    )
    assert "Tab " in text and "Table " not in text  # displayed label overridden
    assert " - " in text  # surrounding spaces preserved verbatim


def test_config_overrides_keep_other_fields():
    cfg = CaptionConfig.chinese().with_overrides({"sectionsep": "."})
    assert cfg.section_sep == "."
    assert cfg.label("Table") == "表"  # unrelated fields untouched
    assert cfg.delim == "　"
