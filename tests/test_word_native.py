"""V4-17: reading Word-native content (foreign equations/tables + field
citations from Zotero / Mendeley / EndNote)."""

from __future__ import annotations

import base64
from xml.sax.saxutils import escape

from lxml import etree

from tex2word import convert_source, ir
from tex2word.frontend import docx_reader as R
from tex2word.frontend.docx_reader import read_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _field_para(instr: str, result: str) -> etree._Element:
    return etree.fromstring(
        f'<w:p xmlns:w="{W}"><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve">{escape(instr)}</w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:t>{result}</w:t></w:r>'
        f'<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'.encode()
    )


def _cites(para: etree._Element) -> list[ir.Cite]:
    return [i for i in R._Reader(ir.DocumentMeta())._inlines(para) if isinstance(i, ir.Cite)]


def _fld_data(xml: str) -> str:
    return base64.b64encode(xml.encode()).decode()


def _endnote_xml(key: str | None, rec_num: int) -> str:
    label = f"<label>{key}</label>" if key else ""
    return (
        f"<EndNote><Cite><RecNum>{rec_num}</RecNum><record>{label}</record>"
        "</Cite></EndNote>"
    )


def _data_cite_para(
    *, outer_xml: str | None, inner_xml: str | None, result: str = "1"
) -> etree._Element:
    outer_data = (
        f'<w:fldData>{_fld_data(outer_xml)}</w:fldData>' if outer_xml else ""
    )
    inner_data = (
        f'<w:fldData>{_fld_data(inner_xml)}</w:fldData>' if inner_xml else ""
    )
    return etree.fromstring(
        f'<w:p xmlns:w="{W}">'
        f'<w:r><w:fldChar w:fldCharType="begin">{outer_data}</w:fldChar></w:r>'
        '<w:r><w:instrText> ADDIN EN.CITE </w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="begin">{inner_data}</w:fldChar></w:r>'
        '<w:r><w:instrText> ADDIN EN.CITE.DATA </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:t>{result}</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'.encode()
    )


def _run_with_field_char(kind: str) -> etree._Element:
    run = etree.Element(f"{{{W}}}r")
    fld = etree.SubElement(run, f"{{{W}}}fldChar")
    fld.set(f"{{{W}}}fldCharType", kind)
    return run


def _run_with_instruction(instruction: str) -> etree._Element:
    run = etree.Element(f"{{{W}}}r")
    instr = etree.SubElement(run, f"{{{W}}}instrText")
    instr.text = instruction
    return run


def _hyperlink_field_around(cite_para: etree._Element) -> etree._Element:
    para = etree.Element(f"{{{W}}}p", nsmap={"w": W})
    para.append(_run_with_field_char("begin"))
    para.append(_run_with_instruction('HYPERLINK \\l "_ENREF_18"'))
    para.append(_run_with_field_char("separate"))
    for run in list(cite_para):
        para.append(run)
    para.append(_run_with_field_char("end"))
    return para


# -- field citations from the three big reference managers ------------------- #


def test_zotero_citation_field_to_cite():
    instr = ('ADDIN ZOTERO_ITEM CSL_CITATION {"citationItems":'
             '[{"uris":["http://zotero.org/users/1/items/ABCD1234"]}]}')
    cites = _cites(_field_para(instr, "[1]"))
    assert cites and cites[0].keys == ["ABCD1234"] and cites[0].rendered == "[1]"


def test_mendeley_citation_field_to_cite():
    instr = ('ADDIN CSL_CITATION {"citationItems":'
             '[{"itemData":{"id":"Smith2020","title":"On Things"}}]}')
    cites = _cites(_field_para(instr, "(Smith, 2020)"))
    assert cites and cites[0].keys == ["Smith2020"]


def test_endnote_citation_field_to_cite():
    instr = ("ADDIN EN.CITE <EndNote><Cite><record>"
             "<rec-number>42</rec-number></record></Cite></EndNote>")
    cites = _cites(_field_para(instr, "(Smith 2020)"))
    assert cites and cites[0].keys == ["RN42"]


def test_endnote_fld_data_prefers_outer_payload():
    para = _data_cite_para(
        outer_xml=_endnote_xml("outerKey", 1),
        inner_xml=_endnote_xml("innerKey", 2),
    )
    cites = _cites(para)
    assert len(cites) == 1
    assert cites[0].keys == ["outerKey"] and cites[0].rendered == "1"


def test_endnote_fld_data_falls_back_to_inner_and_record_number():
    para = _data_cite_para(
        outer_xml=None, inner_xml=_endnote_xml(None, 42), result="42"
    )
    cites = _cites(para)
    assert len(cites) == 1
    assert cites[0].keys == ["RN42"] and cites[0].rendered == "42"


def test_hyperlink_field_with_nested_endnote_is_a_citation():
    xml = _endnote_xml("linkedKey", 18)
    para = _hyperlink_field_around(
        _data_cite_para(outer_xml=xml, inner_xml=xml, result="18")
    )
    inlines = R._Reader(ir.DocumentMeta())._inlines(para)
    cites = [node for node in inlines if isinstance(node, ir.Cite)]
    assert len(cites) == 1 and cites[0].keys == ["linkedKey"]
    assert not any(isinstance(node, ir.Link) for node in inlines)


def test_word_hyperlink_wrapper_keeps_endnote_field_state():
    xml = _endnote_xml("wrappedKey", 7)
    para = _data_cite_para(outer_xml=xml, inner_xml=xml, result="7")
    runs = list(para)
    hyperlink = etree.Element(f"{{{W}}}hyperlink")
    for run in runs:
        hyperlink.append(run)
    para.append(hyperlink)
    cites = _cites(para)
    assert len(cites) == 1 and cites[0].keys == ["wrappedKey"]


def test_endnote_field_can_span_multiple_word_hyperlinks():
    xml = (
        "<EndNote><Cite><RecNum>38</RecNum><record><label>first</label></record></Cite>"
        "<Cite><RecNum>39</RecNum><record><label>second</label></record></Cite></EndNote>"
    )
    para = _field_para(f"ADDIN EN.CITE {xml}", "")
    # Replace the empty cached result with the same shape Word/EndNote uses for
    # individually linked numbers inside one multi-record citation field.
    separate = next(
        run for run in para
        if (fld := run.find(f"{{{W}}}fldChar")) is not None
        and fld.get(f"{{{W}}}fldCharType") == "separate"
    )
    end = para[-1]
    para.remove(end)
    for value in ("38", ",", "39"):
        run = etree.Element(f"{{{W}}}r")
        text = etree.SubElement(run, f"{{{W}}}t")
        text.text = value
        if value != ",":
            hyperlink = etree.Element(f"{{{W}}}hyperlink")
            hyperlink.append(run)
            para.append(hyperlink)
        else:
            para.append(run)
    para.append(end)
    assert separate.getparent() is para
    cites = _cites(para)
    assert len(cites) == 1
    assert cites[0].keys == ["first", "second"]
    assert cites[0].rendered == "38,39"


def test_multi_item_csl_citation():
    instr = ('ADDIN CSL_CITATION {"citationItems":'
             '[{"itemData":{"id":"a"}},{"itemData":{"id":"b"}}]}')
    cites = _cites(_field_para(instr, "(a; b)"))
    assert cites and cites[0].keys == ["a", "b"]


# -- foreign equations + tables already round-trip (lock-in) ----------------- #


def _foreign(src: str) -> ir.Document:
    return read_docx(convert_source(src, embed_manifest=False).docx)


def test_foreign_inline_equation_recovers_as_math():
    doc = _foreign(r"\begin{document}Energy $e=mc^2$ holds.\end{document}")
    maths = [i for b in doc.blocks if isinstance(b, ir.Paragraph)
             for i in b.inlines if isinstance(i, ir.Math)]
    latex = maths[0].latex.replace(" ", "") if maths else ""
    assert maths and latex.startswith("e=m") and "^{2}" in latex


def test_foreign_table_recovers():
    src = r"\begin{document}\begin{tabular}{ll}a & b \\ c & d \\\end{tabular}\end{document}"
    table = next(b for b in _foreign(src).blocks if isinstance(b, ir.Table))
    assert len(table.rows) == 2
