"""SPRINT-V3 A2/M4: block-level reconciliation of Word edits vs the manifest."""

from __future__ import annotations

import io
import zipfile

from tex2word import convert_source, ir
from tex2word.roundtrip import _prose_text, reconcile_blocks, to_latex

SRC = r"""\begin{document}
\section{Intro}
The first paragraph is unchanged.
\begin{equation}E = mc^2\end{equation}
The second paragraph stays too.
\end{document}"""


def _edit_docx(docx: bytes, old: str, new: str) -> bytes:
    """Simulate a Word edit by patching document.xml (manifest left stale)."""
    zin = zipfile.ZipFile(io.BytesIO(docx))
    parts = {n: zin.read(n) for n in zin.namelist()}
    parts["word/document.xml"] = parts["word/document.xml"].decode().replace(old, new).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_reconcile_keeps_unchanged_uses_exact_source():
    docx = convert_source(SRC).docx
    edited = _edit_docx(docx, "The first paragraph is unchanged.", "EDITED IN WORD.")
    latex = to_latex(edited, reconcile=True)
    # the edit shows up
    assert "EDITED IN WORD." in latex
    # the untouched equation keeps its EXACT manifest source (not lossy OMML)
    assert "\\begin{equation}\nE = mc^2\n\\end{equation}" in latex
    # the untouched second paragraph survives verbatim
    assert "The second paragraph stays too." in latex


def test_default_is_manifest_faithful():
    # reconcile is ON by default, but an *unedited* docx reconciles to identity,
    # so the output is still the exact manifest LaTeX.
    docx = convert_source(SRC).docx
    plain = to_latex(docx)  # reconcile=True default
    assert "\\begin{equation}\nE = mc^2\n\\end{equation}" in plain
    assert "The first paragraph is unchanged." in plain


def test_ignore_manifest_reads_current_word_body():
    docx = convert_source(r"\begin{document}Original body.\end{document}").docx
    edited = _edit_docx(docx, "Original body.", "Edited in Word.")

    manifest_only = to_latex(edited, reconcile=False)
    direct = to_latex(edited, ignore_manifest=True)

    assert manifest_only is not None and "Original body." in manifest_only
    assert direct is not None and "Edited in Word." in direct
    assert "Original body." not in direct


def test_reconcile_blocks_equal_prefers_original():
    a = ir.Paragraph([ir.Text("same")])
    orig = [ir.Heading(1, [ir.Text("H")]), a]
    # current has a structurally-equal heading + paragraph (different objects)
    cur = [ir.Heading(1, [ir.Text("H")]), ir.Paragraph([ir.Text("same")])]
    merged = reconcile_blocks(orig, cur)
    assert merged[1] is a  # the original (exact) object is kept


def test_reconcile_blocks_takes_insertions_and_drops_deletions():
    orig = [ir.Paragraph([ir.Text("one")]), ir.Paragraph([ir.Text("two")])]
    cur = [
        ir.Paragraph([ir.Text("one")]),
        ir.Paragraph([ir.Text("inserted")]),  # new in Word
        # "two" deleted in Word
    ]
    merged = reconcile_blocks(orig, cur)
    texts = [b.inlines[0].value for b in merged if isinstance(b, ir.Paragraph)]
    assert texts == ["one", "inserted"]


def test_reconcile_picks_up_edits_to_adjacent_paragraphs():
    # an N:N replace (two *adjacent* prose paragraphs both edited) must apply both
    # edits, not drop the whole run and keep the manifest (the deleted-sentence bug).
    orig = [ir.Paragraph([ir.Text("alpha one")]), ir.Paragraph([ir.Text("beta two")])]
    cur = [ir.Paragraph([ir.Text("alpha EDITED")]), ir.Paragraph([ir.Text("beta two three")])]
    merged = reconcile_blocks(orig, cur)
    texts = [b.inlines[0].value for b in merged if isinstance(b, ir.Paragraph)]
    assert texts == ["alpha EDITED", "beta two three"]


def test_reconcile_picks_up_a_whitespace_only_edit_in_prose():
    # editing Chinese prose often just deletes the spaces around Latin/digits. The
    # reconcile signature ignores whitespace (so the reader's re-spacing isn't a
    # false edit), so this lands as an `equal` pair -- but a genuine whitespace-only
    # edit to pure prose must still be taken from Word, not silently dropped.
    orig = [ir.Paragraph([ir.Text("高性能 CNT 沟道 is good")])]
    cur = [ir.Paragraph([ir.Text("高性能CNT沟道 is good")])]
    assert _block_signature(orig[0]) == _block_signature(cur[0])  # matched as "equal"
    merged = reconcile_blocks(orig, cur)
    assert merged[0].inlines[0].value == "高性能CNT沟道 is good"


def test_reconcile_applies_prose_edit_in_a_math_paragraph():
    # a paragraph with inline math, prose expanded in Word: the edit is applied AND
    # the manifest's exact math (not the lossy OMML read-back) is kept.
    orig = [ir.Paragraph([ir.Text("the ratio "), ir.Math(r"I_{\mathrm{D}}"),
                          ir.Text(" is small here")])]
    cur = [ir.Paragraph([ir.Text("the ratio "), ir.Math("{I}_{D}"),
                         ir.Text(" is VERY small here")])]
    merged = reconcile_blocks(orig, cur)
    p = merged[0]
    assert any(isinstance(n, ir.Math) and n.latex == r"I_{\mathrm{D}}" for n in p.inlines)
    assert any(isinstance(n, ir.Text) and "VERY" in n.value for n in p.inlines)


def test_reconcile_applies_whitespace_edit_in_a_cite_paragraph():
    # deleting the spaces around a citation (whitespace-only) is signature-equal but
    # must still be applied, with the manifest's exact cite keys kept.
    orig = [ir.Paragraph([ir.Text("研究 "), ir.Cite(["mykey"]), ir.Text(" 表明此事")])]
    cur = [ir.Paragraph([ir.Text("研究"), ir.Cite(["mykey"]), ir.Text("表明此事")])]
    merged = reconcile_blocks(orig, cur)
    p = merged[0]
    assert any(isinstance(n, ir.Cite) and n.keys == ["mykey"] for n in p.inlines)
    assert _prose_text(p.inlines) == "研究表明此事"  # spaces removed as in Word


def test_annotate_marks_a_kept_block():
    # a cite paragraph whose Word read-back has the cite as plain text "[1]" can't be
    # merged (anchor count mismatch) -> kept; `annotate` flags it inline.
    orig = [ir.Paragraph([ir.Text("a"), ir.Cite(["k"]), ir.Text(" original tail")])]
    cur = [ir.Paragraph([ir.Text("a [1] rewritten tail")])]
    kept: list = []
    merged = reconcile_blocks(orig, cur, kept=kept, annotate=True)
    raws = [b.latex for b in merged if isinstance(b, ir.RawPassthrough)]
    assert any("kept verbatim" in r for r in raws)
    # the inline comment mirrors the kept-blocks report: it names the block kind
    # and carries the same reason, so it explains *which* block and *why*.
    assert kept and any(
        f"{kept[0].kind} kept verbatim" in r and kept[0].reason in r for r in raws
    )


def test_annotate_marks_a_dropped_word_insertion():
    # Word inserted a non-prose structure (a table) the reader can't merge -> dropped;
    # `annotate` leaves a comment at the spot.
    orig = [ir.Paragraph([ir.Text("keep me")])]
    cur = [ir.Paragraph([ir.Text("keep me")]), ir.Table(rows=[])]
    merged = reconcile_blocks(orig, cur, annotate=True)
    raws = [b.latex for b in merged if isinstance(b, ir.RawPassthrough)]
    assert any("could not be recognised" in r for r in raws)


def test_reconcile_reports_kept_manifest_blocks():
    # a paragraph carrying a citation can't be merged on a prose edit -> the
    # manifest is kept, and that fact is surfaced in the `kept` report.
    orig = [ir.Paragraph([ir.Text("see "), ir.Cite(["k"]), ir.Text(" for the original detail")])]
    cur = [ir.Paragraph([ir.Text("see [1] for the rewritten explanation")])]
    kept: list = []
    merged = reconcile_blocks(orig, cur, kept=kept)
    assert merged[0] is orig[0]  # exact manifest paragraph kept (cite unmergeable)
    assert len(kept) == 1 and kept[0].kind == "paragraph"


# -- reconcile-on-by-default: unedited docs must reconcile to identity -------- #

import os  # noqa: E402

import pytest  # noqa: E402

from tex2word.roundtrip import _block_signature, recover_ir  # noqa: E402

_CORPUS = os.path.join(os.path.dirname(__file__), "corpus")
_UAT = os.path.join(os.path.dirname(__file__), "uat")
_IDENTITY_DOCS = [
    os.path.join(_CORPUS, n)
    for n in ("article.tex", "features.tex", "longtable.tex", "macros.tex", "tables.tex")
] + [
    os.path.join(_UAT, "arXiv-2507.17026v2", "main.tex"),
    os.path.join(_UAT, "arXiv-2605.23904v2", "main.tex"),
]


def _reconciled_is_identity(path: str) -> bool:
    """An *unedited* docx must reconcile to its manifest IR exactly (identity)."""
    from tex2word.frontend.docx_reader import read_docx

    docx = convert_source(
        open(path, encoding="utf-8").read(), base_dir=os.path.dirname(path)
    ).docx
    manifest = recover_ir(docx)
    merged = reconcile_blocks(manifest.blocks, read_docx(docx).blocks)
    return [repr(b) for b in merged] == [repr(b) for b in manifest.blocks]


def _doc_id(p: str) -> str:
    return os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p)


@pytest.mark.parametrize("path", _IDENTITY_DOCS, ids=_doc_id)
def test_unedited_docs_reconcile_to_identity(path):
    # the gate behind reconcile-on-by-default: no spurious edits on an unedited doc
    assert _reconciled_is_identity(path), f"{path} does not reconcile to identity"


def test_inline_reconcile_keeps_exact_math_on_a_prose_edit():
    # a prose edit in a paragraph that also has inline math: the edit is applied
    # AND the math keeps its exact manifest LaTeX (not the lossy OMML read-back).
    src = r"\begin{document}The value $x^2+1$ is positive.\end{document}"
    docx = convert_source(src).docx
    edited = _edit_docx(docx, "value", "result")
    latex = to_latex(edited)
    assert "result" in latex and "value" not in latex  # prose edit applied
    assert "x^2+1" in latex  # exact manifest math kept


def test_inline_reconcile_falls_back_to_manifest_for_ref_paragraphs():
    # a paragraph with a cross-ref injects rendered prose on read-back; reconcile
    # must keep the manifest paragraph rather than leak "fig."-style prefixes.
    src = (
        r"\begin{document}\section{S}\label{s}As shown in \ref{s}, it holds."
        r"\end{document}"
    )
    docx = convert_source(src).docx
    latex = to_latex(docx)  # unedited -> identity; \ref preserved, no leaked prose
    assert r"\ref{s}" in latex
    assert "As shown in" in latex


def test_inline_math_synonyms_do_not_count_as_an_edit():
    # a paragraph whose only difference is OMML->LaTeX math spelling matches
    a = ir.Paragraph([ir.Text("the norm "), ir.Math(r"\left\|x\right\|"), ir.Text(" is ok")])
    b = ir.Paragraph([ir.Text("the norm "), ir.Math(r"\left|x\right|"), ir.Text(" is ok")])
    assert _block_signature(a) == _block_signature(b)
    merged = reconcile_blocks([a], [b])
    assert merged[0] is a  # the exact manifest paragraph (with original math) is kept
