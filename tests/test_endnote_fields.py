"""Live EndNote EN.CITE citation fields."""

from __future__ import annotations

from conftest import NS, document_root

from tex2word import convert_file, ir
from tex2word.frontend.docx_reader import read_docx

BIB = r"""
@article{cho2015,
  author={Cho, HC and Somu, S and Lee, JY},
  title={High-rate nanoscale offset printing process},
  journal={Advanced Materials}, number={10}, volume={27}, pages={1759--1766},
  year={2015}, doi={10.1002/adma.201404769}, issn={0935-9648}}
@book{knuth1984, author={Knuth, Donald E.}, title={The TeXbook},
  publisher={Addison-Wesley}, year={1984}}
"""


def _convert(body: str, tmp_path, *, manifest: bool = True):
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    tex = tmp_path / "paper.tex"
    tex.write_text(
        rf"\begin{{document}}{body}\bibliographystyle{{plainnat}}"
        rf"\bibliography{{refs}}\end{{document}}",
        encoding="utf-8",
    )
    _, result = convert_file(
        str(tex), citation_mode="endnote", embed_manifest=manifest
    )
    return result


def _endnote_instrs(docx: bytes) -> list[str]:
    root = document_root(docx)
    return [
        t.text or ""
        for t in root.xpath("//w:instrText", namespaces=NS)
        if "EN.CITE" in (t.text or "")
    ]


def _instrs(docx: bytes) -> list[str]:
    root = document_root(docx)
    return [t.text or "" for t in root.xpath("//w:instrText", namespaces=NS)]


def test_endnote_mode_embeds_record_and_label(tmp_path):
    result = _convert(r"Text \citep{cho2015}.", tmp_path)
    instr = _endnote_instrs(result.docx)[0]
    assert instr.startswith(" ADDIN EN.CITE <EndNote><Cite>")
    assert "<Author>Cho</Author><Year>2015</Year><RecNum>1</RecNum>" in instr
    assert '<ref-type name="Journal Article">17</ref-type>' in instr
    assert "<label>cho2015</label>" in instr
    assert "<electronic-resource-num>10.1002/adma.201404769" in instr
    assert "<rec-number>" not in instr and "<foreign-keys>" not in instr


def test_endnote_record_numbers_are_per_item_and_reused(tmp_path):
    result = _convert(
        r"\citep{cho2015}; \citep{knuth1984}; \citep{cho2015}.", tmp_path
    )
    instrs = _endnote_instrs(result.docx)
    assert len(instrs) == 3
    assert "<RecNum>1</RecNum>" in instrs[0]
    assert "<RecNum>2</RecNum>" in instrs[1]
    assert "<RecNum>1</RecNum>" in instrs[2]


def test_endnote_multi_cite_contains_each_record(tmp_path):
    result = _convert(r"\citep{cho2015,knuth1984}.", tmp_path)
    instr = _endnote_instrs(result.docx)[0]
    assert instr.count("<Cite>") == 2
    assert "<label>cho2015</label>" in instr
    assert "<label>knuth1984</label>" in instr


def test_endnote_text_cites_set_author_year_on_every_record(tmp_path):
    result = _convert(
        r"\citet{cho2015}; \textcite{cho2015,knuth1984}.", tmp_path,
    )
    instrs = _endnote_instrs(result.docx)
    assert len(instrs) == 2
    assert instrs[0].count('<Cite AuthorYear="1">') == 1
    assert instrs[1].count('<Cite AuthorYear="1">') == 2


def test_endnote_author_year_cite_reads_back_as_text_mode(tmp_path):
    result = _convert(r"\textcite{knuth1984}.", tmp_path, manifest=False)
    doc = read_docx(result.docx)
    cite = next(
        inline
        for block in doc.blocks if isinstance(block, ir.Paragraph)
        for inline in block.inlines if isinstance(inline, ir.Cite)
    )
    assert cite.keys == ["knuth1984"]
    assert cite.mode == "text"


def test_endnote_nocite_emits_hidden_cite_at_command_position(tmp_path):
    result = _convert(r"Before\nocite{knuth1984}After", tmp_path)
    root = document_root(result.docx)
    paragraph = next(
        p for p in root.xpath("//w:p", namespaces=NS)
        if "Before" in "".join(p.itertext())
    )
    sequence = [
        element.text or ""
        for element in paragraph.iter()
        if element.tag.split("}", 1)[-1] in ("t", "instrText")
    ]
    hidden = next(text for text in sequence if "EN.CITE" in text)
    assert sequence.index("Before") < sequence.index(hidden) < sequence.index("After")
    assert hidden.count('<Cite Hidden="1">') == 1
    assert "<label>knuth1984</label>" in hidden


def test_endnote_nocite_star_hides_every_bibliography_record(tmp_path):
    result = _convert(r"\nocite{*}", tmp_path)
    instrs = _endnote_instrs(result.docx)
    assert len(instrs) == 1
    instr = instrs[0]
    assert instr.count('<Cite Hidden="1">') == 2
    assert "<label>cho2015</label>" in instr
    assert "<label>knuth1984</label>" in instr


def test_endnote_hidden_cite_reads_back_as_nocite(tmp_path):
    result = _convert(r"\nocite{knuth1984}", tmp_path, manifest=False)
    doc = read_docx(result.docx)
    cite = next(
        inline
        for block in doc.blocks if isinstance(block, ir.Paragraph)
        for inline in block.inlines if isinstance(inline, ir.Cite)
    )
    assert cite.keys == ["knuth1984"]
    assert cite.hidden is True


def test_endnote_reference_list_uses_en_reflist_field(tmp_path):
    result = _convert(r"\citep{cho2015} and \citep{knuth1984}.", tmp_path)
    assert _instrs(result.docx).count(" ADDIN EN.REFLIST ") == 1


def test_endnote_reference_list_field_wraps_entries(tmp_path):
    result = _convert(r"\citep{cho2015} and \citep{knuth1984}.", tmp_path)
    root = document_root(result.docx)
    sequence: list[str] = []
    for element in root.iter():
        tag = element.tag.split("}", 1)[-1]
        if tag == "fldChar":
            sequence.append("fld:" + element.get(f"{{{NS['w']}}}fldCharType"))
        elif tag == "instrText" and "EN.REFLIST" in (element.text or ""):
            sequence.append("reflist")
        elif tag == "t" and "High-rate nanoscale" in (element.text or ""):
            sequence.append("reference")
    start = sequence.index("reflist")
    field = sequence[start:]
    assert field.index("fld:separate") < field.index("reference") < field.index("fld:end")


def test_endnote_generated_field_reads_back_original_label(tmp_path):
    result = _convert(r"\citep{cho2015}.", tmp_path, manifest=False)
    doc = read_docx(result.docx)
    para = next(block for block in doc.blocks if isinstance(block, ir.Paragraph))
    cite = next(inline for inline in para.inlines if isinstance(inline, ir.Cite))
    assert cite.keys == ["cho2015"]


def test_endnote_reader_without_label_uses_rn_prefix():
    from tex2word.frontend.docx_reader import _Reader

    reader = _Reader(ir.DocumentMeta())
    instr = (
        "ADDIN EN.CITE <EndNote><Cite><Author>Cho</Author><Year>2015</Year>"
        "<RecNum>100</RecNum><record><titles><title>T</title></titles></record>"
        "</Cite></EndNote>"
    )
    cite = reader._endnote_cite(instr, "(Cho 2015)")
    assert cite.keys == ["RN100"]
    assert cite.rendered == "(Cho 2015)"
