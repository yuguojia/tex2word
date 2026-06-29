"""Round-trip support: persist the IR as a manifest inside the .docx.

The PRD makes the IR the round-trip linchpin: embedding it (plus the original
LaTeX retained on each math/figure node, the label->bookmark map, and citation
keys) lets a future Word->LaTeX reader reconcile edits against the source
instead of regenerating from scratch.

V1 ships the *write* side (this manifest) and a reader that recovers the IR
from a generated .docx. The full OOXML->IR reverse path is post-V1.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from . import ir
from .backend.package import MANIFEST_PART

MANIFEST_VERSION = 1


def _generated_timestamp() -> str:
    """The manifest ``generated`` time. Honours ``SOURCE_DATE_EPOCH`` (the
    reproducible-builds convention) so a fixed value yields byte-identical
    output; otherwise the current UTC time."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.strip().isdigit():
        return datetime.fromtimestamp(int(epoch), UTC).replace(microsecond=0).isoformat()
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def build_manifest(doc: ir.Document) -> bytes:
    """Serialise the IR + provenance into the manifest JSON bytes."""
    payload = {
        "tool": "tex2word",
        "manifest_version": MANIFEST_VERSION,
        "generated": _generated_timestamp(),
        "labels": {
            key: {"bookmark": info.bookmark, "kind": info.kind, "counter": info.counter_name}
            for key, info in doc.labels.items()
        },
        "ir": doc.to_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def read_manifest(docx_bytes: bytes) -> dict | None:
    """Return the parsed manifest payload from a .docx, or None if absent."""
    zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    if MANIFEST_PART not in zf.namelist():
        return None
    return json.loads(zf.read(MANIFEST_PART).decode("utf-8"))


def recover_ir(docx_bytes: bytes) -> ir.Document | None:
    """Reconstruct the IR :class:`~tex2word.ir.Document` from the manifest."""
    payload = read_manifest(docx_bytes)
    if payload is None or "ir" not in payload:
        return None
    return ir.Document.from_dict(payload["ir"])


def to_latex(
    docx_bytes: bytes, reconcile: bool = True,
    kept: list[KeptManifestBlock] | None = None,
    annotate: bool = False,
    ignore_manifest: bool = False,
) -> str | None:
    """Convert a ``.docx`` back to LaTeX.

    Prefers the embedded tex2word manifest (exact IR, original math/figure
    source). Pass ``ignore_manifest=True`` to force the foreign-docx reader even
    when a manifest is embedded. Otherwise, with ``reconcile=True`` (the default)
    the manifest IR is merged with the freshly-read document IR by a
    **manifest-biased anchored merge** (:func:`reconcile_blocks`): an unedited
    document reconciles to *identity* (byte-for-byte the original LaTeX), while
    prose edited in Word is picked up; a lossless manifest block is never
    replaced by its lossy read-back. Pass ``reconcile=False`` to emit the
    manifest verbatim and ignore the body. For a *foreign* ``.docx`` (no
    manifest) it always reads ``document.xml`` structurally. Returns ``None``
    only if the document can't be read at all.

    Pass a list as ``kept`` to collect the manifest blocks reconcile retained
    verbatim inside an edited region (stale-risk content to proof-read); pass
    ``annotate=True`` to also flag those spots with a ``%`` comment in the .tex.

    See ``reconcile/`` for the investigation, the Go/No-go, and the design.
    """
    from .backend.latex_writer import write_latex
    from .frontend.docx_reader import read_docx

    if ignore_manifest:
        try:
            return write_latex(read_docx(docx_bytes))
        except Exception:
            return None
    manifest_doc = recover_ir(docx_bytes)
    if manifest_doc is None:
        try:
            return write_latex(read_docx(docx_bytes))
        except Exception:
            return None
    if not reconcile:
        return write_latex(manifest_doc)
    # map sanitised bookmarks back to the original label keys for the read-back
    label_map = {info.bookmark: key for key, info in manifest_doc.labels.items()}
    try:
        current = read_docx(docx_bytes, label_map=label_map)
    except Exception:
        return write_latex(manifest_doc)
    merged = reconcile_blocks(manifest_doc.blocks, current.blocks, kept=kept, annotate=annotate)
    # keep every document-level attribute from the manifest (meta, labels, the
    # book flag, ...) -- only the block list is reconciled.
    merged_doc = replace(manifest_doc, blocks=merged)
    return write_latex(merged_doc)


# --------------------------------------------------------------------------- #
# Block-level reconciliation (manifest = exact source, current = Word edits)
# --------------------------------------------------------------------------- #

_WS = re.compile(r"[\s{}]+")

# Inline math, citations and refs are recovered by the docx reader in a form that
# is *textually* different from the manifest (OMML->LaTeX synonyms, the rendered
# "[1]" of a citation, sanitised bookmark keys), even when nothing was edited. So
# a block's reconcile signature is built from its **stable prose only** -- the
# text the reader reproduces verbatim -- so an unedited block still matches.


def _prose_text(inlines: list) -> str:
    """Stable prose: ``Text`` (through styling wrappers), excluding inline math,
    citations, refs and footnotes (which the reader renders differently)."""
    out: list[str] = []
    for n in inlines:
        if isinstance(n, ir.Text):
            out.append(n.value)
        elif isinstance(n, ir.Emphasis | ir.CharStyle | ir.Link | ir.Colored | ir.FontSize):
            out.append(_prose_text(n.inlines))
    return "".join(out)


# A typeset static citation reads back as literal "[1]" / "[1, 2]" / "[1, p. 5]"
# text and an empty \cite/\ref as "()"; the manifest holds an ir.Cite/ir.Ref
# (excluded from the prose key), so strip these artifacts (any bracket group that
# starts with a digit, plus empty parens) to keep an unedited cite paragraph
# signature-stable.
_CITE_ARTIFACT = re.compile(r"\[\d[^\]]*\]|\(\s*\)")


def _norm(s: str) -> str:
    s = _CITE_ARTIFACT.sub("", s)
    return _WS.sub("", s).lower().replace(":", "").replace("_", "")


_MATH_SYNONYMS = [
    (r"\displaystyle", ""), (r"\rightarrow", r"\to"), (r"\colon", ":"),
    (r"\begin{matrix}", ""), (r"\end{matrix}", ""),
    (r"\left\|", r"\left|"), (r"\right\|", r"\right|"), (r"\|", "|"),
]


def _math_sig(latex: str) -> str:
    """A display-math fingerprint stable across the reader's OMML->LaTeX spelling
    (``\\to``/``\\rightarrow``, dropped ``\\displaystyle``, ``matrix`` scaffolding)."""
    for a, b in _MATH_SYNONYMS:
        latex = latex.replace(a, b)
    return _norm(latex)


def _block_signature(block: ir.Block) -> tuple[str, str]:
    """A normalised (kind, text) key robust to lossy round-tripping.

    Built from stable prose / a synonym-folded math fingerprint so an *unchanged*
    block matches between the exact manifest IR and the lossy document-read IR;
    blocks the reader can't represent (e.g. ``Bibliography``) fall back to a
    type-only key so they align by position rather than spuriously mismatch.
    """
    if isinstance(block, ir.MathBlock):
        return ("math", _math_sig(block.latex))
    if isinstance(block, ir.Heading):
        return (f"h{block.level}", _norm(_prose_text(block.inlines)))
    if isinstance(block, ir.Paragraph):
        return ("p", _norm(_prose_text(block.inlines)))
    if isinstance(block, ir.ItemList):
        text = " ".join(
            _prose_text(b.inlines)
            for it in block.items for b in it.blocks if isinstance(b, ir.Paragraph)
        )
        return ("list", _norm(text))
    if isinstance(block, ir.Table):
        text = " ".join(
            _prose_text(b.inlines)
            for r in block.rows for c in r.cells for b in c.blocks
            if isinstance(b, ir.Paragraph)
        )
        return ("table", _norm(text))
    if isinstance(block, ir.Figure):
        # type-only: the reader can recover neither the image *path* (manifest- /
        # alt-text-only, often stripped by Word/WPS) nor reliably the caption (its
        # paragraph may use a localised "Caption" style the reader misses), so any
        # content key would mismatch every unedited figure and drag its neighbour
        # paragraphs into a kept-manifest region. Align figures by position.
        return ("figure", "")
    if isinstance(block, ir.PageBreak):
        return ("pagebreak", block.command)
    return (type(block).__name__, "")


@dataclass
class KeptManifestBlock:
    """A manifest block reconcile retained verbatim inside a region Word *edited*.

    These are the blocks where a Word edit could not be merged safely, so the
    (possibly stale) manifest text was kept. Surfaced so the author can hand-check
    them against the Word document. ``snippet`` is a short prose/caption excerpt
    and ``reason`` explains why the manifest was preferred.
    """

    kind: str
    snippet: str
    reason: str


def _kept_kind(block: ir.Block) -> str:
    return {
        ir.Paragraph: "paragraph", ir.Heading: "heading", ir.Figure: "figure",
        ir.Table: "table", ir.MathBlock: "equation",
    }.get(type(block), type(block).__name__)


def _kept_snippet(block: ir.Block) -> str:
    """A short human-readable excerpt of a block, for the kept-blocks report."""
    if isinstance(block, ir.Paragraph | ir.Heading):
        return _prose_text(block.inlines)[:60]
    if isinstance(block, ir.MathBlock):
        return block.latex[:60]
    if isinstance(block, ir.Figure):
        return _prose_text(block.caption or [])[:60] or (
            block.image.path if block.image else "")
    if isinstance(block, ir.Table):
        for r in block.rows:
            for c in r.cells:
                for b in c.blocks:
                    if isinstance(b, ir.Paragraph) and _prose_text(b.inlines):
                        return _prose_text(b.inlines)[:60]
    return ""


#: LaTeX-comment markers injected when ``annotate=True`` so the recovered .tex
#: flags reconcile decisions the author should hand-check.
#: format template -- ``{kind}``/``{reason}`` mirror the matching
#: ``reconcile_report.json`` entry so each kept block's inline comment explains
#: *which* block was kept and *why* (not just that one was).
_KEPT_NOTE = (
    "% [tex2word] {kind} kept verbatim from the manifest — {reason}; "
    "please proof-read this block against the .docx"
)
_DROPPED_NOTE = (
    "% [tex2word] Word inserted {n} block(s) here that could not be recognised "
    "(non-prose structure) — skipped"
)


def reconcile_blocks(
    original: list[ir.Block], current: list[ir.Block],
    kept: list[KeptManifestBlock] | None = None,
    annotate: bool = False,
) -> list[ir.Block]:
    """Merge the exact ``original`` (manifest) blocks with ``current`` (edited).

    **Manifest-biased anchored merge** — both signature-stable (an unedited
    document reconciles to identity) and edit-safe (a lossless manifest block is
    never replaced by its lossy read-back). Per ``difflib`` opcode:

    - ``equal`` → the exact ``original`` block (with any review *comments* the
      reviewer added to the matching read-back paragraph grafted on, so notes on
      otherwise-unchanged text survive).
    - ``replace`` of paragraphs by paragraphs → reconcile **pairwise** (1:1, and
      N:N too): each pure-prose paragraph takes ``current``'s text, each mixed
      paragraph is inline-merged (exact manifest math/footnote/image kept). An
      N:M paragraph run (a genuine split/merge) takes ``current`` when the whole
      manifest run is pure prose, else keeps the lossless manifest.
    - any ``replace`` touching a non-paragraph block (math/table/figure/
      bibliography) → keep ``original`` — the lossless side; an edit *inside* such
      a block can't be recovered faithfully anyway.
    - ``insert`` → take an insertion only when the whole run is paragraphs (a
      genuine prose insertion); a run mixing a non-paragraph block in is a lossy
      read-back artifact (bibliography entries, figure-as-table, an align-split
      equation + its "where …" continuation) and is dropped.
    - ``delete`` → drop paragraphs the user removed; keep non-paragraph manifest
      blocks (a "missing" complex block is a reader miss, not a deletion).

    When ``kept`` is given, every manifest block retained verbatim *inside an
    edited region* is appended to it (a :class:`KeptManifestBlock`) so callers can
    flag stale-risk content for manual proof-reading. With ``annotate=True`` the
    same spots get an inline ``%`` comment in the recovered .tex (before each block
    kept verbatim, and where an unrecognised Word insertion was skipped).

    See ``reconcile/`` for the investigation and the design rationale.
    """
    import difflib

    def _is_para(b: ir.Block) -> bool:
        return isinstance(b, ir.Paragraph)

    def _pure_prose(b: ir.Block) -> bool:
        # a paragraph whose read-back is trustworthy: no inline the reader renders
        # differently (math, citations, cross-refs, footnotes, images). Those
        # paragraphs are kept from the manifest until inline reconcile lands.
        return isinstance(b, ir.Paragraph) and not _has_unreliable_inline(b.inlines)

    def _keep(out: list[ir.Block], block: ir.Block, reason: str) -> None:
        """Keep a manifest *block* verbatim: record it, optionally annotate, emit."""
        if kept is not None:
            kept.append(KeptManifestBlock(_kept_kind(block), _kept_snippet(block), reason))
        if annotate:
            note = _KEPT_NOTE.format(kind=_kept_kind(block), reason=reason)
            out.append(ir.RawPassthrough(latex=note))
        out.append(block)

    osig = [_block_signature(b) for b in original]
    csig = [_block_signature(b) for b in current]
    matcher = difflib.SequenceMatcher(a=osig, b=csig, autojunk=False)
    out: list[ir.Block] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        orig_run, cur_run = original[i1:i2], current[j1:j2]
        if tag == "equal":
            out.extend(
                _reconcile_equal(o, c, _pure_prose)
                for o, c in zip(orig_run, cur_run, strict=True)
            )
        elif tag == "delete":
            for b in orig_run:
                if not _is_para(b):
                    _keep(out, b, "manifest-only block with no Word match — verify it "
                                  "wasn't deleted (the reader may simply have missed it)")
        elif tag == "insert":
            # a genuine prose insertion is paragraphs-only; a run mixing in a
            # non-paragraph block is a lossy read-back artifact -> drop it (but flag
            # the spot so the author knows Word content was skipped here).
            if cur_run and all(_is_para(b) for b in cur_run):
                out.extend(cur_run)
            elif cur_run and annotate:
                out.append(ir.RawPassthrough(latex=_DROPPED_NOTE.format(n=len(cur_run))))
        elif all(_is_para(b) for b in orig_run) and all(_is_para(b) for b in cur_run):
            # all-paragraph replace -> reconcile pairwise so edits to *adjacent*
            # paragraphs (an N:N run) are each picked up, not dropped wholesale.
            _reconcile_para_run(out, orig_run, cur_run, _pure_prose, _keep)
        else:
            for b in orig_run:  # mixed/lossy region: keep the lossless manifest
                _keep(out, b, "Word edited a region containing non-prose content — "
                              "kept the exact manifest block")
    return out


def _reconcile_para_run(
    out: list[ir.Block], orig_run: list[ir.Block], cur_run: list[ir.Block],
    pure_prose, keep,
) -> None:
    """Reconcile an all-paragraph ``replace`` run (manifest vs Word) into ``out``.

    Equal counts pair 1:1 (pure prose -> Word's text; mixed -> inline merge);
    an N:M run (a paragraph split/merge) takes Word's shape only when the whole
    manifest run is pure prose, else keeps the lossless manifest."""
    if len(orig_run) == len(cur_run):
        for o, c in zip(orig_run, cur_run, strict=True):
            if pure_prose(o):
                out.append(c)  # pure-prose paragraph: take Word's edited text
            else:
                merged = _reconcile_inline(o, c)
                if merged is o:  # inline merge couldn't apply -> manifest kept
                    keep(out, o, "Word edited a paragraph with math/citations/refs "
                                 "that can't be merged safely — kept the manifest")
                else:
                    out.append(merged)  # prose edit applied, manifest semantics kept
        return
    if all(pure_prose(b) for b in orig_run):
        out.extend(cur_run)  # pure-prose restructure: trust Word's new paragraphs
        return
    for b in orig_run:
        keep(out, b, "Word restructured a region containing non-prose content — "
                     "kept the exact manifest block")


# inline nodes whose exact manifest form we preserve and which inject no prose of
# their own when rendered (so prose segments line up across manifest / read-back).
# Citations are anchors too: a CSL/Zotero field reads back as an ``ir.Cite`` node
# at the same position with no injected prose, so it lines up; its *content* (the
# keys) is always taken from the manifest, never the lossy read-back.
_MERGE_SEMANTIC = (
    ir.Math, ir.DisplayMath, ir.Footnote, ir.Endnote, ir.Image, ir.Cite,
)
# inline nodes that inject prose on read-back (a cleveref ``\cref`` renders a literal
# "fig. "/"Theorem " prefix) or that the reader can't represent faithfully -> we
# can't anchor on them, so keep the whole manifest paragraph.
_INLINE_OPAQUE = (ir.Ref, ir.RawInline, ir.IndexEntry)


def _reconcile_inline(original: ir.Block, current: ir.Block) -> ir.Block:
    """Merge a prose edit into a mixed paragraph, keeping exact manifest semantics.

    Splits both paragraphs at their semantic nodes (math, footnotes, images,
    citations, cross-refs). If the skeletons line up by *type* and count, each
    prose segment is taken from ``current`` when its text changed -- a real edit,
    **whitespace included** (so deleting the spaces between CJK and Latin is picked
    up) -- and kept from the manifest otherwise; the semantic nodes are always the
    manifest's exact ones (their content -- math spelling, cite keys, ref targets
    -- never round-trips through the lossy read-back). A reorder/insert/delete of a
    semantic node (count/type mismatch) or an unrepresentable inline (raw/index) ->
    keep the manifest paragraph. Guarantees an unedited paragraph -> itself.
    """
    if not (isinstance(original, ir.Paragraph) and isinstance(current, ir.Paragraph)):
        return original
    if _contains(original.inlines, _INLINE_OPAQUE) or _contains(current.inlines, _INLINE_OPAQUE):
        return original
    o_prose, o_sem = _split_semantic(original.inlines)
    c_prose, c_sem = _split_semantic(current.inlines)
    if len(o_sem) != len(c_sem) or len(o_prose) != len(c_prose):
        return original
    if any(type(a) is not type(b) for a, b in zip(o_sem, c_sem, strict=True)):
        return original
    merged: list = []
    changed = False
    for i, o_seg in enumerate(o_prose):
        c_seg = c_prose[i]
        if _prose_text(o_seg) == _prose_text(c_seg):
            merged.extend(o_seg)  # segment unchanged -> exact manifest prose
        else:
            merged.extend(c_seg)  # genuine edit (incl. whitespace) -> Word's prose
            changed = True
        if i < len(o_sem):
            merged.append(o_sem[i])
    if not changed:
        return original  # unedited -> the exact manifest paragraph (identity)
    return replace(original, inlines=merged)


def _contains(inlines: list, types: tuple) -> bool:
    for n in inlines:
        if isinstance(n, types):
            return True
        if isinstance(n, ir.Emphasis | ir.CharStyle | ir.Link | ir.Colored | ir.FontSize) \
                and _contains(n.inlines, types):
            return True
    return False


def _split_semantic(inlines: list) -> tuple[list[list], list]:
    """Partition inlines into prose segments around top-level semantic nodes.

    Returns ``(prose_segments, semantic_nodes)`` with
    ``len(prose_segments) == len(semantic_nodes) + 1``."""
    segments: list[list] = [[]]
    sem: list = []
    for n in inlines:
        if isinstance(n, _MERGE_SEMANTIC):
            sem.append(n)
            segments.append([])
        else:
            segments[-1].append(n)
    return segments, sem


_UNRELIABLE = (
    ir.Math, ir.DisplayMath, ir.Cite, ir.Ref, ir.Footnote, ir.Endnote, ir.Image,
    ir.RawInline, ir.IndexEntry,
)


def _has_unreliable_inline(inlines: list) -> bool:
    """True if any inline (recursively) is one the docx reader renders differently
    (math/citations/cross-refs/footnotes/images) -- making prose-edit pickup unsafe."""
    for n in inlines:
        if isinstance(n, _UNRELIABLE):
            return True
        if isinstance(n, ir.Emphasis | ir.CharStyle | ir.Link | ir.Colored | ir.FontSize) \
                and _has_unreliable_inline(n.inlines):
            return True
    return False


def _reconcile_equal(
    original: ir.Block, current: ir.Block, pure_prose,
) -> ir.Block:
    """Reconcile a signature-matched (``equal``) block pair.

    The reconcile signature ignores whitespace (so the reader's lossy re-spacing
    around math/citations isn't mistaken for an edit), so a *genuine* whitespace-
    only edit -- e.g. deleting the spaces between CJK and Latin/digits, common in
    Chinese typesetting -- lands here, not in a ``replace``. When the raw prose
    actually differs it is a real Word edit: a pure-prose paragraph takes Word's
    text; a mixed paragraph (math/citations) is inline-merged so the edit applies
    while the manifest's exact semantics are kept. Otherwise the exact manifest
    block is kept (with the reviewer's comments grafted on either way).
    """
    if (isinstance(original, ir.Paragraph) and isinstance(current, ir.Paragraph)
            and _prose_text(original.inlines) != _prose_text(current.inlines)):
        if pure_prose(original):
            return _graft_comments(current, original)  # whitespace/verbatim edit picked up
        merged = _reconcile_inline(original, current)
        if merged is not original:
            return _graft_comments(merged, original)  # edit applied, semantics kept
    return _graft_comments(original, current)


def _graft_comments(original: ir.Block, current: ir.Block) -> ir.Block:
    """Keep the exact ``original`` block, but carry over any review ``Comment``s the
    reviewer left on the matching ``current`` paragraph (notes on unchanged text)."""
    if not (isinstance(original, ir.Paragraph) and isinstance(current, ir.Paragraph)):
        return original
    # dedup by text: a comment the manifest already carries (e.g. our own \todo)
    # must not be added twice when the read-back recovers it from comments.xml.
    have = {n.text for n in original.inlines if isinstance(n, ir.Comment)}
    notes = [
        n for n in current.inlines
        if isinstance(n, ir.Comment) and n.text not in have
    ]
    if not notes:
        return original  # comment-free (or already present) -> identity preserved
    return replace(original, inlines=[*original.inlines, *notes])
