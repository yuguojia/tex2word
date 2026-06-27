"""End-to-end orchestration: LaTeX source -> IR -> transforms -> .docx."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ir
from .backend.document import DocumentWriter
from .backend.numbering import numbering_xml
from .backend.package import DocxPackage
from .frontend import parse_document
from .report import ConversionReport
from .roundtrip import build_manifest
from .templates import load_styles_xml
from .transforms import resolve_crossrefs


@dataclass
class ConversionResult:
    document: ir.Document
    report: ConversionReport
    docx: bytes


def convert_source(
    source: str,
    base_dir: str = ".",
    *,
    embed_manifest: bool = True,
    number_by_section: bool = False,
    citation_mode: str = "static",
    columns: int = 1,
    frontend: str = "pure",
    math_image_fallback: bool = False,
    csl: str | None = None,
    reference_doc: str | None = None,
    language: str | None = None,
    caption_locale: str = "auto",
) -> ConversionResult:
    """Convert a LaTeX string to a ``.docx`` (bytes) + IR + report.

    When ``embed_manifest`` is set (default), the IR is persisted as a custom
    part inside the ``.docx`` to support round-tripping. With
    ``number_by_section`` figures/tables/equations are numbered ``N.M`` per
    section instead of with a flat counter. ``citation_mode`` is ``"static"``
    (formatted text) or ``"zotero"`` (live ``CSL_CITATION`` fields). ``columns``
    sets the page column count; the default (1) auto-detects
    ``\documentclass[twocolumn]``/``\twocolumn``/``multicols`` and any value >1
    overrides it (``figure*``/``table*`` and the title/abstract span all columns).
    ``frontend`` is ``"pure"`` (default, pylatexenc)
    or ``"latexml"`` (genuine TeX expansion; falls back to pure if unavailable).
    ``reference_doc`` is a path to a Word ``.docx`` whose styles, theme and page
    geometry the output adopts (the journal/corporate "template" pattern); it
    takes priority over an in-source ``\\texwordtemplate{...}`` directive.
    ``caption_locale`` (``auto``/``en``/``zh-CN``) sets the caption and
    cross-reference wording; ``auto`` picks Chinese (图/表 + ``-`` separator) when
    the document language is ``zh-CN`` or a CJK font is set.
    """
    if frontend == "latexml":
        from .frontend.latexml import parse_document as _parse
        doc, report = _parse(source, base_dir)
    else:
        doc, report = parse_document(source, base_dir, csl_path=csl)
    resolve_crossrefs(doc, report)

    # The document preamble (TikZ libraries, colours, macros) is needed to
    # compile any TikZ figures to images; derive it from the flattened source.
    try:
        from .frontend.parser import _split_document
        from .frontend.preprocess import preprocess as _preprocess

        _, preamble = _split_document(_preprocess(source, base_dir))
    except Exception:
        preamble = ""

    image_renderer = None
    if math_image_fallback:
        from .mathml.imagemath import default_renderer

        image_renderer = default_renderer()
        if image_renderer is None:
            report.warn("math", "no math-image backend (install tex2word[mathimg] or TeX)")

    reference = _load_reference(reference_doc, doc.meta.template_doc, base_dir, report)
    hf_refs, hf_parts, hf_rels, hf_extra = _header_footer_wiring(reference, report)
    if not hf_refs and doc.meta.running_head:
        # no template headers -> synthesise a running-head header + page footer
        _add_running_head(doc.meta.running_head, hf_refs, hf_parts, hf_rels)

    # explicit columns>1 wins; otherwise honour the column count detected from the
    # document class (\documentclass[twocolumn]/\twocolumn/multicols).
    effective_columns = columns if columns and columns > 1 else max(doc.meta.columns, 1)

    # \texwordstyle bindings: appendix1..4 / part / figure / caption -> styleIds.
    roles = _resolve_role_styles(doc.meta, reference, report)

    if language is not None:
        doc.meta.language = language  # CLI/API override of the detected language
    # Localisable caption/cross-ref wording: a locale preset (auto keys off the
    # document language or a CJK font) overlaid with any \texwordcaption overrides.
    from .backend.caption_config import CaptionConfig

    has_cjk_font = bool(
        doc.meta.cjk_main_font or doc.meta.cjk_sans_font or doc.meta.cjk_mono_font
    )
    caption_config = CaptionConfig.from_locale(
        caption_locale, doc.meta.language, has_cjk_font=has_cjk_font,
    ).with_overrides(doc.meta.caption_overrides)
    # In a Chinese document, directly-typed curly quotes (“”‘’) get an East-Asian
    # font hint so Word renders them with the CJK font (full-width quotes); quotes
    # from LaTeX commands stay English. Same "is Chinese" test as the caption locale.
    cjk_quote_hint = CaptionConfig.is_chinese_locale(
        caption_locale, doc.meta.language, has_cjk_font=has_cjk_font
    )

    from .backend.numbering import NumIds, reference_num_ids

    # With a reference template we carry its numbering.xml verbatim, so the body's
    # role numIds must clear the template's own; without one, the bundled 1-5.
    num_ids = (
        reference_num_ids(reference.raw_numbering)
        if reference and reference.raw_numbering is not None
        else NumIds()
    )
    writer = DocumentWriter(
        report,
        base_dir=base_dir,
        image_math_renderer=image_renderer,
        number_by_section=number_by_section,
        citation_mode=citation_mode,
        columns=effective_columns,
        page_pgsz=reference.page_pgsz if reference else None,
        page_pgmar=reference.page_pgmar if reference else None,
        header_footer_refs=hf_refs,
        preamble=preamble,
        appendix_style_ids=roles.appendix,
        part_style_id=roles.part,
        figure_style_id=roles.figure,
        caption_style_ids=roles.caption_styles(),
        table_text_style_id=roles.table_text,
        threeline_table_style_id=roles.threeline_table,
        body_style_id=roles.body,
        style_remap=roles.style_remap,
        par_style_names=roles.par_style_names,
        caption_config=caption_config,
        cjk_quote_hint=cjk_quote_hint,
        num_ids=num_ids,
    )
    document_xml = writer.build(doc)
    styles_xml = reference.styles_xml if reference else load_styles_xml()
    if doc.meta.language:
        from .templates import apply_language

        styles_xml = apply_language(styles_xml, doc.meta.language)
    if doc.meta.main_font or doc.meta.cjk_main_font or doc.meta.cjk_sans_font \
            or doc.meta.cjk_mono_font:
        from .templates import apply_fonts

        styles_xml = apply_fonts(
            styles_xml,
            main=doc.meta.main_font,
            cjk_main=doc.meta.cjk_main_font,
            cjk_sans=doc.meta.cjk_sans_font,
            cjk_mono=doc.meta.cjk_mono_font,
        )
    numbering = numbering_xml()
    if reference and reference.raw_numbering is not None:
        from .backend.numbering import reference_numbering

        # carry the template's numbering verbatim + our role numIds (num_ids);
        # the template's lists and the styles that reference them stay untouched,
        # so heading "第1章" / appendix / bullet styles keep working.
        numbering = reference_numbering(
            reference.raw_numbering, reference.heading_rename, num_ids,
            appendix_ids=roles.appendix, part_id=roles.part,
        ) or numbering_xml()

    from .templates.reference import merge_notes

    package = DocxPackage(
        document_xml=document_xml,
        styles_xml=styles_xml,
        numbering_xml=numbering,
        document_rels=writer.document_rels + hf_rels,
        media=writer.media,
        comments=writer.comments_xml(),
        manifest=build_manifest(doc) if embed_manifest else None,
        theme=reference.theme_xml if reference else None,
        settings=reference.settings_xml if reference else None,
        footnotes=merge_notes(
            reference.footnotes_xml if reference else None, writer.footnotes_xml()
        ),
        endnotes=merge_notes(
            reference.endnotes_xml if reference else None, writer.endnotes_xml()
        ),
        header_footer_parts=hf_parts,
        extra_parts=hf_extra,
    )
    return ConversionResult(document=doc, report=report, docx=package.to_bytes())


_HF_REL_TYPE = {
    "header": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
    "footer": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
}


def _header_footer_wiring(reference, report: ConversionReport):
    """Turn carried headers/footers into (sectPr refs, .xml parts, doc rels, extra parts)."""
    refs: list[tuple[str, str, str]] = []
    parts: dict[str, bytes] = {}
    rels: list[str] = []
    extra: dict[str, bytes] = {}
    if reference and reference.headers_footers:
        for i, hf in enumerate(reference.headers_footers, 1):
            rid = f"rIdHF{i}"
            parts[f"word/{hf.part_name}"] = hf.content
            refs.append((f"{hf.kind}Reference", hf.w_type, rid))
            rels.append(
                f'<Relationship Id="{rid}" Type="{_HF_REL_TYPE[hf.kind]}" '
                f'Target="{hf.part_name}"/>'
            )
            if hf.rels:
                extra[f"word/_rels/{hf.part_name}.rels"] = hf.rels
            extra.update(hf.media)
    if reference and reference.skipped_header_footers:
        report.info("reference-doc",
                    f"skipped {reference.skipped_header_footers} header/footer(s) "
                    "with unsupported sub-resources")
    return refs, parts, rels, extra


def _add_running_head(
    running_head: str,
    refs: list[tuple[str, str, str]],
    parts: dict[str, bytes],
    rels: list[str],
) -> None:
    """Synthesise a default running-head header + page-number footer in place."""
    from .backend import runninghead

    parts["word/header_rh.xml"] = runninghead.header_xml(running_head)
    parts["word/footer_rh.xml"] = runninghead.footer_xml()
    refs.append(("headerReference", "default", "rIdRunHead"))
    refs.append(("footerReference", "default", "rIdRunFoot"))
    rels.append(
        f'<Relationship Id="rIdRunHead" Type="{_HF_REL_TYPE["header"]}" '
        'Target="header_rh.xml"/>'
    )
    rels.append(
        f'<Relationship Id="rIdRunFoot" Type="{_HF_REL_TYPE["footer"]}" '
        'Target="footer_rh.xml"/>'
    )


# \texwordstyle caption roles -> the caption "kind" the writer keys on.
_CAPTION_ROLE_KIND = {
    "figurecaption": "Figure",
    "tablecaption": "Table",
    "subfigurecaption": "subfigure",
    "algorithmcaption": "Algorithm",
}

# Generic paragraph-style roles -> (canonical styleId the writer emits, display
# names to auto-discover by in the template when the role is left unbound).
_PARAGRAPH_STYLE_ROLES = {
    "title":        ("Title",        ["title"]),
    "subtitle":     ("Subtitle",     ["subtitle"]),
    "abstract":     ("Abstract",     ["abstract"]),
    "sourcecode":   ("SourceCode",   ["source code", "sourcecode"]),
    "quote":        ("Quote",        ["quote"]),
    "bibliography": ("Bibliography", ["bibliography"]),
    "footnote":     ("FootnoteText", ["footnote text", "footnote"]),
}


@dataclass
class _RoleStyles:
    """Resolved ``\\texwordstyle`` bindings: template styleIds per logical role."""

    appendix: list = field(default_factory=lambda: [None, None, None, None])  # levels 1..4
    part: str | None = None
    figure: str | None = None  # the image-line paragraph style
    caption: str | None = None  # default caption style
    captions: dict = field(default_factory=dict)  # caption kind -> styleId (per type)
    table_text: str | None = None  # paragraph style for text inside table cells
    threeline_table: str | None = None  # Word table style for a 三线表 (first cmd \toprule)
    body: str | None = None  # paragraph style for ordinary body-text (正文) paragraphs
    style_remap: dict = field(default_factory=dict)  # canonical styleId -> effective
    # {lower-cased template style name -> effective styleId}: lets a per-paragraph
    # \texwordparstyle{name} directive name any reference-doc style by display name.
    par_style_names: dict = field(default_factory=dict)

    def caption_styles(self) -> dict:
        """{caption kind -> styleId}, each per-type override falling back to caption."""
        return {kind: self.captions.get(kind) or self.caption
                for kind in _CAPTION_ROLE_KIND.values()}


def _assign_role(styles: "_RoleStyles", role: str, sid: str) -> None:
    """Record a resolved styleId for *role* on the right field of *styles*."""
    if role == "part":
        styles.part = sid
    elif role == "figure":
        styles.figure = sid
    elif role == "caption":
        styles.caption = sid
    elif role == "table":
        styles.table_text = sid
    elif role == "threelinetable":
        styles.threeline_table = sid
    elif role == "body":
        styles.body = sid
    elif role in _CAPTION_ROLE_KIND:
        styles.captions[_CAPTION_ROLE_KIND[role]] = sid
    elif role.startswith("appendix"):
        styles.appendix[int(role[-1]) - 1] = sid
    elif role in _PARAGRAPH_STYLE_ROLES:
        styles.style_remap[_PARAGRAPH_STYLE_ROLES[role][0]] = sid


def _resolve_role_styles(meta, reference, report: ConversionReport) -> "_RoleStyles":
    """Resolve ``\\texwordstyle`` role->style bindings to reference-template styleIds.

    An explicit ``\\texwordstyle{role}{name}`` binds by style *name*. When a
    generic paragraph role is left unbound, its style is auto-discovered by name in
    the template (the role keyword / the built-in's display name); failing that the
    bundled built-in style is used. Name resolution follows the styleId
    normalization the styles merge applies (a localised built-in renamed to our
    canonical id), so a discovered id is always valid in the merged styles. A
    binding naming a style the template lacks is warned about and left unresolved.
    """
    styles = _RoleStyles()
    overrides = getattr(meta, "style_overrides", None) or {}
    name_to_id = reference.style_name_to_id if reference else {}
    heading_rename = reference.heading_rename if reference else {}

    def resolve(name: str) -> str | None:
        sid = name_to_id.get(name.strip().lower())
        return heading_rename.get(sid, sid) if sid else None

    # every template style by display name -> effective styleId, for the
    # per-paragraph \texwordparstyle{name} directive (resolved at write time).
    styles.par_style_names = {
        name: heading_rename.get(sid, sid) for name, sid in name_to_id.items()
    }

    for role, name in overrides.items():
        sid = resolve(name)
        if sid is None:
            where = "reference template" if reference else "(no --reference-doc given)"
            report.warn("reference-doc",
                        f"\\texwordstyle: style {name!r} for '{role}' not found in {where}")
            continue
        _assign_role(styles, role, sid)

    # unbound generic paragraph roles: auto-discover by name, else keep built-in.
    for role, (canonical, names) in _PARAGRAPH_STYLE_ROLES.items():
        if role in overrides:
            continue  # explicit binding already handled (or warned) above
        for cand in names:
            sid = resolve(cand)
            if sid and sid != canonical:
                styles.style_remap[canonical] = sid
                break
    return styles


def _load_reference(
    reference_doc: str | None,
    template_doc: str | None,
    base_dir: str,
    report: ConversionReport,
):
    """Load the Word reference template, or warn + fall back to the bundled styles.

    The CLI ``--reference-doc`` (``reference_doc``) takes priority over an
    in-source ``\\texwordtemplate{...}`` directive (``template_doc``); a relative
    ``\\texwordtemplate`` path is resolved against the ``.tex`` file's directory.
    """
    path = reference_doc
    if path is None and template_doc:
        path = (
            template_doc
            if os.path.isabs(template_doc)
            else os.path.join(base_dir, template_doc)
        )
    if not path:
        return None
    from .templates.reference import extract_reference

    try:
        with open(path, "rb") as fh:
            ref = extract_reference(fh.read())
        report.info("reference-doc", f"using template styles from {path}")
        return ref
    except (OSError, ValueError) as exc:
        report.warn("reference-doc", f"ignored ({exc}); used the built-in styles")
        return None


def convert_file(
    input_path: str,
    output_path: str | None = None,
    *,
    embed_manifest: bool = True,
    number_by_section: bool = False,
    citation_mode: str = "static",
    columns: int = 1,
    frontend: str = "pure",
    math_image_fallback: bool = False,
    csl: str | None = None,
    reference_doc: str | None = None,
    language: str | None = None,
    caption_locale: str = "auto",
) -> tuple[str, ConversionResult]:
    """Convert a ``.tex`` file to ``.docx`` on disk. Returns the output path."""
    with open(input_path, encoding="utf-8") as fh:
        source = fh.read()
    base_dir = os.path.dirname(os.path.abspath(input_path))
    result = convert_source(
        source, base_dir, embed_manifest=embed_manifest,
        number_by_section=number_by_section, citation_mode=citation_mode,
        columns=columns, frontend=frontend, math_image_fallback=math_image_fallback,
        csl=csl, reference_doc=reference_doc, language=language,
        caption_locale=caption_locale,
    )

    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".docx"
    with open(output_path, "wb") as fh:
        fh.write(result.docx)
    return output_path, result
