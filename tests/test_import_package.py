"""Fidelity: import package \\import{dir}{file} / \\subimport flattening."""

from __future__ import annotations

import struct
import zlib

from tex2word import convert_file, ir
from tex2word.frontend import parse_document
from tex2word.validate import validate_docx


_BIB = "@article{e1905, author={Einstein, A.}, title={On X}, year={1905}}\n"


def _make_png(path, w=2, h=2):
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)


def _text(doc) -> str:
    out = []

    def walk(inlines):
        for n in inlines:
            if type(n).__name__ == "Text":
                out.append(n.value)
            elif hasattr(n, "inlines"):
                walk(n.inlines)

    for b in doc.blocks:
        if hasattr(b, "inlines"):
            walk(b.inlines)
    return " ".join(out)


def test_import_inlines_file_from_dir(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "ch1.tex").write_text("Chapter one body.\n")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\import{chapters/}{ch1.tex}\end{document}"
    )
    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)
    t = _text(res.document)
    assert "Chapter one body." in t
    assert "import" not in t  # the macro must not leak as text
    assert validate_docx(res.docx) == []


def test_subimport_and_nested_input(tmp_path):
    sub = tmp_path / "parts"
    sub.mkdir()
    (sub / "a.tex").write_text(r"Part A then \input{b}.")
    (sub / "b.tex").write_text("nested B.")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\subimport{parts/}{a.tex}\end{document}"
    )
    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)
    t = _text(res.document)
    assert "Part A" in t and "nested B." in t


def test_missing_import_is_dropped(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}Before \import{x/}{gone.tex} after."
        r"\end{document}"
    )
    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)
    assert validate_docx(res.docx) == []
    assert "gone" not in _text(res.document)


def test_import_rewrites_texwordtemplate_relative_to_imported_file(tmp_path):
    sub = tmp_path / "chapters"
    sub.mkdir()
    (sub / "ch1.tex").write_text(r"\texwordtemplate{tpl.docx}Chapter one.")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\import{chapters/}{ch1.tex}\end{document}"
    )
    source = (tmp_path / "main.tex").read_text()

    doc, _ = parse_document(source, str(tmp_path))

    assert doc.meta.template_doc == "chapters/tpl.docx"


def test_import_rewrites_includegraphics_relative_to_imported_file(tmp_path):
    sub = tmp_path / "chapters"
    sub.mkdir()
    _make_png(sub / "fig.png")
    (sub / "ch1.tex").write_text(r"\includegraphics{fig.png}")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\import{chapters/}{ch1.tex}\end{document}"
    )

    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)

    fig = next(b for b in res.document.blocks if isinstance(b, ir.Figure))
    assert fig.image is not None
    assert fig.image.path == "chapters/fig.png"
    assert not any(w.construct == "includegraphics" for w in res.report.warnings)
    assert validate_docx(res.docx) == []


def test_import_rewrites_bib_resources_relative_to_imported_file(tmp_path):
    sub = tmp_path / "chapters"
    sub.mkdir()
    (sub / "refs.bib").write_text(_BIB, encoding="utf-8")
    (sub / "setup.tex").write_text(r"\addbibresource{refs.bib}", encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{biblatex}\import{chapters/}{setup.tex}"
        r"\begin{document}Text \cite{e1905}.\printbibliography\end{document}",
        encoding="utf-8",
    )

    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)

    bib = next(b for b in res.document.blocks if isinstance(b, ir.Bibliography))
    assert any(item.id == "e1905" for item in bib.entries)
    assert not any(w.construct == "\\bibliography" for w in res.report.warnings)


def test_nested_import_rewrites_resource_from_nested_directory(tmp_path):
    sub = tmp_path / "chapters"
    nested = sub / "sections"
    nested.mkdir(parents=True)
    (sub / "ch1.tex").write_text(r"\subimport{sections/}{s1.tex}")
    (nested / "s1.tex").write_text(r"\texwordtemplate{tpl.docx}")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\import{chapters/}{ch1.tex}\end{document}"
    )
    source = (tmp_path / "main.tex").read_text()

    doc, _ = parse_document(source, str(tmp_path))

    assert doc.meta.template_doc == "chapters/sections/tpl.docx"


def test_import_rewrites_local_usepackage_relative_to_imported_file(tmp_path):
    sub = tmp_path / "chapters"
    sub.mkdir()
    (sub / "mymac.sty").write_text(r"\newcommand{\ImportedMacro}{Imported text}")
    (sub / "setup.tex").write_text(r"\usepackage{mymac}")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\import{chapters/}{setup.tex}"
        r"\begin{document}\ImportedMacro\end{document}"
    )

    _, res = convert_file(str(tmp_path / "main.tex"), str(tmp_path / "out.docx"),
                          embed_manifest=False)

    assert "Imported text" in _text(res.document)
