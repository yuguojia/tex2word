from __future__ import annotations

import io
import re
import zipfile

from tex2word import convert_source, ir
from tex2word.backend.caption_config import CaptionConfig
from tex2word.frontend.parser import parse_document


def _visible_text(docx: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


def _field_codes(docx: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return "".join(re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml))


def test_declare_floating_environment_parses_custom_float():
    src = (
        r"\documentclass{article}\usepackage{newfloat}"
        r"\DeclareFloatingEnvironment[name={Scheme}]{scheme}"
        r"\begin{document}"
        r"\begin{scheme}\caption{A custom scheme}\label{sch:a}Body text.\end{scheme}"
        r"\end{document}"
    )
    doc, report = parse_document(src)
    flt = next(b for b in doc.blocks if isinstance(b, ir.Float))
    assert doc.meta.custom_floats == {"scheme": "Scheme"}
    assert flt.kind == "scheme"
    assert flt.counter == "Scheme"
    assert flt.label == "sch:a"
    assert flt.caption and flt.caption[0].value == "A custom scheme"
    assert not any(w.construct in {"scheme", "\\caption"} for w in report.warnings)


def test_texwordcaption_overrides_custom_float_label_and_seq():
    src = (
        r"\documentclass{article}\usepackage{newfloat,cleveref}"
        r"\DeclareFloatingEnvironment[name=Scheme]{scheme}"
        r"\texwordcaption{schemelabel}{Sch}"
        r"\texwordcaption{schemeseq}{SchSeq}"
        r"\begin{document}"
        r"\begin{scheme}Body text.\caption{A custom scheme}\label{sch:a}\end{scheme}"
        r"See \Cref{sch:a}."
        r"\end{document}"
    )
    res = convert_source(src, caption_locale="en")
    text = _visible_text(res.docx)
    fields = _field_codes(res.docx)
    assert "Sch " in text
    assert "A custom scheme" in text
    assert "SEQ SchSeq" in fields
    assert "REF sch_a" in fields
    assert res.document.labels["sch:a"].kind == "scheme"
    assert res.document.labels["sch:a"].counter_name == "Scheme"


def test_texwordcaption_custom_label_style_key():
    cfg = (
        CaptionConfig.english()
        .with_custom_kinds({"scheme": "Scheme"})
        .with_overrides({"schemelabelstyle": "Scheme Lead"})
    )
    assert cfg.label_style("Scheme") == "Scheme Lead"
