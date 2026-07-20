"""IR -> ``word/document.xml``.

Walks the IR and emits OOXML, dropping to raw math (OMML) and field plumbing as
needed. Math uses the direct LaTeX->OMML path; on failure it degrades to a raw
text run and records the fallback in the :class:`ConversionReport` (never an
abort).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import Any

from lxml import etree

from .. import ir
from ..mathml import omml
from ..mathml.cascade import ImageMathRenderer, MathCascade
from ..report import ConversionReport
from . import fields, images, raster
from .caption_config import CaptionConfig
from .numbering import NumIds
from .ooxml import el, preserve_space, qn, serialize, sub, text_el

_Element = etree._Element

_HEADING_STYLE = {
    0: "Title", 1: "Heading1", 2: "Heading2", 3: "Heading3", 4: "Heading4",
    5: "Heading5",
}
# multi-line display envs that number each line -> keep per-line, don't collapse
_MULTILINE_NUMBERED_ENVS = {"align", "alignat", "flalign", "eqnarray"}
# Word TOC field instructions per \tableofcontents / \listoffigures / \listoftables.
# \o "1-3" = outline levels; \h = hyperlinks; \z = hide leaders in web view;
# \u = use applied outline levels; \c "Figure" = build from the SEQ Figure captions.
_TOC_SPEC = {
    "contents": ("Contents", 'TOC \\o "1-3" \\h \\z \\u'),
    "figures": ("List of Figures", 'TOC \\h \\z \\c "Figure"'),
    "tables": ("List of Tables", 'TOC \\h \\z \\c "Table"'),
}
_PIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
#: w:sdt tags marking regions for faithful round-trip recovery.
BIB_SDT_TAG = "tex2word:bibliography"
FIG_SDT_TAG = "tex2word:figure"


class DocumentWriter:
    def __init__(
        self,
        report: ConversionReport,
        base_dir: str = ".",
        image_math_renderer: ImageMathRenderer | None = None,
        number_by_section: bool = False,
        citation_mode: str = "static",
        columns: int = 1,
        page_pgsz: dict[str, str] | None = None,
        page_pgmar: dict[str, str] | None = None,
        header_footer_refs: list[tuple[str, str, str]] | None = None,
        preamble: str = "",
        appendix_style_ids: list[str | None] | None = None,
        part_style_id: str | None = None,
        figure_style_id: str | None = None,
        caption_style_ids: dict[str, str | None] | None = None,
        theorem_style_ids: dict[str, str] | None = None,
        table_text_style_id: str | None = None,
        threeline_table_style_id: str | None = None,
        body_style_id: str | None = None,
        first_body_style_id: str | None = None,
        star_heading_style_ids: list[str | None] | None = None,
        style_remap: dict[str, str] | None = None,
        par_style_names: dict[str, str] | None = None,
        char_style_names: dict[str, str] | None = None,
        caption_config: CaptionConfig | None = None,
        cjk_quote_hint: bool = False,
        num_ids: NumIds | None = None,
        style_numbering: bool = False,
        bullet_list_style_id: str | None = None,
        number_list_style_id: str | None = None,
    ) -> None:
        self.report = report
        self.base_dir = base_dir
        #: numIds stamped onto auto-numbered blocks (lists / headings / \part).
        #: Defaults to our bundled 1-5; shifted clear of a --reference-doc's own.
        self._num_ids = num_ids or NumIds()
        #: reference-template styleIds bound to appendix heading levels 1..4 and
        #: \part via \texwordstyle, or None to keep the built-in Heading styles.
        self.appendix_style_ids = (appendix_style_ids or []) + [None] * 4
        self.part_style_id = part_style_id
        #: \texwordstyle{figure} override for the image-line paragraph style.
        self._figure_style = figure_style_id or "Normal"
        #: \texwordstyle caption overrides keyed by kind ("Figure"/"Table"/
        #: "subfigure"/"Algorithm"); each None falls back to the built-in Caption.
        self._caption_style_ids = caption_style_ids or {}
        #: custom theorem environment -> paragraph style for the paragraph that
        #: contains its generated lead and first body paragraph.
        self._theorem_style_ids = theorem_style_ids or {}
        #: \texwordstyle{table} override for the text inside table cells (else Normal).
        self._table_text_style = table_text_style_id or "Normal"
        #: \texwordstyle{threelinetable} Word table style applied to a 三线表 (a
        #: tabular whose first command is \toprule), or None to keep the full grid.
        self._threeline_table_style = threeline_table_style_id
        #: \texwordstyle{body} override for ordinary body-text (正文) paragraphs:
        #: the default paragraph style at body level (else the built-in Normal), so
        #: 正文 can be e.g. an indented "normal-indent" style instead of plain Normal.
        self._body_style = body_style_id or "Normal"
        #: For standard English classes, the opening body paragraph and the first
        #: paragraph after each heading use the bound no-indent style.  The pipeline
        #: only supplies this when a reference template exists and ctex is not used.
        self._first_body_style = first_body_style_id
        #: \texwordstyle{section*}/{heading1*}/... overrides for starred headings
        #: only. Numbered headings keep Heading1..5 for navigation and numbering.
        self.star_heading_style_ids = (star_heading_style_ids or []) + [None] * 5
        #: {canonical styleId -> effective styleId} for \texwordstyle paragraph-style
        #: roles (abstract/sourcecode/title/...): explicit binding or auto-discovery.
        self._style_remap = style_remap or {}
        #: {lower-cased reference-doc style name -> styleId} for \texwordparstyle{name},
        #: the per-paragraph style override; plus a dedupe set for "not found" warnings.
        self._par_style_names = par_style_names or {}
        self._warned_par_styles: set[str] = set()
        #: {lower-cased style name -> character styleId} for
        #: \texwordcharstyle{name}{text}; includes linked paragraph/character styles.
        self._char_style_names = char_style_names or {}
        self._warned_char_styles: set[str] = set()
        #: document preamble (for compiling TikZ pictures to images)
        self.preamble = preamble
        self.number_by_section = number_by_section
        #: localisable caption/cross-reference wording (labels, separators, prefixes).
        self.caption_cfg = caption_config or CaptionConfig.english()
        #: In a Chinese document, give a directly-typed curly quote (“”‘’) an
        #: ``w:rFonts w:hint="eastAsia"`` so Word renders it with the CJK font
        #: (full-width quote); LaTeX-command quotes keep the default (Latin) font.
        self.cjk_quote_hint = cjk_quote_hint
        self.citation_mode = citation_mode
        self.columns = max(columns, 1)
        #: In \texwordtemplate[style-numbering] mode the reference template's
        #: paragraph styles carry numbering, so generated body paragraphs should
        #: not stamp explicit w:numPr/w:numId.
        self.style_numbering = style_numbering
        self._bullet_list_style = bullet_list_style_id or "ListBullet"
        self._number_list_style = number_list_style_id or "ListNumber"
        #: page geometry (from a --reference-doc), or None for the built-in default.
        self.page_pgsz = page_pgsz
        self.page_pgmar = page_pgmar
        #: (element, w:type, r:id) header/footer references from a --reference-doc.
        self.header_footer_refs = header_footer_refs or []
        #: citekey -> CSLItem, populated from the Bibliography block in build().
        self._cite_items: dict[str, ir.CSLItem] = {}
        #: archive path -> bytes, e.g. "word/media/image1.png".
        self.media: dict[str, bytes] = {}
        #: relationship XML strings for word/_rels/document.xml.rels.
        self.document_rels: list[str] = []
        self._image_counter = 0   # distinct media parts written
        self._drawing_counter = 0  # drawing instances (unique wp:docPr ids)
        #: content-hash -> (rel_id, media ext-index) so identical images are
        #: embedded once and merely re-referenced.
        self._media_by_hash: dict[str, tuple[str, int]] = {}
        #: rendered <w:footnote> elements (excluding the separator pair)
        self._footnotes: list[_Element] = []
        self._footnote_counter = 0
        #: rendered <w:endnote> elements (\endnote -> endnotes.xml)
        self._endnotes: list[_Element] = []
        self._endnote_counter = 0
        #: rendered <w:comment> elements (review annotations -> comments.xml)
        self._comments: list[_Element] = []
        self._comment_counter = 0
        self.math = MathCascade(report, image_renderer=image_math_renderer)
        fields.reset_bookmark_ids()

    # -- public ----------------------------------------------------------- #

    def build(self, doc: ir.Document) -> bytes:
        from ..bib import endnote, zotero

        zotero.reset_ids()
        endnote.reset_ids()
        # CJK glyphs inside math get the document's East-Asian font (xeCJK).
        self.math.cjk_font = doc.meta.cjk_main_font or doc.meta.cjk_sans_font
        for block in doc.blocks:
            if isinstance(block, ir.Bibliography):
                self._cite_items = {e.id: e for e in block.entries}
        body = el("w:body")
        self._emit_body(doc, body)
        document = el("w:document", body)
        return serialize(document)

    def _emit_body(self, doc: ir.Document, body: _Element) -> None:
        """Emit the title block and body blocks, inserting continuous section
        breaks so ``figure*``/``table*`` (and, in a multi-column paper, the
        title/abstract) span the full page width while text flows in N columns."""
        n = self.columns
        # (columns, emitter) for each top-level region, in order.
        regions: list[tuple[int, Any]] = []
        has_title = bool(
            doc.meta.title or doc.meta.authors or doc.meta.affiliations
            or doc.meta.date or doc.meta.abstract or doc.meta.keywords
        )
        if has_title:
            # the title/authors/abstract are set full-width above the columns.
            regions.append((1 if n > 1 else n, lambda: self._title_block(doc.meta, body)))
        first_body_paragraph = True
        for block in doc.blocks:
            cols = 1 if (n > 1 and getattr(block, "spanning", False)) else n
            default_style = self._body_style
            if isinstance(block, ir.Heading):
                first_body_paragraph = True
            elif isinstance(block, ir.Paragraph):
                if first_body_paragraph and self._first_body_style:
                    default_style = self._first_body_style
                # An explicit per-paragraph style still wins in _block(), but it
                # consumes the opening-paragraph position like any other paragraph.
                first_body_paragraph = False
            regions.append((
                cols,
                lambda b=block, style=default_style: self._block(
                    b, body, default_style=style,
                ),
            ))

        prev: int | None = None
        for cols, emit in regions:
            if prev is not None and cols != prev:
                # a continuous section break closes the previous region: its
                # sectPr (carried on this empty paragraph) describes that region.
                body.append(self._column_break_para(prev))
            emit()
            prev = cols
        body.append(self._sect_pr(columns=prev if prev is not None else n))

    def _column_break_para(self, columns: int) -> _Element:
        """An empty paragraph whose pPr carries a continuous section break, used to
        end a region with the given column count."""
        p = el("w:p")
        ppr = sub(p, "w:pPr")
        ppr.append(self._sect_pr(columns=columns, continuous=True))
        return p

    # -- title / meta ----------------------------------------------------- #

    def _title_block(self, meta: ir.DocumentMeta, body: _Element) -> None:
        if meta.title:
            p = self._styled_paragraph("Title")
            self._inlines(meta.title, p)
            body.append(p)
        for author in meta.authors:
            p = self._styled_paragraph("Subtitle")
            self._inlines(author, p)
            body.append(p)
        for affil in meta.affiliations:
            p = self._styled_paragraph("Subtitle")
            self._inlines([ir.Emphasis(list(affil), "italic")], p)
            body.append(p)
        if meta.date:
            p = self._styled_paragraph("Subtitle")
            self._inlines(meta.date, p)
            body.append(p)
        if meta.abstract:
            for block in meta.abstract:
                self._block(block, body, default_style="Abstract")
        if meta.keywords:
            p = self._styled_paragraph("Abstract")
            p.append(self._run("Keywords: ", bold=True))
            self._inlines(meta.keywords, p)
            body.append(p)

    # -- blocks ----------------------------------------------------------- #

    def _block(self, block: ir.Block, body: _Element, default_style: str = "Normal") -> None:
        if isinstance(block, ir.Heading):
            self._heading(block, body)
        elif isinstance(block, ir.Paragraph):
            # A standalone \nocite is zero-width outside EndNote mode.  Do not
            # introduce a blank paragraph merely to retain its IR position.
            if self.citation_mode != "endnote" and block.inlines and all(
                isinstance(node, ir.Cite) and node.hidden for node in block.inlines
            ):
                return
            p = self._styled_paragraph(self._par_style(block.style, default_style))
            if block.align:
                self._set_align(p, block.align)
            self._inlines(block.inlines, p)
            body.append(p)
        elif isinstance(block, ir.MathBlock):
            self._math_block(block, body)
        elif isinstance(block, ir.ItemList):
            self._list(block, body)
        elif isinstance(block, ir.Table):
            self._table(block, body)
        elif isinstance(block, ir.Figure):
            self._figure(block, body)
        elif isinstance(block, ir.Float):
            self._float(block, body)
        elif isinstance(block, ir.CodeBlock):
            self._code_block(block, body)
        elif isinstance(block, ir.Quote):
            for inner in block.blocks:
                self._block(inner, body, default_style="Quote")
        elif isinstance(block, ir.Theorem):
            self._theorem(block, body)
        elif isinstance(block, ir.PageBreak):
            self._page_break(body)
        elif isinstance(block, ir.Algorithm):
            self._algorithm(block, body)
        elif isinstance(block, ir.Bibliography):
            self._bibliography(block, body)
        elif isinstance(block, ir.TableOfContents):
            self._toc(block, body)
        elif isinstance(block, ir.Index):
            p = self._styled_paragraph("Normal")
            for run in fields.field('INDEX \\c "2" \\z "1033"'):
                p.append(run)
            body.append(p)
        elif isinstance(block, ir.RawPassthrough):
            p = self._styled_paragraph("SourceCode")
            p.append(self._run(block.latex))
            body.append(p)

    def _heading(self, block: ir.Heading, body: _Element) -> None:
        style = _HEADING_STYLE.get(block.level, "Heading5")
        # \texwordstyle overrides: appendix levels and \part adopt the template's
        # named paragraph style (whose linked multilevel list numId 4/5 point at).
        if block.part and self.part_style_id:
            style = self.part_style_id
        elif block.part and self.style_numbering:
            style = "part"
        elif block.appendix and 1 <= block.level <= 4:
            appendix_sid = self.appendix_style_ids[block.level - 1]
            if appendix_sid:
                style = appendix_sid
            elif self.style_numbering:
                style = f"appendix{block.level}"
        if not block.numbered and not block.part and 1 <= block.level <= 5:
            star_sid = self.star_heading_style_ids[block.level - 1]
            if star_sid:
                style = star_sid
        p = self._styled_paragraph(style)
        if self.style_numbering:
            pass
        elif block.part and block.numbered:
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            numpr = sub(ppr, "w:numPr")
            sub(numpr, "w:ilvl", **{"w:val": "0"})
            sub(numpr, "w:numId", **{"w:val": str(self._num_ids.part)})
        elif block.numbered and 1 <= block.level <= 4:
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            numpr = sub(ppr, "w:numPr")
            sub(numpr, "w:ilvl", **{"w:val": str(block.level - 1)})
            num_id = self._num_ids.appendix if block.appendix else self._num_ids.heading
            sub(numpr, "w:numId", **{"w:val": str(num_id)})
        start = None
        if block.label:
            start = fields.bookmark_start(_bookmark_for(block.label))
            p.append(start)
        self._inlines(block.inlines, p)
        if start is not None:
            p.append(fields.bookmark_end_for(start))
        body.append(p)

    def _page_break(self, body: _Element) -> None:
        p = self._styled_paragraph("Normal")
        r = el("w:r")
        sub(r, "w:br", **{"w:type": "page"})
        p.append(r)
        body.append(p)

    def _math_block(self, block: ir.MathBlock, body: _Element) -> None:
        # top-level numbered align/eqnarray keep per-line numbers; everything
        # else (align*, aligned, single-numbered equation) collapses to one
        # column-aligned matrix so the lines line up at the & relation.
        collapse = not (block.numbered and block.env in _MULTILINE_NUMBERED_ENVS)
        result = self.math.block(block.latex, collapse_align=collapse)
        if result.path == "image" and result.image is not None:
            data, fmt = result.image
            p = self._styled_paragraph("Normal")
            self._set_align(p, "center")
            r = el("w:r")
            r.append(self._embed_image_bytes(data, fmt, "equation"))
            p.append(r)
            body.append(p)
            return
        if result.path == "raw" or result.omath is None:
            p = self._styled_paragraph("Normal")
            p.append(self._run(f"\\[{block.latex}\\]", italic=True))
            body.append(p)
            return

        if block.numbered:
            body.append(self._numbered_equation(block, result.omath))
            return
        for omath in result.omath:
            p = self._styled_paragraph("Normal")
            para = el("m:oMathPara")
            para.append(omath)
            p.append(para)
            body.append(p)

    def _numbered_equation(self, block: ir.MathBlock, omaths: list[_Element]) -> _Element:
        """A numbered display equation: an ``m:eqArr`` per line inside one
        ``m:oMathPara`` whose ``#`` separator right-aligns each line's number.

        Multi-line numbered envs (``align``/``eqnarray``/…) keep the ``&``
        alignment as ``m:aln`` marks so the relations line up; everything else
        wraps its already-rendered (possibly column-collapsed) content as a
        single row."""
        p = self._styled_paragraph("Normal")
        p.append(self._numbered_equation_para(block, omaths))
        return p

    def _numbered_equation_para(
        self, block: ir.MathBlock, omaths: list[_Element]
    ) -> _Element:
        """The ``m:oMathPara`` for a numbered equation (without its paragraph), so
        it can also be embedded inline alongside explanatory text."""
        lines = self._equation_lines(block, omaths)
        para = el("m:oMathPara")
        last = len(lines) - 1
        for idx, segments in enumerate(lines):
            omath = el("m:oMath")
            eqarr = sub(omath, "m:eqArr")
            # maxDist spreads the row to the full width so '#' parks the number
            # hard against the right margin (the native Word numbered-eqn layout).
            sub(sub(eqarr, "m:eqArrPr"), "m:maxDist", **{"m:val": "1"})
            e = sub(eqarr, "m:e")
            for seg_idx, seg in enumerate(segments):
                if seg_idx > 0:
                    _mark_alignment(seg)
                for elem in seg:
                    e.append(elem)
            for run in self._equation_number(block, first=(idx == 0)):
                e.append(run)
            if idx != last:  # a soft line break stacks the rows in one math para
                br = el("m:r")
                sub(br, "w:br")
                omath.append(br)
            para.append(omath)
        if self.math.cjk_font:
            omml.tag_cjk_runs(para, self.math.cjk_font)
        return para

    def _equation_lines(
        self, block: ir.MathBlock, omaths: list[_Element]
    ) -> list[list[list[_Element]]]:
        """``lines -> &-segments -> elements`` for a numbered equation. The
        ``align`` family is re-rendered with alignment marks preserved; other
        envs reuse the cascade's output (one un-aligned segment per line)."""
        if block.env in _MULTILINE_NUMBERED_ENVS:
            try:
                return omml.render_block_segments(block.latex)
            except Exception:  # fall back to the un-aligned, already-rendered lines
                pass
        return [[list(o)] for o in omaths]

    def _equation_number(self, block: ir.MathBlock, first: bool) -> list[_Element]:
        """The ``#(N)`` number machinery appended inside a line's ``m:e``."""
        eq_open, eq_close = self.caption_cfg.eq_wrap
        out: list[_Element] = [
            fields.math_text_run("#", nor=False),  # right-align separator for m:eqArr
            fields.math_text_run(eq_open),
        ]
        start = None
        if block.label and first:
            start = fields.bookmark_start(_bookmark_for(block.label))
            out.append(start)
        out += fields.math_number_field(self.caption_cfg.seq_name("Equation"),
                                        self.number_by_section,
                                        self.caption_cfg.section_sep)
        if start is not None:
            out.append(fields.bookmark_end_for(start))
        out.append(fields.math_text_run(eq_close))
        return out

    def _list(self, block: ir.ItemList, body: _Element, level: int = 0) -> None:
        if block.description:
            for item in block.items:
                self._description_item(item, body, level)
            return
        num_id = self._num_ids.decimal if block.ordered else self._num_ids.bullet
        for item in block.items:
            self._list_item(item, body, level, num_id)

    def _list_item(self, item: ir.ListItem, body: _Element, level: int, num_id: int) -> None:
        if item.term:  # a custom \item[label] -> show the label, suppress the bullet
            self._description_item(item, body, level)
            return
        marked = False
        # If the item leads with a nested list (no leading text of its own — e.g.
        # an exam question whose \parts follow immediately), emit an empty
        # numbered paragraph first so the item's own number ("1.") renders ABOVE
        # the nested (a)/(b)/(c), rather than being deferred onto a later
        # trailing paragraph (which left the sub-list looking unnumbered).
        first_structural = next(
            (b for b in item.blocks if isinstance(b, (ir.Paragraph, ir.ItemList))), None
        )
        if isinstance(first_structural, ir.ItemList):
            p = self._styled_paragraph("Normal")
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            numpr = sub(ppr, "w:numPr")
            sub(numpr, "w:ilvl", **{"w:val": str(level)})
            sub(numpr, "w:numId", **{"w:val": str(num_id)})
            self._bookmark_list_item(item, p)
            body.append(p)
            marked = True
        for inner in item.blocks:
            if isinstance(inner, ir.ItemList):
                self._list(inner, body, level + 1)
                continue
            if not isinstance(inner, ir.Paragraph):
                self._block(inner, body)
                continue
            p = self._styled_paragraph(self._list_style(num_id) if not marked else "Normal")
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            if not marked and not self.style_numbering:
                numpr = sub(ppr, "w:numPr")
                sub(numpr, "w:ilvl", **{"w:val": str(level)})
                sub(numpr, "w:numId", **{"w:val": str(num_id)})
                # bookmark the numbered paragraph so \ref to \item\label{} resolves
                # to its list number (a REF \r field).
                self._bookmark_list_item(item, p)
                marked = True
            elif marked:
                sub(ppr, "w:ind", **{"w:left": str((level + 1) * 360)})
            if self.style_numbering and not marked:
                self._bookmark_list_item(item, p)
                marked = True
            self._inlines(inner.inlines, p)
            body.append(p)
        if not marked:
            p = self._styled_paragraph(self._list_style(num_id))
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            if not self.style_numbering:
                numpr = sub(ppr, "w:numPr")
                sub(numpr, "w:ilvl", **{"w:val": str(level)})
                sub(numpr, "w:numId", **{"w:val": str(num_id)})
            self._bookmark_list_item(item, p)
            body.append(p)

    def _list_style(self, num_id: int) -> str:
        if not self.style_numbering:
            return "Normal"
        return (
            self._number_list_style
            if num_id == self._num_ids.decimal
            else self._bullet_list_style
        )

    def _bookmark_list_item(self, item: ir.ListItem, p: _Element) -> None:
        """Wrap a numbered list-item paragraph in its label bookmark, if any."""
        if not item.label:
            return
        start = fields.bookmark_start(_bookmark_for(item.label))
        p.append(start)
        p.append(fields.bookmark_end_for(start))

    def _description_item(self, item: ir.ListItem, body: _Element, level: int) -> None:
        p = self._styled_paragraph("Normal")
        ppr = p.find(_qn("w:pPr"))
        assert ppr is not None
        sub(ppr, "w:ind", **{"w:left": str((level + 1) * 360)})
        if item.term:
            for t in item.term:
                self._inline_styled(t, p, "bold")
            p.append(self._run("  "))
        first_para = next((b for b in item.blocks if isinstance(b, ir.Paragraph)), None)
        if first_para is not None:
            self._inlines(first_para.inlines, p)
        body.append(p)
        for inner in item.blocks:
            if inner is first_para:
                continue
            if isinstance(inner, ir.ItemList):
                self._list(inner, body, level + 1)
            else:
                self._block(inner, body)

    def _table(self, block: ir.Table, body: _Element) -> None:
        tbl = el("w:tbl")
        tpr = sub(tbl, "w:tblPr")
        # a 三线表 (first command is \toprule) adopts the bound Word table style and
        # leaves border formatting to it; tblStyle must precede tblW in CT_TblPr.
        three_line = block.three_line and self._threeline_table_style is not None
        if three_line:
            sub(tpr, "w:tblStyle", **{"w:val": self._threeline_table_style})
        sub(tpr, "w:tblW", **{"w:w": "0", "w:type": "auto"})
        # \centering inside the table float -> center the table on the page; w:jc
        # must follow tblW and precede tblBorders in CT_TblPr.
        if block.align in ("center", "right"):
            sub(tpr, "w:jc", **{"w:val": "center" if block.align == "center" else "end"})
        if not three_line:
            # direct tblBorders would override a table style's borders, so only emit
            # the default full grid when no three-line table style applies.
            borders = sub(tpr, "w:tblBorders")
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                sub(borders, f"w:{side}", **{"w:val": "single", "w:sz": "4", "w:color": "auto"})
        ncols = max((sum(c.colspan for c in r.cells) for r in block.rows), default=1)
        # p{width} column widths -> twips (dxa); 914400 EMU = 1440 twips.
        widths = [int(w / 635) if w else None for w in block.colwidths]
        grid = sub(tbl, "w:tblGrid")
        for i in range(ncols):
            w = widths[i] if i < len(widths) else None
            sub(grid, "w:gridCol", **({"w:w": str(w)} if w else {}))

        # remaining vertical-merge continuations keyed by starting column index ->
        # (rows still to merge, colspan of the merged cell). The colspan must come
        # from the originating cell, not the current row's cell, so a cell that is
        # both \multicolumn and \multirow continues with the right grid width.
        vmerge_pending: dict[int, tuple[int, int]] = {}
        for row in block.rows:
            tr = sub(tbl, "w:tr")
            if row.is_header:
                trpr = sub(tr, "w:trPr")
                sub(trpr, "w:tblHeader")  # repeat header on each page
            col = 0
            for cell in row.cells:
                pending = vmerge_pending.get(col)
                if pending and pending[0] > 0:
                    remaining, merged_colspan = pending
                    self._merge_continue_cell(tr, merged_colspan)
                    vmerge_pending[col] = (remaining - 1, merged_colspan)
                    col += merged_colspan
                    continue
                span_w = sum(
                    widths[c] or 0 for c in range(col, col + cell.colspan) if c < len(widths)
                )
                self._table_cell(tr, cell, width=span_w or None)
                if cell.rowspan > 1:
                    vmerge_pending[col] = (cell.rowspan - 1, cell.colspan)
                col += cell.colspan
        cap = None
        if block.caption is not None:
            cap = self._caption("Table", block.caption, block.label,
                                numbered=block.caption_numbered)
        if cap is not None and block.caption_above:
            body.append(cap)
        body.append(tbl)
        if cap is not None and not block.caption_above:
            body.append(cap)

    def _table_cell(self, tr: _Element, cell: ir.TableCell, width: int | None = None) -> None:
        tc = sub(tr, "w:tc")
        tcpr = sub(tc, "w:tcPr")
        # CT_TcPr child order: tcW, gridSpan, vMerge, ..., shd
        if width:
            sub(tcpr, "w:tcW", **{"w:w": str(width), "w:type": "dxa"})
        if cell.colspan > 1:
            sub(tcpr, "w:gridSpan", **{"w:val": str(cell.colspan)})
        if cell.rowspan > 1:
            sub(tcpr, "w:vMerge", **{"w:val": "restart"})
        if cell.border_bottom:  # partial \cmidrule/\cline rule under this cell
            borders = sub(tcpr, "w:tcBorders")
            sub(borders, "w:bottom",
                **{"w:val": "single", "w:sz": "4", "w:space": "0", "w:color": "auto"})
        if cell.shade:
            sub(tcpr, "w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": cell.shade})
        last_is_para = False
        for cb in cell.blocks:
            if isinstance(cb, ir.Paragraph):
                p = self._styled_paragraph(self._table_text_style)
                self._set_align(p, cell.align)
                self._inlines(cb.inlines, p)
                tc.append(p)
                last_is_para = True
            else:  # nested tables and other block content
                self._block(cb, tc)
                last_is_para = False
        # a table cell must end with a paragraph (Word/ECMA-376 requirement)
        if not last_is_para:
            tc.append(self._styled_paragraph(self._table_text_style))

    def _merge_continue_cell(self, tr: _Element, colspan: int) -> None:
        tc = sub(tr, "w:tc")
        tcpr = sub(tc, "w:tcPr")
        if colspan > 1:
            sub(tcpr, "w:gridSpan", **{"w:val": str(colspan)})
        sub(tcpr, "w:vMerge")  # continue the merge
        tc.append(self._styled_paragraph(self._table_text_style))

    def _figure(self, block: ir.Figure, body: _Element) -> None:
        # wrap in a tagged block SDT so the round-trip reader recovers one
        # ir.Figure even when sub-figures render as a table grid (else they read
        # back as a Table + paragraphs).
        sdt = el("w:sdt")
        sub(sub(sdt, "w:sdtPr"), "w:tag", **{"w:val": FIG_SDT_TAG})
        content = sub(sdt, "w:sdtContent")
        cap = None
        if block.caption is not None:
            bookmark = _figure_bookmark(block)
            cap = self._caption("Figure", block.caption, block.label, bookmark=bookmark,
                                numbered=block.caption_numbered)
        if cap is not None and block.caption_above:
            content.append(cap)
        if block.subfigures:
            self._subfigures(block, content)
        elif block.image is None:
            # No convertible graphic (e.g. a TikZ picture): compile it to an image
            # if a TeX engine is available, else fall back to a caption-only figure
            # (no developer-facing placeholder text in the document).
            rendered = self._render_tikz(block)
            if rendered is not None:
                content.append(rendered)
        else:
            content.append(self._image_paragraph(block.image))
        if cap is not None and not block.caption_above:
            content.append(cap)
        body.append(sdt)

    def _float(self, block: ir.Float, body: _Element) -> None:
        cap = None
        if block.caption is not None:
            cap = self._caption(
                block.counter,
                block.caption,
                block.label,
                numbered=block.caption_numbered,
            )
        if cap is not None and block.caption_above:
            body.append(cap)
        for inner in block.blocks:
            self._block(inner, body)
        if cap is not None and not block.caption_above:
            body.append(cap)

    def _render_tikz(self, block: ir.Figure) -> _Element | None:
        """Compile a figure's TikZ/PGF source to an embedded PNG, or None."""
        if not block.source:
            return None
        try:
            from . import raster, tikz

            result = tikz.render(block.source, self.preamble)
        except Exception:  # never let a render bug abort the conversion
            return None
        if result is None:
            # Be specific about why so the user can fix their setup.
            if tikz.extract_picture(block.source) is None:
                reason = "no recognised TikZ/PGF picture in the figure"
            elif not tikz.available_engines():
                reason = "no TeX engine on PATH (install e.g. xelatex/pdflatex)"
            elif not raster.has_pdf_support():
                reason = "install tex2word[pdf] (pypdfium2) to rasterise the compiled figure"
            else:
                reason = "the TikZ compile failed (set TEX2WORD_TIKZ_DEBUG=1 to see the log)"
            self.report.warn("figure", f"TikZ figure not rendered: {reason}")
            return None
        data, w, h = result
        self.report.info("figure", "rendered TikZ figure to an image")
        p = self._styled_paragraph(self._figure_style)
        self._set_align(p, "center")
        r = el("w:r")
        r.append(self._register_and_draw(data, images.ImageInfo("png", w, h), "tikz.png"))
        p.append(r)
        return p

    def _subfigures(self, block: ir.Figure, body: _Element) -> None:
        # Sub-figures sit side by side in a borderless 1-row table; each cell
        # holds the (width-scaled) image and its "(a)" sub-caption.
        subs = block.subfigures
        ncols = max(len(subs), 1)
        col_emu = int(images._MAX_WIDTH_EMU / ncols) - 90000  # small inter-cell gap
        tbl = el("w:tbl")
        tpr = sub(tbl, "w:tblPr")
        sub(tpr, "w:tblW", **{"w:w": "0", "w:type": "auto"})
        sub(tpr, "w:jc", **{"w:val": "center"})
        grid = sub(tbl, "w:tblGrid")
        for _ in range(ncols):
            sub(grid, "w:gridCol")
        tr = sub(tbl, "w:tr")
        for idx, subfig in enumerate(subs):
            letter = chr(ord("a") + idx)
            tc = sub(tr, "w:tc")
            sub(sub(tc, "w:tcPr"), "w:tcW", **{"w:w": "0", "w:type": "auto"})
            cap = self._styled_paragraph(self._caption_style_for("subfigure"))
            cap.append(self._run(f"({letter}) ", bold=True))
            if subfig.caption:
                self._inlines(subfig.caption, cap)
            if subfig.caption_above:
                tc.append(cap)
            if subfig.image is not None:
                tc.append(self._image_paragraph(subfig.image, max_width_emu=col_emu))
            else:
                ph = self._styled_paragraph("Normal")
                self._set_align(ph, "center")
                ph.append(self._run("[sub-figure omitted]", italic=True))
                tc.append(ph)
            if not subfig.caption_above:
                tc.append(cap)
        body.append(tbl)

    def _image_paragraph(self, image: ir.Image, max_width_emu: int | None = None) -> _Element:
        p = self._styled_paragraph(self._figure_style)
        self._set_align(p, "center")
        drawing = self._embed_image(image, max_width_emu)
        if drawing is None:
            p.append(self._run(f"[image: {image.path}]", italic=True))
            return p
        r = el("w:r")
        r.append(drawing)
        p.append(r)
        return p

    def _embed_image(self, image: ir.Image, max_width_emu: int | None = None) -> _Element | None:
        import os

        path = self._resolve_image_path(image.path)
        if path is None:
            self.report.warn("includegraphics", f"image file not found: {image.path}")
            return None
        fmt = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        name = os.path.basename(path)

        info = images.probe(path)
        if info is not None and info.embeddable:
            with open(path, "rb") as fh:
                data = fh.read()
            return self._register_and_draw(data, info, name, max_width_emu, image)

        # SVG embeds as a vector blip (no raster fallback); Word renders it via
        # the asvg:svgBlip extension, older viewers fall back to the same part.
        if fmt == "svg":
            with open(path, "rb") as fh:
                data = fh.read()
            svg_info = info if info is not None else images.probe_bytes(data, "svg")
            return self._register_and_draw(data, svg_info, name, max_width_emu, image)

        # vector formats (PDF/EPS): rasterise to PNG when a backend is available.
        if fmt in ("pdf", "eps", "ps"):
            result = raster.rasterize(path, fmt)
            if result is not None:
                data, w, h = result
                self.report.info("includegraphics", f"rasterised {fmt} -> png: {image.path}")
                return self._register_and_draw(
                    data, images.ImageInfo("png", w, h), name, max_width_emu, image
                )
            hint = (
                "install tex2word[pdf]" if fmt == "pdf" and not raster.has_pdf_support()
                else f"{fmt} rasterisation not supported"
            )
            self.report.warn("includegraphics", f"image not embedded ({hint}): {image.path}")
            return None

        self.report.warn("includegraphics", f"unsupported image format '{fmt}': {image.path}")
        return None

    def _resolve_image_path(self, rel: str) -> str | None:
        import os

        path = rel if os.path.isabs(rel) else os.path.join(self.base_dir, rel)
        if os.path.isfile(path):
            return path
        # graphicx resolves a missing extension against known formats.
        base = os.path.splitext(path)[0]
        for candidate in (path, base):
            for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps"):
                if os.path.isfile(candidate + ext):
                    return candidate + ext
        return None

    def _embed_image_bytes(self, data: bytes, fmt: str, name: str) -> _Element:
        info = images.probe_bytes(data, fmt)
        return self._register_and_draw(data, info, name)

    def _register_and_draw(
        self, data: bytes, info: images.ImageInfo, name: str,
        max_width_emu: int | None = None, image: ir.Image | None = None,
    ) -> _Element:
        # dedup identical image bytes -> one media part + one relationship,
        # referenced by every drawing that uses it.
        key = hashlib.sha1(data).hexdigest()
        cached = self._media_by_hash.get(key)
        if cached is not None:
            rel_id, _ = cached
        else:
            self._image_counter += 1
            n = self._image_counter
            ext = info.fmt if info.fmt != "jpg" else "jpeg"
            rel_id = f"rIdImg{n}"
            self.media[f"word/media/image{n}.{ext}"] = data
            self.document_rels.append(
                f'<Relationship Id="{rel_id}" Type="{_IMAGE_REL_TYPE}" '
                f'Target="media/image{n}.{ext}"/>'
            )
            self._media_by_hash[key] = (rel_id, n)
        # each drawing instance needs a unique wp:docPr id
        self._drawing_counter += 1
        draw_id = self._drawing_counter
        cx, cy = self._image_extent(info, image, max_width_emu)
        rot = self._rotation(image)
        src_rect = self._src_rect(info, image)
        alt = (image.alt if image and image.alt else name)
        svg_rel = rel_id if info.fmt == "svg" else None
        return self._drawing(
            rel_id, cx, cy, draw_id, name,
            rot=rot, src_rect=src_rect, alt=alt, svg_rel=svg_rel,
        )

    @staticmethod
    def _image_extent(
        info: images.ImageInfo, image: ir.Image | None, max_width_emu: int | None
    ) -> tuple[int, int]:
        """Final (cx, cy) in EMU, honouring width/height/scale then fitting width."""
        nat_cx = info.width_px * images.EMU_PER_PX
        nat_cy = info.height_px * images.EMU_PER_PX
        w = image.width if image else None
        h = image.height if image else None
        scale = image.scale if image else None
        if w and h:
            cx, cy = w, h
        elif w:
            cx, cy = w, (nat_cy * w / nat_cx if nat_cx else nat_cy)
        elif h:
            cx, cy = (nat_cx * h / nat_cy if nat_cy else nat_cx), h
        elif scale:
            cx, cy = nat_cx * scale, nat_cy * scale
        else:
            return images.emu_size(info, max_width_emu)
        limit = max_width_emu if max_width_emu is not None else images._MAX_WIDTH_EMU
        if cx > limit:
            cy *= limit / cx
            cx = limit
        return int(cx), int(cy)

    @staticmethod
    def _rotation(image: ir.Image | None) -> int | None:
        if not image or not image.angle:
            return None
        # OOXML rot is 1/60000 deg, clockwise; LaTeX angle is counter-clockwise.
        return int(round((-image.angle % 360) * 60000))

    @staticmethod
    def _src_rect(info: images.ImageInfo, image: ir.Image | None) -> dict[str, str] | None:
        # graphicx only actually crops the trimmed region when `clip` is given;
        # trim alone shifts the box. srcRect always crops, so gate it on clip.
        if not image or not image.trim or not image.clip:
            return None
        nat_cx = info.width_px * images.EMU_PER_PX or 1
        nat_cy = info.height_px * images.EMU_PER_PX or 1
        left, bottom, right, top = image.trim  # graphicx order: l b r t
        # a:srcRect insets are in 1000ths of a percent of the natural dimension.
        return {
            "l": str(int(left / nat_cx * 100000)),
            "t": str(int(top / nat_cy * 100000)),
            "r": str(int(right / nat_cx * 100000)),
            "b": str(int(bottom / nat_cy * 100000)),
        }

    #: fixed ext uri Word uses to attach an SVG vector blip to a picture.
    _SVG_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"

    def _drawing(self, rel_id: str, cx: int, cy: int, n: int, name: str, *,
                 rot: int | None = None, src_rect: dict[str, str] | None = None,
                 alt: str = "", svg_rel: str | None = None) -> _Element:
        drawing = el("w:drawing")
        inline = sub(drawing, "wp:inline", **{
            "distT": "0", "distB": "0", "distL": "0", "distR": "0",
        })
        sub(inline, "wp:extent", cx=str(cx), cy=str(cy))
        sub(inline, "wp:docPr", id=str(n), name=f"Picture {n}", descr=alt)
        frame = sub(inline, "wp:cNvGraphicFramePr")
        sub(frame, "a:graphicFrameLocks", noChangeAspect="1")
        graphic = sub(inline, "a:graphic")
        gdata = sub(graphic, "a:graphicData", uri=_PIC_URI)
        pic = sub(gdata, "pic:pic")
        nv = sub(pic, "pic:nvPicPr")
        sub(nv, "pic:cNvPr", id="0", name=name, descr=alt)
        sub(nv, "pic:cNvPicPr")
        blipfill = sub(pic, "pic:blipFill")
        blip = sub(blipfill, "a:blip", **{"r:embed": rel_id})
        if svg_rel is not None:
            ext = sub(sub(blip, "a:extLst"), "a:ext", uri=self._SVG_EXT_URI)
            sub(ext, "asvg:svgBlip", **{"r:embed": svg_rel})
        if src_rect is not None:
            sub(blipfill, "a:srcRect", **src_rect)
        stretch = sub(blipfill, "a:stretch")
        sub(stretch, "a:fillRect")
        sppr = sub(pic, "pic:spPr")
        xfrm = sub(sppr, "a:xfrm", **({"rot": str(rot)} if rot else {}))
        sub(xfrm, "a:off", x="0", y="0")
        sub(xfrm, "a:ext", cx=str(cx), cy=str(cy))
        geom = sub(sppr, "a:prstGeom", prst="rect")
        sub(geom, "a:avLst")
        return drawing

    def _toc(self, block: ir.TableOfContents, body: _Element) -> None:
        title, code = _TOC_SPEC[block.kind]
        # the figure/table lists build from the SEQ counter, whose identifier the
        # caption may have renamed (\texwordcaption{figureseq}{...}); keep the
        # \c reference in step or the list comes up empty.
        if block.kind == "figures":
            code = f'TOC \\h \\z \\c "{self.caption_cfg.seq_name("Figure")}"'
        elif block.kind == "tables":
            code = f'TOC \\h \\z \\c "{self.caption_cfg.seq_name("Table")}"'
        heading = self._styled_paragraph("Normal")
        heading.append(self._run(title, bold=True))
        body.append(heading)
        p = self._styled_paragraph("Normal")
        for run in fields.field(code, "Right-click and Update Field to build this list."):
            p.append(run)
        body.append(p)

    def _caption_style_for(self, kind: str) -> str:
        """The paragraph style for a caption of *kind* ("Figure"/"Table"/...).

        A \\texwordstyle per-type or default binding, else the built-in Caption.
        """
        return self._caption_style_ids.get(kind) or "Caption"

    def _caption_label_char_style(self, kind: str) -> str | None:
        """Resolved character style for the displayed caption identifier."""
        return self._char_style(self.caption_cfg.label_style(kind))

    def _caption(
        self,
        counter: str,
        caption: list[ir.Inline],
        label: str | None,
        bookmark: str | None = None,
        numbered: bool = True,
    ) -> _Element:
        p = self._styled_paragraph(self._caption_style_for(counter))
        if not numbered:
            # \caption*: no counter/SEQ, no "Figure N:" prefix -- just the text.
            self._inlines(caption, p)
            return p
        cfg = self.caption_cfg
        label_style = self._caption_label_char_style(counter)
        p.append(
            self._run(
                f"{cfg.label(counter)}{cfg.label_number_sep}",
                char_style=label_style,
            )
        )
        name = bookmark or (_bookmark_for(label) if label else None)
        start = None
        if name:
            start = fields.bookmark_start(name)
            p.append(start)
        for run in fields.number_field(
            cfg.seq_name(counter), self.number_by_section, cfg.section_sep
        ):
            _set_run_char_style(run, label_style)
            p.append(run)
        if start is not None:
            p.append(fields.bookmark_end_for(start))
        p.append(self._run(cfg.delim, char_style=label_style))
        self._inlines(caption, p)
        return p

    def _bibliography(self, block: ir.Bibliography, body: _Element) -> None:
        from ..bib.render import format_reference

        if not block.entries:
            return
        # wrap the whole reference list in a tagged block SDT so the round-trip
        # reader recovers it as one ir.Bibliography (not heading + paragraphs).
        sdt = el("w:sdt")
        sdtpr = sub(sdt, "w:sdtPr")
        sub(sdtpr, "w:tag", **{"w:val": BIB_SDT_TAG})
        content = sub(sdt, "w:sdtContent")
        level = min(max(block.heading_level, 1), 5)
        heading_style = _HEADING_STYLE.get(level, "Heading5")
        if not block.heading_numbered:
            heading_style = self.star_heading_style_ids[level - 1] or heading_style
        heading = self._styled_paragraph(heading_style)
        if block.heading_numbered and not self.style_numbering and level <= 4:
            ppr = heading.find(_qn("w:pPr"))
            assert ppr is not None
            numpr = sub(ppr, "w:numPr")
            sub(numpr, "w:ilvl", **{"w:val": str(level - 1)})
            sub(numpr, "w:numId", **{"w:val": str(self._num_ids.heading)})
        self._inlines(block.title or [ir.Text("References")], heading)
        content.append(heading)
        zotero = self.citation_mode == "zotero"
        endnote = self.citation_mode == "endnote"
        for i, item in enumerate(block.entries, start=1):
            p = self._styled_paragraph("Bibliography")
            start = fields.bookmark_start(_bookmark_for("bib_" + item.id))
            p.append(start)
            if i == 1 and (zotero or endnote):
                # Open the live bibliography field *before* the first reference
                # so the whole list is its cached result; close it after the last
                # entry. This keeps reference-manager refreshes in place.
                if zotero:
                    from ..bib.zotero import bibliography_field_begin
                else:
                    from ..bib.endnote import bibliography_field_begin

                for run in bibliography_field_begin():
                    p.append(run)
            if block.style == "numeric":
                p.append(self._run(f"[{i}]\t"))
            p.append(self._run(format_reference(item)))
            p.append(fields.bookmark_end_for(start))
            content.append(p)
        if (zotero or endnote) and block.entries:
            if zotero:
                from ..bib.zotero import bibliography_field_end
            else:
                from ..bib.endnote import bibliography_field_end

            closer = self._styled_paragraph("Bibliography")
            closer.append(bibliography_field_end())
            content.append(closer)
        body.append(sdt)

    def _code_block(self, block: ir.CodeBlock, body: _Element) -> None:
        for line in block.text.split("\n"):
            p = self._styled_paragraph("SourceCode")
            p.append(self._run(line))
            body.append(p)

    def _algorithm(self, block: ir.Algorithm, body: _Element) -> None:
        # Render in a "ruled" box: a full-width single-cell table with only top
        # and bottom borders, the caption ruled off from the body.
        tbl = el("w:tbl")
        tpr = sub(tbl, "w:tblPr")
        sub(tpr, "w:tblW", **{"w:w": "5000", "w:type": "pct"})
        borders = sub(tpr, "w:tblBorders")
        for side in ("top", "bottom"):
            sub(borders, f"w:{side}", **{"w:val": "single", "w:sz": "12", "w:color": "auto"})
        for side in ("left", "right", "insideH", "insideV"):
            sub(borders, f"w:{side}", **{"w:val": "nil"})
        sub(sub(tbl, "w:tblGrid"), "w:gridCol")
        tc = sub(sub(tbl, "w:tr"), "w:tc")

        if block.caption is not None or block.label:
            cap = self._styled_paragraph(self._caption_style_for("Algorithm"))
            # a rule under the caption
            cap_ppr = cap.find(_qn("w:pPr"))
            assert cap_ppr is not None
            pbdr = sub(cap_ppr, "w:pBdr")
            sub(pbdr, "w:bottom", **{
                "w:val": "single", "w:sz": "6", "w:space": "2", "w:color": "auto",
            })
            cfg = self.caption_cfg
            label_style = self._caption_label_char_style("Algorithm")
            label_kw = (
                {"char_style": label_style}
                if label_style else
                {"bold": True}
            )
            cap.append(
                self._run(
                    f"{cfg.label('Algorithm')}{cfg.label_number_sep}",
                    **label_kw,  # type: ignore[arg-type]
                )
            )
            start = None
            if block.label:
                start = fields.bookmark_start(_bookmark_for(block.label))
                cap.append(start)
            for run in fields.number_field(
                cfg.seq_name("Algorithm"), self.number_by_section, cfg.section_sep
            ):
                _set_run_char_style(run, label_style)
                cap.append(run)
            if start is not None:
                cap.append(fields.bookmark_end_for(start))
            cap.append(self._run(cfg.delim, **label_kw))  # type: ignore[arg-type]
            if block.caption:
                self._inlines(block.caption, cap)
            tc.append(cap)
        # numbered, indented pseudocode lines
        line_no = 0
        for aline in block.lines:
            p = self._styled_paragraph("SourceCode")
            ppr = p.find(_qn("w:pPr"))
            assert ppr is not None
            sub(ppr, "w:ind", **{"w:left": str(360 + aline.indent * 360)})
            if aline.number:
                line_no += 1
                p.append(self._run(f"{line_no:>2}  "))
            self._inlines(aline.inlines, p)
            tc.append(p)
        if not block.lines:
            tc.append(self._styled_paragraph("SourceCode"))
        body.append(tbl)

    def _theorem(self, block: ir.Theorem, body: _Element) -> None:
        blocks = block.blocks or [ir.Paragraph([])]
        env = (block.env or "").strip().lower()
        p = self._styled_paragraph(self._theorem_style_ids.get(env, "Normal"))
        self._write_theorem_prefix(block, p)
        if isinstance(blocks[0], ir.Paragraph):
            self._inlines(blocks[0].inlines, p)
            rest = blocks[1:]
        else:
            rest = blocks
        body.append(p)
        for inner in rest:
            self._block(inner, body)
        if block.kind == "Proof":
            self._append_qed(body)

    def _write_theorem_prefix(self, block: ir.Theorem, p: _Element) -> None:
        is_proof = block.kind == "Proof"
        emphasis = "italic" if is_proof else "bold"
        flag = {emphasis: True}
        cfg = self.caption_cfg
        theorem_label = cfg.theorem_value(block.env, "label", block.kind)
        label_style_name = cfg.theorem_value(block.env, "style", "")
        label_style = self._char_style(label_style_name)
        label_kw = {"char_style": label_style} if label_style else flag
        p.append(self._run(theorem_label, **label_kw))  # type: ignore[arg-type]
        if block.counter:
            label_sep = cfg.theorem_value(block.env, "labelsep", " ")
            p.append(self._run(label_sep, **label_kw))  # type: ignore[arg-type]
            start = None
            if block.label:
                start = fields.bookmark_start(_bookmark_for(block.label))
                p.append(start)
            counter = cfg.theorem_value(block.env, "seq", block.counter)
            for run in fields.number_field(
                counter, self.number_by_section, cfg.section_sep
            ):
                _set_run_char_style(run, label_style)
                p.append(run)
            if start is not None:
                p.append(fields.bookmark_end_for(start))
        if block.title:
            title_open = cfg.theorem_value(block.env, "titleopen", " (")
            title_close = cfg.theorem_value(block.env, "titleclose", ")")
            p.append(self._run(title_open, char_style=label_style))
            for t in block.title:
                if label_style is None:
                    self._inline_styled(t, p, emphasis)
                else:
                    self._inline(t, p)
            p.append(self._run(title_close))
        delim = cfg.theorem_value(block.env, "delim", ". ")
        delim_kw = label_kw if not block.title else flag
        p.append(self._run(delim, **delim_kw))  # type: ignore[arg-type]

    def _append_qed(self, body: _Element) -> None:
        last = body[-1]
        if last.tag == _qn("w:p"):
            last.append(self._run(" □"))  # em-space + QED box
        else:
            q = self._styled_paragraph("Normal")
            self._set_align(q, "right")
            q.append(self._run("□"))
            body.append(q)

    # -- inline ----------------------------------------------------------- #

    def _inlines(self, inlines: list[ir.Inline], p: _Element) -> None:
        for idx, node in enumerate(inlines):
            if isinstance(node, ir.DisplayMath):
                # a display equation sharing the paragraph with text: a soft break
                # ends the lead-in text, and another ends the equation when more
                # text follows (so they stay in one Word paragraph).
                lead = _has_visible_inline(inlines[:idx])
                trail = _has_visible_inline(inlines[idx + 1:])
                self._display_math_inline(node, p, lead_break=lead, trail_break=trail)
            else:
                self._inline(node, p)

    def _soft_break(self, p: _Element) -> None:
        r = el("w:r")
        sub(r, "w:br")
        p.append(r)

    def _display_math_inline(
        self, node: ir.DisplayMath, p: _Element, *, lead_break: bool, trail_break: bool
    ) -> None:
        block = node.to_block()
        collapse = not (block.numbered and block.env in _MULTILINE_NUMBERED_ENVS)
        result = self.math.block(block.latex, collapse_align=collapse)
        if result.path == "image" and result.image is not None:
            if lead_break:
                self._soft_break(p)
            data, fmt = result.image
            r = el("w:r")
            r.append(self._embed_image_bytes(data, fmt, "equation"))
            p.append(r)
            if trail_break:
                self._soft_break(p)
            return
        if result.path == "raw" or result.omath is None:
            if lead_break:
                self._soft_break(p)
            p.append(self._run(f"\\[{block.latex}\\]", italic=True))
            if trail_break:
                self._soft_break(p)
            return
        if lead_break:
            self._soft_break(p)
        if block.numbered:
            para = self._numbered_equation_para(block, result.omath)
        else:
            para = el("m:oMathPara")
            for omath in result.omath:
                para.append(omath)
            if self.math.cjk_font:
                omml.tag_cjk_runs(para, self.math.cjk_font)
        if trail_break and len(para):
            # a soft break inside the last m:oMath separates it from the text that
            # follows in the same paragraph.
            br = el("m:r")
            sub(br, "w:br")
            para[-1].append(br)
        p.append(para)

    def _inline(self, node: ir.Inline, p: _Element) -> None:
        self._emit(node, p, _RunStyle())

    def _inline_styled(self, node: ir.Inline, p: _Element, kind: str) -> None:
        self._emit(node, p, _RunStyle().with_flag(_STYLE_FLAG[kind]))

    def _emit(self, node: ir.Inline, p: _Element, st: _RunStyle) -> None:  # noqa: C901
        # leaf + style-carrying nodes thread the accumulated run style; the rest
        # (math, refs, links, ...) render once and ignore inline styling.
        if isinstance(node, ir.Text):
            kw = st.kwargs()
            if node.cjk_quote and self.cjk_quote_hint:
                kw["eastasia_hint"] = True
            p.append(self._run(node.value, **kw))
        elif isinstance(node, ir.Emphasis):
            st2 = st.with_flag(_STYLE_FLAG[node.kind_])
            for child in node.inlines:
                self._emit(child, p, st2)
        elif isinstance(node, ir.CharStyle):
            st2 = _replace(st, char_style=self._char_style(node.style))
            for child in node.inlines:
                self._emit(child, p, st2)
        elif isinstance(node, ir.Colored):
            st2 = _replace(st, color=node.fg or st.color, shade=node.bg or st.shade)
            for child in node.inlines:
                self._emit(child, p, st2)
        elif isinstance(node, ir.FontSize):
            st2 = _replace(st, size=node.half_points)
            for child in node.inlines:
                self._emit(child, p, st2)
        elif isinstance(node, ir.RawInline):
            p.append(self._run(node.latex, **_replace(st, italic=True).kwargs()))
        elif isinstance(node, ir.LineBreak):
            r = el("w:r")
            sub(r, "w:br")
            p.append(r)
        elif isinstance(node, ir.Image):
            drawing = self._embed_image(node)
            if drawing is not None:
                r = el("w:r")
                r.append(drawing)
                p.append(r)
        elif isinstance(node, ir.Math):
            self._inline_math(node, p, st)
        elif isinstance(node, ir.Ref):
            self._ref(node, p)
        elif isinstance(node, ir.Cite):
            self._cite(node, p)
        elif isinstance(node, ir.Link):
            self._link(node, p)
        elif isinstance(node, ir.Footnote):
            self._footnote(node, p)
        elif isinstance(node, ir.Endnote):
            self._endnote(node, p)
        elif isinstance(node, ir.Comment):
            self._comment(node, p)
        elif isinstance(node, ir.IndexEntry):
            for run in fields.index_entry(node.term):
                p.append(run)
        elif isinstance(node, ir.WordField):
            for run in fields.field(node.code, node.cached):
                p.append(run)

    def _comment(self, node: ir.Comment, p: _Element) -> None:
        self._comment_counter += 1
        cid = str(self._comment_counter)
        # a point anchor in the body: empty range + a reference mark
        sub(p, "w:commentRangeStart", **{"w:id": cid})
        sub(p, "w:commentRangeEnd", **{"w:id": cid})
        sub(sub(p, "w:r"), "w:commentReference", **{"w:id": cid})
        # the comment body (lives in comments.xml)
        cp = self._styled_paragraph("Normal")
        self._inlines([ir.Text(node.text)], cp)
        attrs = {"w:id": cid, "w:author": node.author or "tex2word",
                 "w:date": "2026-01-01T00:00:00Z"}
        self._comments.append(el("w:comment", cp, **attrs))

    def comments_xml(self) -> bytes | None:
        """Return ``word/comments.xml`` bytes, or None if there are no comments."""
        if not self._comments:
            return None
        root = el("w:comments")
        for c in self._comments:
            root.append(c)
        return serialize(root)

    def _inline_math(self, node: ir.Math, p: _Element, st: _RunStyle | None = None) -> None:
        result = self.math.inline(node.latex)
        if result.path == "omml" and result.omath:
            omath = result.omath[0]
            if st is not None and (st.color or st.size):
                _style_math_runs(omath, st.color, st.size)
            p.append(omath)
        elif result.path == "image" and result.image is not None:
            data, fmt = result.image
            p.append(self._embed_image_bytes(data, fmt, "math"))
        else:
            kw = st.kwargs() if st is not None else {}
            kw["italic"] = True
            p.append(self._run(f"${node.latex}$", **kw))

    def _ref(self, node: ir.Ref, p: _Element) -> None:
        if node.bookmark is None:
            p.append(self._run("??"))
            return
        # cleveref-style type prefix ("Figure ", "fig. ", 图, ...)
        names = self.caption_cfg.ref_names.get(node.ref_kind)
        if names and node.style in ("abbrev", "full"):
            prefix = names[0 if node.style == "abbrev" else 1]
            if prefix:
                p.append(self._run(prefix))
        if node.ref_kind == "page":
            runs = fields.pageref_field(node.bookmark, "0")
        elif node.ref_kind == "equation":
            eq_open, eq_close = self.caption_cfg.eq_wrap
            p.append(self._run(eq_open))
            for run in fields.ref_field(node.bookmark, "0"):
                p.append(run)
            p.append(self._run(eq_close))
            return
        elif node.ref_kind in ("section", "listitem"):
            # \r inserts the paragraph's (list) number -- section numbers and the
            # auto-numbered list item that an \item\label{} sits on.
            runs = fields.ref_field(node.bookmark, "0", paragraph_number=True)
        else:
            runs = fields.ref_field(node.bookmark, "0")
        for run in runs:
            p.append(run)

    def _cite(self, node: ir.Cite, p: _Element) -> None:
        rendered = node.rendered
        if node.hidden:
            keys = list(self._cite_items) if "*" in node.keys else node.keys
            if self.citation_mode == "endnote" and any(
                key in self._cite_items for key in keys
            ):
                from ..bib import endnote

                for run in endnote.citation_field(node, self._cite_items, ""):
                    p.append(run)
            return
        if (
            self.citation_mode == "zotero"
            and rendered is not None
            and any(k in self._cite_items for k in node.keys)
        ):
            from ..bib import zotero

            for run in zotero.citation_field(node, self._cite_items, rendered):
                p.append(run)
            return
        if (
            self.citation_mode == "endnote"
            and rendered is not None
            and any(k in self._cite_items for k in node.keys)
        ):
            from ..bib import endnote

            for run in endnote.citation_field(node, self._cite_items, rendered):
                p.append(run)
            return
        if rendered is not None:
            p.append(self._run(rendered))
            return
        # Fallback when no bibliography is present.
        text = ", ".join(node.keys)
        p.append(self._run(f"[{text}]" if node.mode == "paren" else text))

    def _link(self, node: ir.Link, p: _Element) -> None:
        instr = f'HYPERLINK \\l "{node.anchor}"' if node.anchor else f'HYPERLINK "{node.url}"'
        p.append(fields._fldchar("begin"))
        p.append(fields._instr_run(instr))
        p.append(fields._fldchar("separate"))
        for child in node.inlines:
            self._inline_styled(child, p, "hyperlink")
        p.append(fields._fldchar("end"))

    def _footnote(self, node: ir.Footnote, p: _Element) -> None:
        self._footnote_counter += 1
        note_id = self._footnote_counter
        # reference mark in the body
        r = el("w:r")
        rpr = sub(r, "w:rPr")
        sub(rpr, "w:rStyle", **{"w:val": "FootnoteReference"})
        sub(r, "w:footnoteReference", **{"w:id": str(note_id)})
        p.append(r)
        # the footnote content paragraph (lives in footnotes.xml)
        fp = self._styled_paragraph("FootnoteText")
        mark = el("w:r")
        mark_rpr = sub(mark, "w:rPr")
        sub(mark_rpr, "w:rStyle", **{"w:val": "FootnoteReference"})
        sub(mark, "w:footnoteRef")
        fp.append(mark)
        fp.append(self._run(" "))
        self._inlines(node.inlines, fp)
        footnote = el("w:footnote", fp, **{"w:id": str(note_id)})
        self._footnotes.append(footnote)

    def footnotes_xml(self) -> bytes | None:
        """Return ``word/footnotes.xml`` bytes, or None if there are no notes."""
        if not self._footnotes:
            return None
        root = el("w:footnotes")
        root.append(self._special_footnote("separator", -1, "w:separator"))
        root.append(self._special_footnote("continuationSeparator", 0, "w:continuationSeparator"))
        for footnote in self._footnotes:
            root.append(footnote)
        return serialize(root)

    def _special_footnote(self, kind: str, note_id: int, mark_tag: str) -> _Element:
        p = el("w:p")
        r = el("w:r")
        sub(r, mark_tag)
        p.append(r)
        return el("w:footnote", p, **{"w:type": kind, "w:id": str(note_id)})

    def _endnote(self, node: ir.Endnote, p: _Element) -> None:
        self._endnote_counter += 1
        note_id = self._endnote_counter
        r = el("w:r")
        sub(sub(r, "w:rPr"), "w:rStyle", **{"w:val": "FootnoteReference"})
        sub(r, "w:endnoteReference", **{"w:id": str(note_id)})
        p.append(r)
        ep = self._styled_paragraph("FootnoteText")
        mark = el("w:r")
        sub(sub(mark, "w:rPr"), "w:rStyle", **{"w:val": "FootnoteReference"})
        sub(mark, "w:endnoteRef")
        ep.append(mark)
        ep.append(self._run(" "))
        self._inlines(node.inlines, ep)
        self._endnotes.append(el("w:endnote", ep, **{"w:id": str(note_id)}))

    def endnotes_xml(self) -> bytes | None:
        """Return ``word/endnotes.xml`` bytes, or None if there are no endnotes."""
        if not self._endnotes:
            return None
        root = el("w:endnotes")
        for kind, nid, tag in (
            ("separator", -1, "w:separator"),
            ("continuationSeparator", 0, "w:continuationSeparator"),
        ):
            r = el("w:r")
            sub(r, tag)
            root.append(el("w:endnote", el("w:p", r), **{"w:type": kind, "w:id": str(nid)}))
        for endnote in self._endnotes:
            root.append(endnote)
        return serialize(root)

    # -- run / paragraph primitives -------------------------------------- #

    def _run(self, text: str, *, bold: bool = False, italic: bool = False,
             underline: bool = False, typewriter: bool = False, smallcaps: bool = False,
             hyperlink: bool = False, superscript: bool = False,
             subscript: bool = False, color: str | None = None,
             shade: str | None = None, strike: bool = False,
             highlight: bool = False, size: int | None = None,
             eastasia_hint: bool = False, char_style: str | None = None) -> _Element:
        r = el("w:r")
        if any([bold, italic, underline, typewriter, smallcaps, hyperlink,
                superscript, subscript, color, shade, strike, highlight, size,
                eastasia_hint, char_style]):
            rpr = sub(r, "w:rPr")
            # children must follow the ECMA-376 CT_RPr sequence, else strict
            # validators reject the run: rStyle, rFonts, b, i, smallCaps, strike,
            # color, sz, szCs, highlight, u, shd, vertAlign.
            if char_style:
                sub(rpr, "w:rStyle", **{"w:val": char_style})
            elif hyperlink:
                sub(rpr, "w:rStyle", **{"w:val": "Hyperlink"})
            if typewriter:
                sub(rpr, "w:rFonts", **{"w:ascii": "Consolas", "w:hAnsi": "Consolas"})
            elif eastasia_hint:
                # ambiguous quote glyph -> Word picks the CJK (eastAsia) font.
                sub(rpr, "w:rFonts", **{"w:hint": "eastAsia"})
            if bold:
                sub(rpr, "w:b")
            if italic:
                sub(rpr, "w:i")
            if smallcaps:
                sub(rpr, "w:smallCaps")
            if strike:
                sub(rpr, "w:strike")
            if color:
                sub(rpr, "w:color", **{"w:val": color})
            if size:
                sub(rpr, "w:sz", **{"w:val": str(size)})
                sub(rpr, "w:szCs", **{"w:val": str(size)})
            if highlight:
                sub(rpr, "w:highlight", **{"w:val": "yellow"})
            if underline:
                sub(rpr, "w:u", **{"w:val": "single"})
            if shade:
                sub(rpr, "w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": shade})
            if superscript:
                sub(rpr, "w:vertAlign", **{"w:val": "superscript"})
            if subscript:
                sub(rpr, "w:vertAlign", **{"w:val": "subscript"})
        t = text_el("w:t", text)
        preserve_space(t)
        r.append(t)
        return r

    def _par_style(self, name: str | None, default: str) -> str:
        """Resolve a ``\\texwordparstyle{name}`` per-paragraph style to a styleId.

        ``name`` is a reference-doc style's display name (case-insensitive); an
        unknown name (or no template) warns once and falls back to *default*.
        """
        if not name:
            return default
        sid = self._par_style_names.get(name.strip().lower())
        if sid is None:
            if name not in self._warned_par_styles:
                self._warned_par_styles.add(name)
                self.report.warn(
                    "reference-doc",
                    f"\\texwordparstyle: style {name!r} not found in the reference template",
                )
            return default
        return sid

    def _char_style(self, name: str | None) -> str | None:
        """Resolve a ``\\texwordcharstyle{name}{text}`` style to a character styleId."""
        if not name:
            return None
        key = name.strip().lower()
        sid = self._char_style_names.get(key)
        if sid is None:
            if key not in self._warned_char_styles:
                self.report.warn(
                    "texwordcharstyle",
                    f"\\texwordcharstyle: character style {name!r} not found in the "
                    "reference template",
                )
                self._warned_char_styles.add(key)
            return None
        return sid

    def _styled_paragraph(self, style: str) -> _Element:
        style = self._style_remap.get(style, style)  # \texwordstyle paragraph roles
        p = el("w:p")
        ppr = sub(p, "w:pPr")
        sub(ppr, "w:pStyle", **{"w:val": style})
        return p

    def _set_align(self, p: _Element, align: str) -> None:
        if align == "left":
            return
        ppr = p.find(_qn("w:pPr"))
        assert ppr is not None
        jc = {"center": "center", "right": "end"}.get(align, "start")
        sub(ppr, "w:jc", **{"w:val": jc})

    def _sect_pr(self, columns: int | None = None, continuous: bool = False) -> _Element:
        cols = self.columns if columns is None else max(columns, 1)
        sect = el("w:sectPr")
        # CT_SectPr child order: hdr/ftr refs, type, pgSz, pgMar, cols, …, titlePg.
        # Same page size + header/footer refs on every section so continuous breaks
        # (used for full-width figure*/table*) don't force a page break or drop the
        # running head.
        for tag, w_type, rid in self.header_footer_refs:
            sub(sect, f"w:{tag}", **{"w:type": w_type, "r:id": rid})
        if continuous:
            sub(sect, "w:type", **{"w:val": "continuous"})
        pgsz = self.page_pgsz or {"w:w": "12240", "w:h": "15840"}
        pgmar = self.page_pgmar or {
            "w:top": "1440", "w:right": "1440", "w:bottom": "1440",
            "w:left": "1440", "w:header": "720", "w:footer": "720",
        }
        sub(sect, "w:pgSz", **pgsz)
        sub(sect, "w:pgMar", **pgmar)
        # A section with no w:cols is single-column (the schema default), so only a
        # multi-column section needs one; a 1-col region after a break resets to a
        # single column simply by omitting it.
        if cols > 1:
            sub(sect, "w:cols", **{"w:num": str(cols), "w:space": "720"})
        if any(w_type == "first" for _, w_type, _ in self.header_footer_refs):
            sub(sect, "w:titlePg")
        return sect


_STYLE_FLAG = {
    "bold": "bold", "italic": "italic", "underline": "underline",
    "typewriter": "typewriter", "smallcaps": "smallcaps", "hyperlink": "hyperlink",
    "superscript": "superscript", "subscript": "subscript",
    "strike": "strike", "highlight": "highlight",
}


@dataclass(frozen=True)
class _RunStyle:
    """Accumulated character formatting, applied at the text leaf."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    typewriter: bool = False
    smallcaps: bool = False
    hyperlink: bool = False
    superscript: bool = False
    subscript: bool = False
    strike: bool = False
    highlight: bool = False
    color: str | None = None
    shade: str | None = None
    size: int | None = None
    char_style: str | None = None

    def kwargs(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v}

    def with_flag(self, flag: str) -> _RunStyle:
        return _replace(self, **{flag: True})  # type: ignore[arg-type]


def _bookmark_for(label: str) -> str:
    from ..transforms.crossref import sanitize_bookmark

    return sanitize_bookmark(label)


def _has_visible_inline(inlines: list[ir.Inline]) -> bool:
    """Whether *inlines* hold any rendered content (not just whitespace text)."""
    return any(
        not (isinstance(x, ir.Text) and not x.value.strip()) for x in inlines
    )


def _mark_alignment(seg: list[_Element]) -> None:
    """Mark the start of an ``&`` segment as an ``m:aln`` alignment point.

    Word lines up rows of an ``m:eqArr`` at their ``m:aln`` marks, so flagging
    the first run after each ``&`` makes the relation signs align. A non-run
    first element (e.g. a fraction) gets an empty marker run prepended."""
    if not seg:
        return
    first = seg[0]
    if first.tag == qn("m:r"):
        rpr = first.find(qn("m:rPr"))
        if rpr is None:
            rpr = el("m:rPr")
            first.insert(0, rpr)
        if rpr.find(qn("m:aln")) is None:
            sub(rpr, "m:aln")  # aln is last in CT_RPr; append
    else:
        marker = el("m:r")
        sub(sub(marker, "m:rPr"), "m:aln")
        seg.insert(0, marker)


def _figure_bookmark(fig: ir.Figure) -> str | None:
    from ..transforms.crossref import figure_bookmark

    return figure_bookmark(fig)


def _qn(tag: str) -> str:
    from .ooxml import qn

    return qn(tag)


def _set_run_char_style(run: _Element, style: str | None) -> None:
    """Apply a Word character style to a ``w:r`` run in-place."""
    if not style or run.tag != _qn("w:r"):
        return
    rpr = run.find(_qn("w:rPr"))
    if rpr is None:
        rpr = el("w:rPr")
        run.insert(0, rpr)
    existing = rpr.find(_qn("w:rStyle"))
    if existing is not None:
        rpr.remove(existing)
    rpr.insert(0, el("w:rStyle", **{"w:val": style}))


def _style_math_runs(omath: _Element, color: str | None, size: int | None) -> None:
    """Apply ``w:color``/``w:sz`` to every math run (``m:r``) of an OMML tree.

    A math run may carry a ``w:rPr`` (WordprocessingML run properties) alongside
    its ``m:rPr`` (math run properties); it goes after ``m:rPr`` and before the
    run content. This lets ``\\textcolor{red}{$x$}`` / ``{\\large $x$}`` colour
    and resize an equation.
    """
    for mr in omath.iter(_qn("m:r")):
        wrpr = el("w:rPr")
        if color:
            sub(wrpr, "w:color", **{"w:val": color})
        if size:
            sub(wrpr, "w:sz", **{"w:val": str(size)})
            sub(wrpr, "w:szCs", **{"w:val": str(size)})
        mrpr = mr.find(_qn("m:rPr"))
        if mrpr is not None:
            mrpr.addnext(wrpr)
        else:
            mr.insert(0, wrpr)
