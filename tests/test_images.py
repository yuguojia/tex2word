from __future__ import annotations

import io
import struct
import zipfile
import zlib

from conftest import NS
from lxml import etree

from tex2word import convert_file, convert_source, ir
from tex2word.backend import images


def _make_png(path, w, h):
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)


def _make_tiff(path, w, h):
    # minimal little-endian TIFF: header + one IFD with width/height tags.
    header = b"II" + struct.pack("<HI", 42, 8)
    entries = [
        (256, 4, 1, w),   # ImageWidth  (LONG)
        (257, 4, 1, h),   # ImageLength (LONG)
    ]
    ifd = struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        ifd += struct.pack("<HHII", tag, typ, cnt, val)
    ifd += struct.pack("<I", 0)  # next-IFD offset
    path.write_bytes(header + ifd)


def _make_svg(path, w, h, unit="px"):
    path.write_text(
        f'<svg width="{w}{unit}" height="{h}{unit}" '
        f'viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#abc"/></svg>',
        encoding="utf-8",
    )


def test_probe_png_dimensions(tmp_path):
    p = tmp_path / "x.png"
    _make_png(p, 120, 90)
    info = images.probe(str(p))
    assert info is not None
    assert (info.width_px, info.height_px) == (120, 90)
    assert info.embeddable


def test_probe_tiff_dimensions(tmp_path):
    p = tmp_path / "x.tif"
    _make_tiff(p, 2100, 2429)
    info = images.probe(str(p))
    assert info is not None
    assert info.fmt == "tif"
    assert (info.width_px, info.height_px) == (2100, 2429)
    assert info.embeddable  # Word embeds TIFF like a raster


def test_probe_svg_dimensions(tmp_path):
    p = tmp_path / "x.svg"
    _make_svg(p, 200, 160)
    info = images.probe(str(p))
    assert info is not None
    assert info.fmt == "svg"
    assert (info.width_px, info.height_px) == (200, 160)
    assert not info.embeddable  # SVG goes through the vector-blip path


def test_probe_svg_viewbox_fallback(tmp_path):
    p = tmp_path / "x.svg"
    p.write_text(
        '<svg viewBox="0 0 152 123" xmlns="http://www.w3.org/2000/svg"></svg>',
        encoding="utf-8",
    )
    info = images.probe(str(p))
    assert info is not None and (info.width_px, info.height_px) == (152, 123)


def test_probe_svg_pt_units_to_px(tmp_path):
    p = tmp_path / "x.svg"
    _make_svg(p, 72, 72, unit="pt")  # 72pt == 96px at 96 dpi
    info = images.probe(str(p))
    assert info is not None and (info.width_px, info.height_px) == (96, 96)


def test_probe_missing_file():
    assert images.probe("/nonexistent/x.png") is None


def test_emu_scales_down_wide_image():
    info = images.ImageInfo("png", 4000, 2000)
    cx, cy = images.emu_size(info)
    max_w = int(6.0 * images.EMU_PER_INCH)
    assert cx == max_w  # clamped to text width
    assert abs(cy / cx - 2000 / 4000) < 0.01  # aspect ratio preserved


def test_emu_keeps_small_image_unscaled():
    info = images.ImageInfo("png", 96, 48)
    cx, cy = images.emu_size(info)
    assert cx == 96 * images.EMU_PER_PX
    assert cy == 48 * images.EMU_PER_PX


def test_figure_embeds_image(tmp_path):
    _make_png(tmp_path / "fig.png", 80, 60)
    tex = tmp_path / "main.tex"
    tex.write_text(
        r"\begin{document}\begin{figure}\includegraphics{fig.png}"
        r"\caption{C}\label{fig:x}\end{figure}\end{document}",
        encoding="utf-8",
    )
    _, result = convert_file(str(tex))
    zf = zipfile.ZipFile(io.BytesIO(result.docx))
    assert "word/media/image1.png" in zf.namelist()
    rels = zf.read("word/_rels/document.xml.rels").decode()
    assert "media/image1.png" in rels
    root = etree.fromstring(zf.read("word/document.xml"))
    assert root.xpath("//w:drawing", namespaces=NS)
    assert root.xpath(
        "//a:blip", namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    )


def test_tiff_embeds_as_plain_blip(tmp_path):
    _make_tiff(tmp_path / "fig.tif", 800, 600)
    tex = tmp_path / "main.tex"
    tex.write_text(
        r"\begin{document}\includegraphics{fig.tif}\end{document}", encoding="utf-8"
    )
    _, result = convert_file(str(tex))
    zf = zipfile.ZipFile(io.BytesIO(result.docx))
    assert "word/media/image1.tif" in zf.namelist()
    ctypes = zf.read("[Content_Types].xml").decode()
    assert 'Extension="tif" ContentType="image/tiff"' in ctypes
    assert "media/image1.tif" in zf.read("word/_rels/document.xml.rels").decode()
    root = etree.fromstring(zf.read("word/document.xml"))
    # plain raster blip, no SVG extension
    assert root.xpath("//a:blip", namespaces=_A_NS)
    assert not root.xpath("//asvg:svgBlip", namespaces=_SVG_NS)


def test_svg_embeds_with_svgblip_extension(tmp_path):
    _make_svg(tmp_path / "fig.svg", 200, 160)
    tex = tmp_path / "main.tex"
    tex.write_text(
        r"\begin{document}\includegraphics{fig.svg}\end{document}", encoding="utf-8"
    )
    _, result = convert_file(str(tex))
    zf = zipfile.ZipFile(io.BytesIO(result.docx))
    assert "word/media/image1.svg" in zf.namelist()
    ctypes = zf.read("[Content_Types].xml").decode()
    assert 'Extension="svg" ContentType="image/svg+xml"' in ctypes
    assert "media/image1.svg" in zf.read("word/_rels/document.xml.rels").decode()
    root = etree.fromstring(zf.read("word/document.xml"))
    blip = root.xpath("//a:blip", namespaces=_A_NS)[0]
    svgblip = root.xpath("//asvg:svgBlip", namespaces=_SVG_NS)
    assert svgblip, "SVG must carry an asvg:svgBlip extension"
    # primary blip and svgBlip reference the same SVG part (no PNG fallback)
    assert svgblip[0].get(_R_EMBED) == blip.get(_R_EMBED)
    # extent honours the SVG's declared size at 96 dpi
    ext = root.xpath("//wp:extent", namespaces=_A_NS)[0]
    assert ext.get("cx") == str(200 * images.EMU_PER_PX)


def test_missing_image_degrades_gracefully(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text(
        r"\begin{document}\includegraphics{nope.png}\end{document}", encoding="utf-8"
    )
    _, result = convert_file(str(tex))
    assert any("includegraphics" in w.construct for w in result.report.warnings)
    # no media part, but no crash
    assert not result.document.labels


# -- SPRINT-V4-6/7: \includegraphics options + alt-text ---------------------- #

from tex2word.frontend import parse_document  # noqa: E402

_A_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
         "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
_SVG_NS = {"asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main"}
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


def _convert(tmp_path, body):
    _make_png(tmp_path / "pic.png", 100, 50)
    tex = tmp_path / "main.tex"
    tex.write_text(rf"\begin{{document}}{body}\end{{document}}", encoding="utf-8")
    _, result = convert_file(str(tex))
    return result


def test_width_option_sets_extent(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[width=2in]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    ext = root.xpath("//wp:extent", namespaces=_A_NS)[0]
    assert ext.get("cx") == str(2 * 914400)           # 2 inch
    assert ext.get("cy") == str(914400)               # aspect-preserved (50/100)


def test_scale_option(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[scale=0.5]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    ext = root.xpath("//wp:extent", namespaces=_A_NS)[0]
    # 100px*9525*0.5 = 476250 ; 50px*9525*0.5 = 238125
    assert ext.get("cx") == str(int(100 * images.EMU_PER_PX * 0.5))


def test_angle_sets_rotation(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[angle=90]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    xfrm = root.xpath("//a:xfrm", namespaces=_A_NS)[0]
    assert xfrm.get("rot") == str(270 * 60000)        # 90deg CCW -> 270deg CW


def test_trim_clip_sets_srcrect(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[trim=10pt 0pt 10pt 0pt,clip]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    sr = root.xpath("//a:srcRect", namespaces=_A_NS)
    assert sr and int(sr[0].get("l")) > 0 and int(sr[0].get("r")) > 0


def test_alt_text_on_docpr(tmp_path):
    res = _convert(tmp_path, r"\includegraphics{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    docpr = root.xpath("//wp:docPr", namespaces=_A_NS)[0]
    assert docpr.get("descr") == "pic.png"


def test_options_parsed_into_ir():
    doc, _ = parse_document(
        r"\includegraphics[width=3cm,angle=45,scale=2]{a.png}", "."
    )
    img = doc.blocks[0].image
    assert img is not None
    assert img.angle == 45.0 and img.scale == 2.0
    assert img.width is not None and round(img.width) == round(3 * 914400 / 2.54)
    assert img.alt == "a.png"


# -- code-review regressions ------------------------------------------------- #


def test_relative_linewidth_width_is_not_fixed(tmp_path):
    # \linewidth-relative width must fall back to fit-to-column, NOT 0.5pt
    from tex2word import ir as _ir
    doc, _ = parse_document(r"\includegraphics[width=0.5\linewidth]{x.png}", ".")
    fig = next(b for b in doc.blocks if isinstance(b, _ir.Figure))
    assert fig.image.width is None  # not 0.5pt


def test_trim_without_clip_does_not_crop(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[trim=10pt 0pt 10pt 0pt]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    assert not root.xpath("//a:srcRect", namespaces=_A_NS)  # no clip -> no crop


def test_trim_with_clip_crops(tmp_path):
    res = _convert(tmp_path, r"\includegraphics[trim=10pt 0pt 10pt 0pt,clip]{pic.png}")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    assert root.xpath("//a:srcRect", namespaces=_A_NS)


# -- inline images (icons/logos in running text) ----------------------------- #


def test_mid_text_includegraphics_is_inline(tmp_path):
    _make_png(tmp_path / "logo.png", 16, 16)
    doc, rep = parse_document(
        r"See the \includegraphics[height=1em]{logo.png} logo here.", str(tmp_path)
    )
    assert len(doc.blocks) == 1  # one paragraph, not split by a Figure
    kinds = [type(i).__name__ for i in doc.blocks[0].inlines]
    assert kinds == ["Text", "Image", "Text"]
    assert rep.warnings == []


def test_standalone_includegraphics_is_still_a_figure(tmp_path):
    _make_png(tmp_path / "logo.png", 16, 16)
    doc, _ = parse_document(r"\includegraphics{logo.png}", str(tmp_path))
    assert isinstance(doc.blocks[0], ir.Figure)


def test_inline_image_renders_in_paragraph(tmp_path):
    res = _convert(tmp_path, r"text \includegraphics[height=1em]{pic.png} more")
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(res.docx)).read("word/document.xml"))
    para = [p for p in root.iter(f"{{{NS['w']}}}p")
            if p.findall(f".//{{{NS['w']}}}drawing") and p.findall(f".//{{{NS['w']}}}t")]
    assert len(para) == 1  # drawing and text in the same paragraph


def test_inline_image_round_trips(tmp_path):
    from tex2word.roundtrip import recover_ir, to_latex

    _make_png(tmp_path / "pic.png", 16, 16)
    res = convert_source(
        r"\begin{document}a \includegraphics{pic.png} b\end{document}",
        base_dir=str(tmp_path), embed_manifest=True,
    )
    assert recover_ir(res.docx).to_dict() == res.document.to_dict()
    assert r"\includegraphics{pic.png}" in to_latex(res.docx)


def test_foreign_reader_keeps_inline_image_inline(tmp_path):
    from tex2word.frontend.docx_reader import read_docx

    docx = _convert(tmp_path, r"a \includegraphics{pic.png} b").docx
    block = read_docx(docx).blocks[0]
    assert isinstance(block, ir.Paragraph)
    assert any(isinstance(i, ir.Image) for i in block.inlines)
    assert any(isinstance(i, ir.Text) for i in block.inlines)
