"""V5-1: --reference-doc (journal/corporate Word template) support."""

from __future__ import annotations

import io
import zipfile

from lxml import etree

from tex2word import convert_source
from tex2word.backend.numbering import reference_numbering
from tex2word.templates.reference import (
    _separator_notes,
    extract_reference,
    merge_notes,
    merge_settings,
    merge_styles,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# A reference template: Heading1 in red 24pt, A4 page, plus a (tiny) theme part.
_REF_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_W}">
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
    <w:rPr><w:color w:val="FF0000"/><w:sz w:val="48"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="ap1"><w:name w:val="附录1"/></w:style>
  <w:style w:type="paragraph" w:styleId="ap2"><w:name w:val="附录2"/></w:style>
  <w:style w:type="paragraph" w:styleId="pt"><w:name w:val="部分标题"/></w:style>
  <w:style w:type="paragraph" w:styleId="fig"><w:name w:val="图"/></w:style>
  <w:style w:type="paragraph" w:styleId="cap"><w:name w:val="图注"/></w:style>
  <w:style w:type="paragraph" w:styleId="tcap"><w:name w:val="表注"/></w:style>
  <w:style w:type="paragraph" w:styleId="abs"><w:name w:val="Abstract"/></w:style>
  <w:style w:type="paragraph" w:styleId="code"><w:name w:val="Source Code"/></w:style>
  <w:style w:type="paragraph" w:styleId="ni"><w:name w:val="正文缩进"/></w:style>
  <w:style w:type="character" w:styleId="kw"><w:name w:val="关键词"/></w:style>
  <w:style w:type="paragraph" w:styleId="topic"><w:name w:val="术语"/>
    <w:link w:val="topicChar"/></w:style>
  <w:style w:type="character" w:styleId="topicChar"><w:name w:val="术语 字符"/>
    <w:link w:val="topic"/></w:style>
</w:styles>""".encode()

_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"

_REF_DOC = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body><w:p/>
  <w:sectPr>
    <w:headerReference w:type="default" r:id="rId10"/>
    <w:footerReference w:type="default" r:id="rId11"/>
    <w:headerReference w:type="first" r:id="rId12"/>
    <w:headerReference w:type="even" r:id="rId13"/>
    <w:pgSz w:w="11906" w:h="16838"/>
    <w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"
             w:header="360" w:footer="360"/></w:sectPr>
</w:body></w:document>""".encode()

_REF_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_PR}">
  <Relationship Id="rId10" Type="{_R}/header" Target="header1.xml"/>
  <Relationship Id="rId11" Type="{_R}/footer" Target="footer1.xml"/>
  <Relationship Id="rId12" Type="{_R}/header" Target="header2.xml"/>
  <Relationship Id="rId13" Type="{_R}/header" Target="header3.xml"/>
</Relationships>""".encode()

_HDR = (
    f'<?xml version="1.0"?><w:hdr xmlns:w="{_W}">'
    "<w:p><w:r><w:t>Running Title</w:t></w:r></w:p></w:hdr>"
).encode()
_FTR = (
    f'<?xml version="1.0"?><w:ftr xmlns:w="{_W}">'
    "<w:p><w:r><w:t>x</w:t></w:r></w:p></w:ftr>"
).encode()
# header2 references a logo that IS present -> carried with its media.
_HDR2_RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
    f'<Relationship Id="rIdI" Type="{_R}/image" Target="media/logo.png"/></Relationships>'
).encode()
# header3 references a MISSING image -> the whole part is skipped (no dangling rel).
_HDR3_RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
    f'<Relationship Id="rIdI" Type="{_R}/image" Target="media/missing.png"/></Relationships>'
).encode()

# a minimal 1x1 PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63f8cfc0f01f0005000100ff5ccae20000000049454e44ae426082"
)

_REF_THEME = (
    b'<?xml version="1.0"?><a:theme '
    b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="t"/>'
)

# A template numbering part: a bullet list (en-dash), an ordered list ("1)"),
# and a heading-linked multilevel list (Chinese counting at the top level).
_REF_NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{_W}">
  <w:abstractNum w:abstractNumId="7">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8211;"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="8">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1)"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="9">
    <w:multiLevelType w:val="multilevel"/>
    <w:lvl w:ilvl="0"><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="%1"/>
      <w:pStyle w:val="Heading1"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>
      <w:pStyle w:val="Heading2"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="10">
    <w:multiLevelType w:val="multilevel"/>
    <w:lvl w:ilvl="0"><w:numFmt w:val="upperLetter"/><w:lvlText w:val="附录%1"/>
      <w:pStyle w:val="ap1"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>
      <w:pStyle w:val="ap2"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="11">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0"><w:numFmt w:val="upperRoman"/><w:lvlText w:val="第%1部分"/>
      <w:pStyle w:val="pt"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="3"><w:abstractNumId w:val="9"/></w:num>
  <w:num w:numId="42"><w:abstractNumId w:val="7"/></w:num>
</w:numbering>""".encode()


# A settings.xml exercising: an advanced/compat option (doNotExpandShiftReturn),
# a relationship-bearing element (attachedTemplate) and protection -- the latter
# two must be stripped, the compat option preserved.
_REF_SETTINGS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{_W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:writeProtection w:recommended="1"/>
  <w:attachedTemplate r:id="rId1"/>
  <w:defaultTabStop w:val="480"/>
  <w:characterSpacingControl w:val="compressPunctuation"/>
  <w:documentProtection w:edit="readOnly" w:enforcement="1"/>
  <w:footnotePr><w:numFmt w:val="decimalEnclosedCircleChinese"/>
    <w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>
  <w:endnotePr><w:endnote w:id="-1"/><w:endnote w:id="0"/></w:endnotePr>
  <w:compat><w:doNotExpandShiftReturn/></w:compat>
</w:settings>""".encode()

# A footnotes part like a real template's: the separator/continuation notes plus
# a *content* note (id 1) from the template's own body carrying a relationship --
# only the separators should be carried (content + its rels must be dropped).
_REF_FOOTNOTES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="{_W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:hyperlink r:id="rIdX"><w:r><w:t>template note</w:t></w:r></w:hyperlink></w:p></w:footnote>
</w:footnotes>""".encode()

_REF_ENDNOTES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:endnotes xmlns:w="{_W}">
  <w:endnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:endnote>
  <w:endnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:endnote>
</w:endnotes>""".encode()


def _reference_docx(
    with_theme: bool = True, with_headers: bool = True, with_numbering: bool = True,
    with_settings: bool = False,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/styles.xml", _REF_STYLES)
        z.writestr("word/document.xml", _REF_DOC)
        if with_settings:
            z.writestr("word/settings.xml", _REF_SETTINGS)
            z.writestr("word/footnotes.xml", _REF_FOOTNOTES)
            z.writestr("word/endnotes.xml", _REF_ENDNOTES)
        if with_numbering:
            z.writestr("word/numbering.xml", _REF_NUMBERING)
        if with_theme:
            z.writestr("word/theme/theme1.xml", _REF_THEME)
        if with_headers:
            z.writestr("word/_rels/document.xml.rels", _REF_RELS)
            z.writestr("word/header1.xml", _HDR)
            z.writestr("word/footer1.xml", _FTR)
            z.writestr("word/header2.xml", _HDR)  # carries a logo (present)
            z.writestr("word/_rels/header2.xml.rels", _HDR2_RELS)
            z.writestr("word/media/logo.png", _PNG)
            z.writestr("word/header3.xml", _HDR)  # references a missing image
            z.writestr("word/_rels/header3.xml.rels", _HDR3_RELS)
    return buf.getvalue()


def _convert_with_reference(tmp_path, **kw) -> bytes:
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx(**kw))
    src = r"\begin{document}\section{Intro}Body.\end{document}"
    return convert_source(src, reference_doc=str(ref)).docx


def _part(docx: bytes, name: str) -> bytes:
    return zipfile.ZipFile(io.BytesIO(docx)).read(name)


# -- merge_styles ------------------------------------------------------------ #


def test_merge_keeps_reference_styles_and_adds_ours():
    from tex2word.templates import load_styles_xml

    merged = merge_styles(_REF_STYLES, load_styles_xml()).decode()
    # the reference's Heading1 (red) wins, not our bundled one
    assert 'w:styleId="Heading1"' in merged and 'w:val="FF0000"' in merged
    # our custom styles the template lacks are appended so nothing is unstyled
    assert 'w:styleId="SourceCode"' in merged
    assert 'w:styleId="Caption"' in merged


def test_merge_remaps_localized_builtin_styleids():
    # a Chinese-Word template: built-in styles under localized/short styleIds,
    # with their English built-in names in w:name (as Word actually saves them).
    localized = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_W}">
  <w:style w:type="paragraph" w:styleId="a"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/>
    <w:rPr><w:color w:val="00FF00"/></w:rPr></w:style>
  <w:style w:type="character" w:styleId="10"><w:name w:val="heading 1 char"/>
    <w:link w:val="1"/></w:style>
</w:styles>""".encode()
    from tex2word.templates import load_styles_xml

    merged = merge_styles(localized, load_styles_xml()).decode()
    # the template's heading 1 is now reachable under the id our body emits, and
    # its formatting (green) wins over our bundled Heading1.
    assert 'w:styleId="Heading1"' in merged and 'w:val="00FF00"' in merged
    assert 'w:styleId="a"' not in merged  # the localized Normal id was rewritten
    # intra-styles references (w:link) follow the rename, so nothing dangles.
    assert 'w:link w:val="Heading1"' in merged


# -- end-to-end through the pipeline ----------------------------------------- #


def test_reference_styles_are_applied(tmp_path):
    styles = _part(_convert_with_reference(tmp_path), "word/styles.xml").decode()
    assert 'w:val="FF0000"' in styles  # template's red Heading1 carried through
    assert 'w:styleId="SourceCode"' in styles  # merged in from our bundled set


def test_texwordtemplate_directive_selects_reference(tmp_path):
    # \texwordtemplate{...} (relative to the .tex dir) adopts the template styles.
    ref = tmp_path / "tmpl.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordtemplate{tmpl.docx}"
        r"\begin{document}\section{Intro}Body.\end{document}"
    )
    styles = _part(
        convert_source(src, base_dir=str(tmp_path)).docx, "word/styles.xml"
    ).decode()
    assert 'w:val="FF0000"' in styles  # template's red Heading1 carried through


def test_cli_reference_doc_overrides_texwordtemplate(tmp_path):
    # The --reference-doc option wins over an in-source \texwordtemplate directive.
    bad = tmp_path / "missing.docx"  # the directive points at a non-existent file
    good = tmp_path / "good.docx"
    good.write_bytes(_reference_docx())
    src = (
        rf"\texwordtemplate{{{bad}}}"
        r"\begin{document}\section{Intro}Body.\end{document}"
    )
    result = convert_source(src, base_dir=str(tmp_path), reference_doc=str(good))
    styles = _part(result.docx, "word/styles.xml").decode()
    assert 'w:val="FF0000"' in styles  # the CLI template was used, not the directive
    assert not any(w.construct == "reference-doc" for w in result.report.warnings)


def test_reference_theme_is_carried(tmp_path):
    docx = _convert_with_reference(tmp_path)
    names = zipfile.ZipFile(io.BytesIO(docx)).namelist()
    assert "word/theme/theme1.xml" in names
    ctypes = _part(docx, "[Content_Types].xml").decode()
    assert "theme+xml" in ctypes
    rels = _part(docx, "word/_rels/document.xml.rels").decode()
    assert "theme/theme1.xml" in rels


def test_reference_page_geometry_is_applied(tmp_path):
    doc = _part(_convert_with_reference(tmp_path), "word/document.xml").decode()
    assert 'w:w="11906"' in doc and 'w:h="16838"' in doc  # A4 from the template


def test_no_reference_uses_builtin_letter(tmp_path):
    src = r"\begin{document}\section{S}x\end{document}"
    doc = _part(convert_source(src).docx, "word/document.xml").decode()
    assert 'w:w="12240"' in doc  # the built-in Letter default, unchanged


# -- settings.xml carry-over (advanced/compatibility options) ---------------- #


def test_merge_settings_keeps_compat_and_note_format_drops_unsafe():
    out = merge_settings(_REF_SETTINGS)
    assert b"doNotExpandShiftReturn" in out  # advanced/compat option preserved
    assert b"characterSpacingControl" in out and b"defaultTabStop" in out
    assert b"decimalEnclosedCircleChinese" in out  # footnote number format preserved
    assert b"<w:footnote " in out  # separator references kept (parts are carried now)
    assert b"attachedTemplate" not in out  # relationship-bearing -> stripped
    assert b"Protection" not in out  # write/document protection -> stripped
    assert b"updateFields" in out  # our field-refresh setting (re)inserted


def test_separator_notes_keeps_only_separators():
    out = _separator_notes(_REF_FOOTNOTES, "footnote")
    import lxml.etree as ET

    root = ET.fromstring(out)
    ids = [c.get(f"{{{_W}}}id") for c in root]
    assert ids == ["-1", "0"]  # content note id=1 dropped
    assert b"hyperlink" not in out and b"rIdX" not in out  # its relationship gone


def test_separator_notes_returns_none_without_separators():
    only_content = f'<w:footnotes xmlns:w="{_W}"><w:footnote w:id="1"/></w:footnotes>'.encode()
    assert _separator_notes(only_content, "footnote") is None


def test_merge_notes_combines_template_separators_with_content():
    generated = (
        f'<w:footnotes xmlns:w="{_W}">'
        f'<w:footnote w:type="separator" w:id="-1"/>'
        f'<w:footnote w:type="continuationSeparator" w:id="0"/>'
        f'<w:footnote w:id="1"><w:p/></w:footnote></w:footnotes>'
    ).encode()
    seps = _separator_notes(_REF_FOOTNOTES, "footnote")
    import lxml.etree as ET

    root = ET.fromstring(merge_notes(seps, generated))
    ids = [(c.get(f"{{{_W}}}type"), c.get(f"{{{_W}}}id")) for c in root]
    assert ids == [("separator", "-1"), ("continuationSeparator", "0"), (None, "1")]


def test_reference_notes_carried_when_doc_has_no_notes(tmp_path):
    # regression: a template whose settings.xml references note separators must get
    # its footnotes.xml/endnotes.xml carried (separators only), so the references
    # resolve and Word doesn't report unreadable footnote/endnote content.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx(with_settings=True))
    src = r"\begin{document}\section{S}no notes here\end{document}"
    docx = convert_source(src, reference_doc=str(ref)).docx
    names = set(zipfile.ZipFile(io.BytesIO(docx)).namelist())
    assert "word/footnotes.xml" in names and "word/endnotes.xml" in names
    footnotes = _part(docx, "word/footnotes.xml")
    assert b'w:type="separator"' in footnotes  # the template separators are present
    assert b"template note" not in footnotes  # the template's content note is not


def test_merge_settings_orders_updatefields_before_compat():
    import lxml.etree as ET

    root = ET.fromstring(merge_settings(_REF_SETTINGS))
    kids = [ET.QName(c).localname for c in root if isinstance(c.tag, str)]
    assert kids.index("updateFields") < kids.index("compat")  # ECMA-376 order


def test_reference_settings_carried_into_output(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx(with_settings=True))
    src = r"\begin{document}\section{S}x\end{document}"
    settings = _part(convert_source(src, reference_doc=str(ref)).docx, "word/settings.xml")
    assert b"doNotExpandShiftReturn" in settings  # template's advanced option survives
    assert b"updateFields" in settings


def test_no_reference_uses_builtin_settings(tmp_path):
    src = r"\begin{document}\section{S}x\end{document}"
    settings = _part(convert_source(src).docx, "word/settings.xml").decode()
    assert "updateFields" in settings and "compat" not in settings


def test_output_with_carried_settings_is_valid(tmp_path):
    from tex2word.validate import validate_docx

    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx(with_settings=True))
    src = r"\begin{document}\section{S}x\end{document}"
    assert validate_docx(convert_source(src, reference_doc=str(ref)).docx) == []


def test_output_with_reference_is_valid(tmp_path):
    from tex2word.validate import validate_docx

    assert validate_docx(_convert_with_reference(tmp_path)) == []


def test_invalid_reference_warns_and_falls_back(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a docx at all")
    src = r"\begin{document}\section{S}x\end{document}"
    result = convert_source(src, reference_doc=str(bad))
    styles = _part(result.docx, "word/styles.xml").decode()
    assert 'w:styleId="Heading1"' in styles  # bundled styles still present
    assert any(w.construct == "reference-doc" for w in result.report.warnings)


def test_extract_reference_reads_parts():
    ref = extract_reference(_reference_docx())
    assert ref.theme_xml is not None
    assert ref.page_pgsz == {"w:w": "11906", "w:h": "16838"}
    assert ref.page_pgmar and ref.page_pgmar["w:top"] == "720"


# -- headers / footers ------------------------------------------------------- #


def test_reference_headers_footers_are_carried(tmp_path):
    docx = _convert_with_reference(tmp_path)
    names = zipfile.ZipFile(io.BytesIO(docx)).namelist()
    assert "word/header1.xml" in names and "word/footer1.xml" in names
    assert "word/header2.xml" in names  # carries its logo
    assert "word/header3.xml" not in names  # missing image -> skipped
    ctypes = _part(docx, "[Content_Types].xml").decode()
    assert "wordprocessingml.header+xml" in ctypes and "wordprocessingml.footer+xml" in ctypes


def test_header_logo_subresource_is_carried_and_namespaced(tmp_path):
    docx = _convert_with_reference(tmp_path)
    names = zipfile.ZipFile(io.BytesIO(docx)).namelist()
    # the logo media is carried under the tmpl/ namespace (no collision with ours)
    assert "word/media/tmpl/header2_logo.png" in names
    # header2's rels were carried and rewritten to point at the namespaced media
    rels = _part(docx, "word/_rels/header2.xml.rels").decode()
    assert "media/tmpl/header2_logo.png" in rels and "media/logo.png" not in rels


def test_sectpr_references_headers_and_footers(tmp_path):
    doc = _part(_convert_with_reference(tmp_path), "word/document.xml").decode()
    assert "headerReference" in doc and "footerReference" in doc
    rels = _part(_convert_with_reference(tmp_path), "word/_rels/document.xml.rels").decode()
    # every header/footer reference in the body must resolve to a relationship
    import re
    rids = set(re.findall(r'r:id="(rIdHF\d+)"', doc))
    assert rids, "no header/footer references emitted"
    for rid in rids:
        assert f'Id="{rid}"' in rels


def test_header_with_unresolvable_subresource_is_skipped(tmp_path):
    ref = extract_reference(_reference_docx())
    carried = {hf.part_name for hf in ref.headers_footers}
    assert {"header1.xml", "footer1.xml", "header2.xml"} <= carried
    # header3 points at a missing image -> skipped, never carried (no dangling rel)
    assert "header3.xml" not in carried
    assert ref.skipped_header_footers == 1
    # header2 carried its media + rewritten rels
    h2 = next(hf for hf in ref.headers_footers if hf.part_name == "header2.xml")
    assert h2.rels is not None and h2.media


def test_output_with_headers_is_valid(tmp_path):
    from tex2word.validate import validate_docx

    assert validate_docx(_convert_with_reference(tmp_path)) == []


# -- numbering (multilevel + item lists) ------------------------------------- #


def _num_for(numbering_xml: str, num_id: str) -> str:
    """The abstractNumId a given w:num points at, from a numbering.xml string."""
    import re

    m = re.search(
        rf'<w:num w:numId="{num_id}"[^>]*>\s*<w:abstractNumId w:val="(\d+)"',
        numbering_xml,
    )
    assert m, f"no w:num {num_id} in numbering"
    return m.group(1)


# the template's highest numId is 42, so our role numIds land at the 1000 floor.
_B = 1000  # bullet; +1 decimal, +2 heading, +3 appendix, +4 part


def test_reference_numbering_remaps_our_numids(tmp_path):
    nbr = _part(_convert_with_reference(tmp_path), "word/numbering.xml").decode()
    # our role numIds (shifted clear of the template) point at its own abstractNums:
    assert _num_for(nbr, str(_B)) == "7"      # bullet -> template's en-dash list
    assert _num_for(nbr, str(_B + 1)) == "8"  # decimal -> template's "1)" ordered list
    assert _num_for(nbr, str(_B + 2)) == "9"  # headings -> template's chineseCounting list
    # the template's level formats are carried verbatim
    assert "chineseCounting" in nbr and "%1)" in nbr


def test_reference_numbering_keeps_template_numids(tmp_path):
    import re

    nbr = _part(_convert_with_reference(tmp_path), "word/numbering.xml").decode()
    ids = sorted(int(i) for i in re.findall(r'<w:num w:numId="(\d+)"', nbr))
    # the template's own w:num (3, 42) are carried verbatim, plus our five roles
    assert ids == [3, 42, _B, _B + 1, _B + 2, _B + 3, _B + 4]


def test_reference_numbering_falls_back_per_role(tmp_path):
    # a template whose numbering defines ONLY a bullet list: bullet adopts it,
    # but decimal/heading/appendix/part fall back to our bundled definitions.
    nbr = _part(
        _convert_with_reference(tmp_path, with_numbering=False), "word/numbering.xml"
    ).decode()
    # no template numbering at all -> our built-in part (default numIds 1-5) is used
    assert _num_for(nbr, "3") in {"2"}  # our heading abstractNum
    assert "Heading1" in nbr


def test_numbering_heading_link_survives_localized_styleids():
    # a Chinese template: heading 1 saved under styleId "1"; its multilevel list
    # links levels to "1"/"2". The pStyle refs must be rewritten to Heading1/2.
    localized_styles = f"""<?xml version="1.0"?><w:styles xmlns:w="{_W}">
      <w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>
      <w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/></w:style>
    </w:styles>"""
    ref_numbering = f"""<?xml version="1.0"?><w:numbering xmlns:w="{_W}">
      <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>
        <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:pStyle w:val="1"/></w:lvl>
        <w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:pStyle w:val="2"/></w:lvl>
      </w:abstractNum></w:numbering>""".encode()
    from lxml import etree

    from tex2word.backend.numbering import reference_num_ids
    from tex2word.templates.reference import _compute_builtin_rename

    rename = _compute_builtin_rename(etree.fromstring(localized_styles.encode()))
    num_ids = reference_num_ids(ref_numbering)  # no template w:num -> floor at 1000
    out = reference_numbering(ref_numbering, rename, num_ids).decode()
    assert 'w:pStyle w:val="Heading1"' in out  # rewritten from "1"
    assert 'w:pStyle w:val="Heading2"' in out  # rewritten from "2"
    assert _num_for(out, str(num_ids.heading)) == "0"  # headings bind to template list


def test_carried_heading_style_keeps_its_template_numbering(tmp_path):
    # a template whose heading 1 style carries its OWN numId for a "第%1章" list:
    # the template's numbering is carried verbatim, so the style's numId stays
    # valid and the chapter numbering survives without any relinking.

    from lxml import etree

    styles = _REF_STYLES.replace(
        b'<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>',
        b'<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        b'<w:pPr><w:numPr><w:numId w:val="3"/></w:numPr></w:pPr>',
    )
    ref = tmp_path / "template.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/styles.xml", styles)
        z.writestr("word/document.xml", _REF_DOC)
        z.writestr("word/numbering.xml", _REF_NUMBERING)
    ref.write_bytes(buf.getvalue())

    docx = convert_source(
        r"\begin{document}\section{Intro}Body.\end{document}", reference_doc=str(ref)
    ).docx
    st = etree.fromstring(_part(docx, "word/styles.xml"))
    nbr = _part(docx, "word/numbering.xml").decode()
    h1 = next(s for s in st.findall(f"{{{_W}}}style")
              if s.get(f"{{{_W}}}styleId") == "Heading1")
    num_id = h1.find(f"{{{_W}}}pPr/{{{_W}}}numPr/{{{_W}}}numId").get(f"{{{_W}}}val")
    assert num_id == "3"  # the style keeps the template's own numId, untouched
    assert _num_for(nbr, "3") == "9"  # which still resolves (carried verbatim)


def test_extract_reference_reads_numbering():
    ref = extract_reference(_reference_docx())
    assert ref.raw_numbering is not None
    assert b"chineseCounting" in ref.raw_numbering
    # the name->styleId map resolves \texwordstyle's display names
    assert ref.style_name_to_id.get("附录1") == "ap1"
    assert ref.style_name_to_id.get("部分标题") == "pt"


# -- \texwordstyle: appendix / part binding ---------------------------------- #


def _convert_appendix(tmp_path) -> bytes:
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{appendix1}{附录1}"
        r"\texwordstyle{appendix2}{附录2}"
        r"\texwordstyle{part}{部分标题}"
        r"\begin{document}"
        r"\part{First Part}"
        r"\section{Body}text."
        r"\appendix\section{Extra}\subsection{Detail}"
        r"\end{document}"
    )
    return convert_source(src, reference_doc=str(ref)).docx


def test_appendix_and_part_numbering_follow_template(tmp_path):
    nbr = _part(_convert_appendix(tmp_path), "word/numbering.xml").decode()
    assert _num_for(nbr, str(_B + 3)) == "10"  # appendix -> template's 附录 list
    assert _num_for(nbr, str(_B + 4)) == "11"  # \part -> template's 第N部分 list
    assert "附录%1" in nbr and "第%1部分" in nbr


def test_appendix_and_part_paragraphs_use_template_styles(tmp_path):
    doc = _part(_convert_appendix(tmp_path), "word/document.xml").decode()
    # the appendix \section adopts the bound 附录1 style (styleId ap1), \part the
    # 部分标题 style (pt) -- while the non-appendix \section stays Heading1.
    assert 'w:pStyle w:val="ap1"' in doc
    assert 'w:pStyle w:val="ap2"' in doc  # the appendix \subsection -> level 2
    assert 'w:pStyle w:val="pt"' in doc
    assert 'w:pStyle w:val="Heading1"' in doc  # the ordinary section is unchanged


def test_starred_appendix_heading_uses_bound_style_without_numbering(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{appendix1}{附录1}"
        r"\begin{document}\appendix\section*{Extra}\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="ap1"' in doc
    assert "<w:numPr>" not in doc


def test_starred_section_can_use_custom_style_without_changing_numbered(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{section*}{部分标题}"
        r"\begin{document}\section{Numbered}\section*{Starred}\end{document}"
    )
    root = etree.fromstring(_part(convert_source(src, reference_doc=str(ref)).docx,
                                  "word/document.xml"))
    paras = root.findall(f".//{{{_W}}}p")
    styled = []
    for p in paras:
        sid = p.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
        if sid is None:
            continue
        numpr = p.find(f"{{{_W}}}pPr/{{{_W}}}numPr")
        text = "".join(t.text or "" for t in p.iter(f"{{{_W}}}t"))
        styled.append((text, sid.get(f"{{{_W}}}val"), numpr is not None))
    assert ("Numbered", "Heading1", True) in styled
    assert ("Starred", "pt", False) in styled


def test_starred_heading_level_role_applies_to_subsection(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{heading2*}{部分标题}"
        r"\begin{document}\subsection*{Unnumbered}\subsection{Numbered}\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="pt"' in doc
    assert 'w:pStyle w:val="Heading2"' in doc


def test_book_starred_section_role_uses_book_level(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\documentclass{book}"
        r"\texwordstyle{section*}{部分标题}"
        r"\begin{document}\section*{Book Section}\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="pt"' in doc
    assert 'w:pStyle w:val="Heading2"' not in doc


def test_unbound_appendix_falls_back_to_builtin(tmp_path):
    # no \texwordstyle -> appendix/part keep the bundled numbering + Heading styles
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = r"\begin{document}\appendix\section{Extra}\end{document}"
    docx = convert_source(src, reference_doc=str(ref)).docx
    doc = _part(docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Heading1"' in doc and "ap1" not in doc


def test_figure_and_caption_styles_follow_texwordstyle(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{figure}{图}"
        r"\texwordstyle{caption}{图注}"
        r"\begin{document}"
        r"\begin{figure}\includegraphics{img.png}\caption{Hi}\end{figure}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="fig"' in doc  # the image line adopts the 图 style
    assert 'w:pStyle w:val="cap"' in doc  # the caption adopts the 图注 style


def test_caption_style_defaults_when_unbound(tmp_path):
    # without \texwordstyle{caption}, captions keep the built-in Caption style
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"\begin{figure}\includegraphics{img.png}\caption{Hi}\end{figure}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Caption"' in doc and 'w:pStyle w:val="cap"' not in doc


def test_commented_texwordstyle_does_not_warn(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = r"""
\texwordstyle{tablecaption}{表注}
%\texwordstyle{threelinetable}{三线表}
\texwordstyle{Abstract}{Abstract}
\begin{document}Plain text.\end{document}
"""
    result = convert_source(src, reference_doc=str(ref))
    assert not any("三线表" in w.message for w in result.report.warnings)


def test_per_type_caption_overrides_default(tmp_path):
    # {caption} sets the default for all; {tablecaption} overrides only tables.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{caption}{图注}"
        r"\texwordstyle{tablecaption}{表注}"
        r"\begin{document}"
        r"\begin{figure}\includegraphics{img.png}\caption{F}\end{figure}"
        r"\begin{table}\begin{tabular}{c}a\end{tabular}\caption{T}\end{table}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="cap"' in doc   # figure caption -> the 图注 default
    assert 'w:pStyle w:val="tcap"' in doc  # table caption -> the 表注 override
    assert 'w:pStyle w:val="Caption"' not in doc  # nothing left on the default


def test_texwordcaption_labelstyle_styles_caption_identifier(tmp_path):
    # \texwordcaption{labelstyle}{name} styles the displayed "Figure N:" lead
    # without styling the caption body text.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordcaption{labelstyle}{关键词}"
        r"\begin{document}\begin{figure}\caption{Hi}\end{figure}\end{document}"
    )
    root = etree.fromstring(_part(convert_source(src, reference_doc=str(ref)).docx,
                                  "word/document.xml"))
    cap = next(
        p for p in root.findall(f".//{{{_W}}}p")
        if "Hi" in "".join(t.text or "" for t in p.iter(f"{{{_W}}}t"))
    )

    def run_style(run):
        rstyle = run.find(f"{{{_W}}}rPr/{{{_W}}}rStyle")
        return rstyle.get(f"{{{_W}}}val") if rstyle is not None else None

    runs = cap.findall(f"{{{_W}}}r")
    styled_text = [
        ("".join(t.text or "" for t in r.iter(f"{{{_W}}}t")), run_style(r))
        for r in runs
    ]
    assert ("Figure ", "kw") in styled_text
    assert ("1", "kw") in styled_text
    assert (": ", "kw") in styled_text
    assert ("Hi", None) in styled_text


def test_texwordcaption_labelstyle_accepts_linked_style_and_per_kind_override(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordcaption{labelstyle}{关键词}"
        r"\texwordcaption{tablelabelstyle}{术语}"
        r"\begin{document}"
        r"\begin{figure}\caption{F}\end{figure}"
        r"\begin{table}\begin{tabular}{c}x\end{tabular}\caption{T}\end{table}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:rStyle w:val="kw"' in doc
    assert 'w:rStyle w:val="topicChar"' in doc
    assert 'w:rStyle w:val="topic"' not in doc


def test_texwordcaption_algorithm_labelstyle(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordcaption{algorithmlabelstyle}{关键词}"
        r"\begin{document}"
        r"\begin{algorithm}\caption{Algo}\begin{algorithmic}\State x\end{algorithmic}\end{algorithm}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:rStyle w:val="kw"' in doc
    assert ">Algo<" in doc


def test_generic_paragraph_style_autodiscovered_by_name(tmp_path):
    # no \texwordstyle: a template style named like the role is found automatically.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"\begin{abstract}Summary.\end{abstract}"
        r"\begin{verbatim}code here\end{verbatim}"
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="abs"' in doc   # Abstract found by name (styleId abs)
    assert 'w:pStyle w:val="code"' in doc  # Source Code found by name (styleId code)
    assert 'w:pStyle w:val="Abstract"' not in doc  # not the bundled fallback


def test_generic_paragraph_style_explicit_binding_wins(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{abstract}{部分标题}"  # bind abstract to an arbitrary style
        r"\begin{document}\begin{abstract}S.\end{abstract}\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="pt"' in doc  # the bound 部分标题 (styleId pt) wins
    assert 'w:pStyle w:val="abs"' not in doc


def test_body_style_follows_texwordstyle(tmp_path):
    # \texwordstyle{body} restyles ordinary 正文 paragraphs (default Normal).
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{body}{正文缩进}"
        r"\begin{document}Body text.\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="ni"' in doc          # 正文缩进 (styleId ni) applied to body
    assert 'w:pStyle w:val="Normal"' not in doc  # body no longer the bundled Normal


def test_body_style_defaults_to_normal_when_unbound(tmp_path):
    # without \texwordstyle{body}, 正文 keeps the built-in Normal style.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = r"\begin{document}Body text.\end{document}"
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Normal"' in doc
    assert 'w:pStyle w:val="ni"' not in doc


def test_texwordparstyle_sets_one_paragraph_style(tmp_path):
    # \texwordparstyle{name} styles just the paragraph it precedes (like \noindent),
    # naming a reference-doc style by its display name; the next paragraph is Normal.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"\texwordparstyle{部分标题}First paragraph." "\n\n"
        r"Second paragraph."
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    # the styled paragraph adopts 部分标题 (styleId pt); the directive leaves no text
    assert 'w:pStyle w:val="pt"' in doc
    assert "部分标题" not in doc and "texwordparstyle" not in doc
    # scope is one paragraph: the second paragraph keeps the bundled Normal
    assert 'w:pStyle w:val="Normal"' in doc
    assert doc.count('w:pStyle w:val="pt"') == 1


def test_texwordparstyle_unknown_style_warns_and_falls_back(tmp_path):
    # an unknown style name warns once and the paragraph keeps the default (Normal).
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"\texwordparstyle{NoSuchStyle}Body text."
        r"\end{document}"
    )
    result = convert_source(src, reference_doc=str(ref))
    doc = _part(result.docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Normal"' in doc
    assert any("texwordparstyle" in w.message for w in result.report.warnings)


def test_texwordcharstyle_sets_character_style(tmp_path):
    # \texwordcharstyle{name}{text} applies a reference-doc character style to
    # just that inline span, naming the style by its Word display name.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"Before \texwordcharstyle{关键词}{styled text} after."
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:rStyle w:val="kw"' in doc
    assert "texwordcharstyle" not in doc and "关键词" not in doc
    assert ">styled text<" in doc


def test_texwordcharstyle_accepts_linked_paragraph_style_name(tmp_path):
    # Naming a linked paragraph/character style by the paragraph style's display
    # name resolves to its linked character style id.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"Before \texwordcharstyle{术语}{linked text} after."
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:rStyle w:val="topicChar"' in doc
    assert 'w:pStyle w:val="topic"' not in doc


def test_texwordcharstyle_declaration_scopes_to_group(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"Before {\texwordcharstyle{关键词}declared text} after."
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert doc.count('w:rStyle w:val="kw"') == 1
    assert ">declared text<" in doc


def test_texwordcharstyle_unknown_style_warns_and_falls_back(tmp_path):
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\begin{document}"
        r"\texwordcharstyle{NoSuchStyle}{Body text.}"
        r"\end{document}"
    )
    result = convert_source(src, reference_doc=str(ref))
    doc = _part(result.docx, "word/document.xml").decode()
    assert 'w:rStyle w:val="NoSuchStyle"' not in doc
    assert ">Body text.<" in doc
    assert any("texwordcharstyle" in w.message for w in result.report.warnings)


def test_noindent_adopts_bound_style(tmp_path):
    # \texwordstyle{noindent}{name} makes every \noindent paragraph take that style;
    # a paragraph without \noindent keeps the default (Normal). Scope is per-paragraph.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = (
        r"\texwordstyle{noindent}{正文缩进}"
        r"\begin{document}"
        r"\noindent First paragraph." "\n\n"
        r"Second paragraph."
        r"\end{document}"
    )
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert doc.count('w:pStyle w:val="ni"') == 1   # only the \noindent paragraph
    assert 'w:pStyle w:val="Normal"' in doc          # the plain paragraph stays Normal


def test_noindent_dropped_when_unbound(tmp_path):
    # without a \texwordstyle{noindent} binding, \noindent is dropped as before.
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = r"\begin{document}\noindent Body text.\end{document}"
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Normal"' in doc
    assert 'w:pStyle w:val="ni"' not in doc


def test_generic_paragraph_style_falls_back_when_absent(tmp_path):
    # a template lacking an Abstract-named style -> abstract keeps the bundled style
    minimal = io.BytesIO()
    with zipfile.ZipFile(minimal, "w") as z:
        z.writestr("word/styles.xml", _REF_STYLES.replace(
            b'<w:style w:type="paragraph" w:styleId="abs"><w:name w:val="Abstract"/></w:style>',
            b"",
        ))
        z.writestr("word/document.xml", _REF_DOC)
    ref = tmp_path / "template.docx"
    ref.write_bytes(minimal.getvalue())
    src = r"\begin{document}\begin{abstract}S.\end{abstract}\end{document}"
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert 'w:pStyle w:val="Abstract"' in doc  # bundled Abstract, no remap


def test_texwordstyle_directive_emits_no_body_text(tmp_path):
    # the directive itself must never leak into the output text
    ref = tmp_path / "template.docx"
    ref.write_bytes(_reference_docx())
    src = r"\begin{document}\texwordstyle{part}{部分标题}Hello.\end{document}"
    doc = _part(convert_source(src, reference_doc=str(ref)).docx, "word/document.xml").decode()
    assert "texwordstyle" not in doc and "部分标题" not in doc


# -- \texwordtemplate[keep]: content-injection mode -------------------------- #

# A keep-mode template: a cover paragraph, the bookmarked anchor paragraph, and a
# trailing back-matter paragraph. The converted body is spliced after the anchor.
_KEEP_DOC = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>
  <w:p><w:r><w:t>COVER PAGE</w:t></w:r></w:p>
  <w:p><w:bookmarkStart w:id="9" w:name="tex2word_section"/>
       <w:bookmarkEnd w:id="9"/><w:r><w:t>ANCHOR</w:t></w:r></w:p>
  <w:p><w:r><w:t>BACK MATTER</w:t></w:r></w:p>
  <w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
</w:body></w:document>""".encode()

_CT_NS = _PR.replace("relationships", "content-types")
_KEEP_CT = f"""<?xml version="1.0"?><Types xmlns="{_CT_NS}">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>""".encode()

_KEEP_DOC_RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
    f'<Relationship Id="rId1" Type="{_R}/styles" Target="styles.xml"/>'
    "</Relationships>"
).encode()

_KEEP_ROOT_RELS = (
    f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
    f'<Relationship Id="rIdDoc" Type="{_R}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
).encode()


def _keep_docx(document: bytes = _KEEP_DOC, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", _KEEP_CT)
        z.writestr("_rels/.rels", _KEEP_ROOT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _REF_STYLES)
        z.writestr("word/_rels/document.xml.rels", _KEEP_DOC_RELS)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _keep_convert(tmp_path, body: str, *, template: bytes | None = None):
    ref = tmp_path / "tmpl.docx"
    ref.write_bytes(template if template is not None else _keep_docx())
    src = (
        r"\texwordtemplate[keep]{tmpl.docx}"
        r"\begin{document}" + body + r"\end{document}"
    )
    return convert_source(src, base_dir=str(tmp_path))


def test_keep_mode_splices_body_between_template_content(tmp_path):
    result = _keep_convert(tmp_path, r"\section{Intro}Hello body.")
    doc = _part(result.docx, "word/document.xml").decode()
    # the template's own content is preserved ...
    assert "COVER PAGE" in doc and "BACK MATTER" in doc
    # ... and our converted body is spliced in between (where the anchor was).
    assert "Intro" in doc
    assert doc.index("COVER PAGE") < doc.index("Intro") < doc.index("BACK MATTER")
    # the bookmarked placeholder paragraph itself is replaced (no stray empty page)
    assert "ANCHOR" not in doc


def test_keep_mode_preserves_section_break_on_replaced_anchor(tmp_path):
    # the bookmarked paragraph is deleted, but a section break it carried must
    # survive (on a trailing empty paragraph) so the template's layout is kept.
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
        '<w:p><w:r><w:t>COVER</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:type w:val="nextPage"/></w:sectPr></w:pPr>'
        '<w:bookmarkStart w:id="9" w:name="tex2word_section"/>'
        '<w:bookmarkEnd w:id="9"/></w:p>'
        '<w:p><w:r><w:t>BACK</w:t></w:r></w:p>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        "</w:body></w:document>"
    ).encode()
    result = _keep_convert(
        tmp_path, r"\section{Intro}Body.", template=_keep_docx(document)
    )
    body = etree.fromstring(_part(result.docx, "word/document.xml")).find(f"{{{_W}}}body")
    # exactly the two section breaks remain (the carried one + the body-level one)
    sectprs = list(body.iter(f"{{{_W}}}sectPr"))
    assert len(sectprs) == 2


def test_keep_mode_output_is_structurally_valid(tmp_path):
    from tex2word.validate import validate_docx

    result = _keep_convert(tmp_path, r"\section{Intro}Body with $E=mc^2$.")
    assert validate_docx(result.docx) == []


def test_keep_mode_roundtrips(tmp_path):
    from tex2word.roundtrip import to_latex

    result = _keep_convert(tmp_path, r"\section{Intro}Recoverable body.")
    latex = to_latex(result.docx, reconcile=False)
    assert latex is not None and "Intro" in latex


def test_keep_mode_embeds_manifest_and_merges_styles(tmp_path):
    result = _keep_convert(tmp_path, r"\section{Intro}Body.")
    names = zipfile.ZipFile(io.BytesIO(result.docx)).namelist()
    assert "word/tex2word/manifest.json" in names
    # our custom styles are merged into the template's styles.xml
    styles = _part(result.docx, "word/styles.xml").decode()
    assert 'w:styleId="SourceCode"' in styles


def test_keep_mode_without_bookmark_falls_back_and_warns(tmp_path):
    no_bm = f"""<?xml version="1.0"?>
    <w:document xmlns:w="{_W}"><w:body>
      <w:p><w:r><w:t>COVER PAGE</w:t></w:r></w:p>
      <w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
    </w:body></w:document>""".encode()
    result = _keep_convert(tmp_path, r"\section{Intro}Body.", template=_keep_docx(no_bm))
    doc = _part(result.docx, "word/document.xml").decode()
    assert "Intro" in doc  # converted on the styling-only fallback path
    assert "COVER PAGE" not in doc  # template content not preserved on fallback
    assert any("texwordtemplate[keep]" in w.message for w in result.report.warnings)


def test_keep_mode_offsets_our_footnotes_past_the_templates(tmp_path):
    fn = (
        f'<?xml version="1.0"?><w:footnotes xmlns:w="{_W}">'
        '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p/></w:footnote>'
        '<w:footnote w:id="1"><w:p><w:r><w:t>TEMPLATE NOTE</w:t></w:r></w:p></w:footnote>'
        "</w:footnotes>"
    ).encode()
    rels = (
        f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
        f'<Relationship Id="rId1" Type="{_R}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rIdFn" Type="{_R}/footnotes" Target="footnotes.xml"/>'
        "</Relationships>"
    ).encode()
    ct = _KEEP_CT.replace(
        b"</Types>",
        b'<Override PartName="/word/footnotes.xml" ContentType="application/'
        b'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>',
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", _KEEP_ROOT_RELS)
        z.writestr("word/document.xml", _KEEP_DOC)
        z.writestr("word/styles.xml", _REF_STYLES)
        z.writestr("word/footnotes.xml", fn)
        z.writestr("word/_rels/document.xml.rels", rels)
    result = _keep_convert(
        tmp_path, r"\section{Intro}Body.\footnote{OUR NOTE}", template=buf.getvalue()
    )
    notes = _part(result.docx, "word/footnotes.xml").decode()
    assert "TEMPLATE NOTE" in notes and "OUR NOTE" in notes  # both kept
    root = etree.fromstring(_part(result.docx, "word/footnotes.xml"))
    ids = sorted(
        int(n.get(f"{{{_W}}}id")) for n in root if n.get(f"{{{_W}}}type") is None
    )
    assert ids == [1, 2]  # template note id 1, ours shifted to 2 (no collision)
    body = etree.fromstring(_part(result.docx, "word/document.xml"))
    refs = [r.get(f"{{{_W}}}id") for r in body.iter(f"{{{_W}}}footnoteReference")]
    assert refs == ["2"]  # our body reference follows the offset


def test_keep_mode_relocates_our_images_clear_of_template_media(tmp_path):
    # template already ships word/media/image1.png + a rIdImg1 relationship; our
    # converted image (also image1.png / rIdImg1) must not clobber either.
    rels = (
        f'<?xml version="1.0"?><Relationships xmlns="{_PR}">'
        f'<Relationship Id="rId1" Type="{_R}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rIdImg1" Type="{_R}/image" Target="media/image1.png"/>'
        "</Relationships>"
    ).encode()
    ct = _KEEP_CT.replace(
        b'<Default Extension="xml" ContentType="application/xml"/>',
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="png" ContentType="image/png"/>',
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", _KEEP_ROOT_RELS)
        z.writestr("word/document.xml", _KEEP_DOC)
        z.writestr("word/styles.xml", _REF_STYLES)
        z.writestr("word/media/image1.png", _PNG)
        z.writestr("word/_rels/document.xml.rels", rels)
    (tmp_path / "pic.png").write_bytes(_PNG)
    result = _keep_convert(
        tmp_path, r"\includegraphics{pic.png}", template=buf.getvalue()
    )
    names = zipfile.ZipFile(io.BytesIO(result.docx)).namelist()
    assert "word/media/image1.png" in names  # template image untouched
    assert "word/media/t2w/image1.png" in names  # ours relocated
    doc = _part(result.docx, "word/document.xml").decode()
    assert 'r:embed="rIdT2W1"' in doc  # our colliding rel id was renamed


def test_keep_mode_default_is_styling_only(tmp_path):
    # \texwordtemplate WITHOUT [keep] keeps the historical styling-only behaviour.
    ref = tmp_path / "tmpl.docx"
    ref.write_bytes(_keep_docx())
    src = (
        r"\texwordtemplate{tmpl.docx}"
        r"\begin{document}\section{Intro}Body.\end{document}"
    )
    doc = _part(
        convert_source(src, base_dir=str(tmp_path)).docx, "word/document.xml"
    ).decode()
    assert "Intro" in doc and "COVER PAGE" not in doc


def test_keep_mode_dotx_template_rewrites_main_part_content_type(tmp_path):
    # A .dotx template declares /word/document.xml as the *template* main part;
    # the emitted .docx must use the *document* main part or Word reports it as
    # corrupt. (We feed a template-typed Content_Types and check it is rewritten.)
    template_ct = _KEEP_CT.replace(
        b"wordprocessingml.document.main+xml", b"wordprocessingml.template.main+xml"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", template_ct)
        z.writestr("_rels/.rels", _KEEP_ROOT_RELS)
        z.writestr("word/document.xml", _KEEP_DOC)
        z.writestr("word/styles.xml", _REF_STYLES)
        z.writestr("word/_rels/document.xml.rels", _KEEP_DOC_RELS)
    result = _keep_convert(tmp_path, r"\section{Intro}Body.", template=buf.getvalue())
    ct = _part(result.docx, "[Content_Types].xml").decode()
    assert "wordprocessingml.document.main+xml" in ct
    assert "wordprocessingml.template.main+xml" not in ct


def test_keep_mode_remaps_localised_builtin_style_refs(tmp_path):
    # The template's cover paragraphs reference a localised built-in style id
    # ("aff9" whose w:name is "Title"); merge_styles normalises that id to our
    # canonical "Title", so the kept body's reference must be remapped too or the
    # cover line loses its style.
    styles = (
        f'<?xml version="1.0"?><w:styles xmlns:w="{_W}">'
        '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="aff9"><w:name w:val="Title"/></w:style>'
        "</w:styles>"
    ).encode()
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="aff9"/></w:pPr><w:r><w:t>COVER TITLE</w:t></w:r></w:p>'
        '<w:p><w:bookmarkStart w:id="9" w:name="tex2word_section"/>'
        '<w:bookmarkEnd w:id="9"/></w:p>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        "</w:body></w:document>"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", _KEEP_CT)
        z.writestr("_rels/.rels", _KEEP_ROOT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", _KEEP_DOC_RELS)
    result = _keep_convert(tmp_path, r"\section{Intro}Body.", template=buf.getvalue())
    body = etree.fromstring(_part(result.docx, "word/document.xml")).find(f"{{{_W}}}body")
    cover = next(p for p in body.iter(f"{{{_W}}}p")
                 if "COVER TITLE" in "".join(t.text or "" for t in p.iter(f"{{{_W}}}t")))
    pstyle = cover.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
    assert pstyle is not None and pstyle.get(f"{{{_W}}}val") == "Title"  # remapped
    styles_ids = {s.get(f"{{{_W}}}styleId")
                  for s in etree.fromstring(_part(result.docx, "word/styles.xml")).findall(f"{{{_W}}}style")}
    assert "Title" in styles_ids and "aff9" not in styles_ids  # normalised in styles
