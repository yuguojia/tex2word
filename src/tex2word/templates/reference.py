"""Reference-document ("template") support for the ``--reference-doc`` option.

The flagship adoption feature (PRD v3, V5-1): convert onto the named styles,
theme and page geometry of a user-supplied Word template so the output matches a
journal's or organisation's required look -- while keeping tex2word's live
fields intact.

We do *not* clone the reference wholesale (that would lose our content); instead
we lift its styling parts -- ``styles.xml`` (merged with the few custom styles we
require), the theme, and the body section geometry (page size + margins) -- and
emit our own ``document.xml`` against them. Our writer already references the
standard Word style ids (``Title``/``Heading1``..``Heading5``/``Caption``/
``Quote``/``Normal``/...), so a template's definitions of those ids simply take
effect.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from . import load_styles_xml

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _w(name: str) -> str:
    return f"{{{_W}}}{name}"


# image extensions our package already declares a content-type Default for, so a
# carried header/footer logo needs no extra content-type plumbing.
_CARRY_IMAGE_EXT = {"png", "jpg", "jpeg", "emf"}


@dataclass
class HeaderFooter:
    """A carried header/footer part from the reference template."""

    kind: str  # "header" or "footer"
    w_type: str  # "default" / "first" / "even"
    part_name: str  # archive basename, e.g. "header1.xml"
    content: bytes
    rels: bytes | None = None  # rewritten word/_rels/<part>.rels, if it has any
    media: dict[str, bytes] = field(default_factory=dict)  # arcname -> bytes


@dataclass
class ReferenceParts:
    """Styling parts lifted from a reference ``.docx``."""

    styles_xml: bytes  # the reference styles, merged with our required styles
    settings_xml: bytes | None = None  # template settings.xml (compat/advanced opts) + updateFields
    footnotes_xml: bytes | None = None  # template footnotes.xml, separator notes only
    endnotes_xml: bytes | None = None  # template endnotes.xml, separator notes only
    raw_numbering: bytes | None = None  # the template's word/numbering.xml, if present
    style_name_to_id: dict[str, str] = field(default_factory=dict)  # w:name -> styleId
    heading_rename: dict[str, str] = field(default_factory=dict)  # template id -> our id
    theme_xml: bytes | None = None  # word/theme/theme1.xml, if present
    page_pgsz: dict[str, str] | None = None  # body w:pgSz attributes
    page_pgmar: dict[str, str] | None = None  # body w:pgMar attributes
    headers_footers: list[HeaderFooter] = field(default_factory=list)
    skipped_header_footers: int = 0  # carried only rels-free parts; count skipped


def extract_reference(docx_bytes: bytes) -> ReferenceParts:
    """Lift the styling parts from a reference ``.docx``.

    Raises :class:`ValueError` if it is not a readable docx with a ``styles.xml``.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
        names = set(zf.namelist())
        if "word/styles.xml" not in names:
            raise ValueError("reference docx has no word/styles.xml")
        styles_bytes = zf.read("word/styles.xml")
        styles = merge_styles(styles_bytes, load_styles_xml())
        settings = (
            merge_settings(zf.read("word/settings.xml"))
            if "word/settings.xml" in names else None
        )
        footnotes = (
            _separator_notes(zf.read("word/footnotes.xml"), "footnote")
            if "word/footnotes.xml" in names else None
        )
        endnotes = (
            _separator_notes(zf.read("word/endnotes.xml"), "endnote")
            if "word/endnotes.xml" in names else None
        )
        styles_root = etree.fromstring(styles_bytes)
        heading_rename = _compute_builtin_rename(styles_root)
        name_to_id = _style_name_to_id(styles_root)
        raw_numbering = zf.read("word/numbering.xml") if "word/numbering.xml" in names else None
        theme = None
        # the theme part name varies (theme1.xml); take the first under word/theme/
        theme_name = next(
            (n for n in sorted(names) if n.startswith("word/theme/") and n.endswith(".xml")),
            None,
        )
        if theme_name is not None:
            theme = zf.read(theme_name)
        pgsz = pgmar = None
        hfs: list[HeaderFooter] = []
        skipped = 0
        if "word/document.xml" in names:
            body = etree.fromstring(zf.read("word/document.xml")).find(_w("body"))
            if body is not None:
                sects = list(body.iter(_w("sectPr")))
                target = _target_sect_index(body, sects)
                if target is not None:
                    pgsz, pgmar = _page_geometry(sects, target)
                    hfs, skipped = _headers_footers(sects, target, zf, names)
        return ReferenceParts(styles_xml=styles, settings_xml=settings,
                              footnotes_xml=footnotes, endnotes_xml=endnotes,
                              raw_numbering=raw_numbering,
                              style_name_to_id=name_to_id, heading_rename=heading_rename,
                              theme_xml=theme, page_pgsz=pgsz, page_pgmar=pgmar,
                              headers_footers=hfs, skipped_header_footers=skipped)
    except ValueError:
        raise
    except Exception as exc:  # corrupt zip / malformed XML
        raise ValueError(f"unreadable reference docx: {exc}") from exc


# Word built-in style *names* -> the styleId our writer emits. Word stores a
# built-in style's English name in ``w:name`` even in localized files, but the
# styleId is language/version specific: a Chinese template saves ``heading 1``
# as styleId ``1``, ``Normal`` as ``a``, ``Title`` as ``a8``, ``caption`` as
# ``a7``, ... Our document body references the canonical English ids, so without
# remapping a template's built-in definitions would never bind to our content.
_BUILTIN_NAME_TO_ID = {
    "normal": "Normal",
    "title": "Title",
    "subtitle": "Subtitle",
    "heading 1": "Heading1",
    "heading 2": "Heading2",
    "heading 3": "Heading3",
    "heading 4": "Heading4",
    "heading 5": "Heading5",
    "caption": "Caption",
    "quote": "Quote",
    "hyperlink": "Hyperlink",
    "footnote text": "FootnoteText",
    "footnote reference": "FootnoteReference",
    "bibliography": "Bibliography",
}


def _compute_builtin_rename(root: etree._Element) -> dict[str, str]:
    """{template styleId -> the styleId our writer emits} for built-in styles.

    Match by built-in *name* (``w:name``, recorded in English even in localized
    templates). Skips remaps that would clobber an id the template already uses
    for a different style, or collapse two template styles onto the same target.
    """
    styles = root.findall(_w("style"))
    existing = {s.get(_w("styleId")) for s in styles}
    rename: dict[str, str] = {}
    for style in styles:
        old = style.get(_w("styleId"))
        name_el = style.find(_w("name"))
        name = name_el.get(_w("val")) if name_el is not None else None
        if not old or not name:
            continue
        target = _BUILTIN_NAME_TO_ID.get(name.strip().lower())
        if not target or target == old:
            continue
        if target in existing or target in rename.values():
            continue
        rename[old] = target
    return rename


def _style_name_to_id(root: etree._Element) -> dict[str, str]:
    """{lower-cased w:name -> styleId} for the template's styles.

    Lets a ``\\texwordstyle{role}{name}`` directive name a style by its display
    name (what the user sees in Word) and have us resolve it to the styleId the
    body and numbering reference. Non-built-in styleIds are kept as-is, so a
    resolved id is valid in both the merged styles and the carried numbering.
    """
    out: dict[str, str] = {}
    for style in root.findall(_w("style")):
        sid = style.get(_w("styleId"))
        name_el = style.find(_w("name"))
        name = name_el.get(_w("val")) if name_el is not None else None
        if sid and name:
            out.setdefault(name.strip().lower(), sid)
    return out


def _normalize_builtin_ids(root: etree._Element) -> None:
    """Rename a template's built-in styles to the styleIds our writer emits.

    Match by built-in *name* (``w:name``, recorded in English even in localized
    templates) and rewrite the ``w:styleId`` plus every intra-styles reference to
    it (``basedOn`` / ``next`` / ``link``). This makes a localized or older
    template's standard styles (``heading 1`` saved as styleId ``1``, ...)
    actually take effect, instead of our bundled fallbacks being appended under
    the English ids the body uses.
    """
    rename = _compute_builtin_rename(root)
    if not rename:
        return
    styles = root.findall(_w("style"))
    for style in styles:
        sid = style.get(_w("styleId"))
        if sid in rename:
            style.set(_w("styleId"), rename[sid])
    ref_tags = {_w("basedOn"), _w("next"), _w("link")}
    for el in root.iter():
        val = el.get(_w("val"))
        if val is not None and el.tag in ref_tags and val in rename:
            el.set(_w("val"), rename[val])


def merge_styles(reference_styles: bytes, our_styles: bytes) -> bytes:
    """Reference styles, augmented with any of *our* styles it doesn't define.

    The reference's definitions of standard ids (``Heading1``, ``Title``, ...)
    win -- that is the whole point. Built-in styles saved under localized/short
    styleIds are first normalized to the English ids our writer emits (see
    :func:`_normalize_builtin_ids`). We then append only the custom styles our
    writer relies on (``SourceCode``, ``Abstract``, ``Bibliography``,
    ``Hyperlink``, footnote styles, ...) when the template lacks them, so no
    content renders unstyled.
    """
    ref_root = etree.fromstring(reference_styles)
    _normalize_builtin_ids(ref_root)
    have = {
        s.get(_w("styleId"))
        for s in ref_root.findall(_w("style"))
        if s.get(_w("styleId"))
    }
    our_root = etree.fromstring(our_styles)
    for style in our_root.findall(_w("style")):
        sid = style.get(_w("styleId"))
        if sid and sid not in have:
            ref_root.append(style)
            have.add(sid)
    return etree.tostring(ref_root, xml_declaration=True, encoding="UTF-8", standalone=True)


#: ``w:settings`` children that, per ECMA-376 CT_Settings, come at/after the
#: ``w:updateFields`` slot -- the insertion point for our updateFields element.
_SETTINGS_AFTER_UPDATEFIELDS = frozenset({
    "hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "rsids", "mathPr",
    "uiCompat97To2003", "attachedSchema", "themeFontLang", "clrSchemeMapping",
    "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures", "forceUpgrade",
    "captions", "readModeInkLockDown", "smartTagType", "shapeDefaults",
    "doNotEmbedSmartTags", "decimalSymbol", "listSeparator", "docId",
    "defaultImageDpi", "chartTrackingRefBased",
})
#: settings we drop when carrying a template's settings.xml: relationship-bearing
#: ``w:attachedTemplate`` (would dangle without settings.xml.rels) and the two
#: protection elements (would make the output read-only, defeating editable output).
_DROP_SETTINGS = frozenset({"attachedTemplate", "writeProtection", "documentProtection"})


def merge_settings(reference_settings: bytes) -> bytes:
    """Carry a template's ``settings.xml`` (compat/advanced options) + updateFields.

    This preserves the reference document's advanced/compatibility options -- the
    ``w:compat`` block (e.g. ``doNotExpandShiftReturn`` -- "don't expand character
    spacing on a line ended with Shift+Enter"), ``w:characterSpacingControl``,
    ``w:defaultTabStop``, ``w:mathPr``, kerning/drawing-grid settings, etc.

    We strip elements that would break the output: ``w:attachedTemplate`` and any
    element bearing a relationship (``r:*``) attribute -- we do not carry
    ``settings.xml.rels``, so those would dangle -- and ``w:writeProtection`` /
    ``w:documentProtection``, which would lock the document against editing. We
    then (re)insert ``w:updateFields`` at its canonical position so Word still
    recalculates ``SEQ``/``REF`` fields on first open (the live-numbering feature).
    """
    root = etree.fromstring(reference_settings)
    for child in list(root):
        if not isinstance(child.tag, str):
            continue
        local = etree.QName(child).localname
        has_rel_attr = any(etree.QName(a).namespace == _R for a in child.attrib)
        if local in _DROP_SETTINGS or has_rel_attr or local == "updateFields":
            root.remove(child)
    update = etree.Element(_w("updateFields"))
    update.set(_w("val"), "true")
    insert_at = len(root)
    for i, child in enumerate(root):
        if (isinstance(child.tag, str)
                and etree.QName(child).localname in _SETTINGS_AFTER_UPDATEFIELDS):
            insert_at = i
            break
    root.insert(insert_at, update)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


#: ``w:type`` values of the structural notes (separator / continuation separator /
#: continuation notice) that every footnotes.xml / endnotes.xml carries -- these,
#: not the body's content notes, are what ``settings.xml`` references and what
#: gives the note area its look.
_SEPARATOR_NOTE_TYPES = frozenset({"separator", "continuationSeparator", "continuationNotice"})


def _separator_notes(part_bytes: bytes, note_local: str) -> bytes | None:
    """A footnotes/endnotes part reduced to its separator notes, or None.

    The reference template's ``footnotes.xml`` also holds the *content* notes of
    its sample body (ids >= 1), which belong to the document we discard -- and
    those are what carry hyperlink/image relationships. We keep only the
    separator / continuation-separator notes (ids -1 / 0), so the carried part is
    self-contained (no ``.rels``) and free of stray template content. Returns
    None when the part defines no separator notes.
    """
    root = etree.fromstring(part_bytes)
    type_attr = _w("type")
    kept = [n for n in root if isinstance(n.tag, str) and n.get(type_attr) in _SEPARATOR_NOTE_TYPES]
    if not kept:
        return None
    for child in list(root):
        root.remove(child)
    for note in kept:
        root.append(note)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def merge_notes(template_separators: bytes | None, generated: bytes | None) -> bytes | None:
    """Combine a template's separator notes with tex2word's generated notes part.

    The separator / continuation-separator definitions come from the template
    (preserving the reference document's note-area look) when it provides them;
    the actual content notes come from the converted document. Returns:

    * the merged part when the document has notes and the template has separators;
    * ``generated`` unchanged when the template has none;
    * the template's separator-only part when the document has no notes (so the
      ``settings.xml`` footnotePr/endnotePr separator references still resolve);
    * ``None`` when neither side contributes anything.
    """
    if template_separators is None:
        return generated
    if generated is None:
        return template_separators
    type_attr = _w("type")
    gen_root = etree.fromstring(generated)
    tpl_root = etree.fromstring(template_separators)
    content = [n for n in gen_root if isinstance(n.tag, str)
               and n.get(type_attr) not in _SEPARATOR_NOTE_TYPES]
    for child in list(gen_root):
        gen_root.remove(child)
    for sep in tpl_root:  # template separators (already separator-only) first
        gen_root.append(sep)
    for note in content:  # then the document's content notes
        gen_root.append(note)
    return etree.tostring(gen_root, xml_declaration=True, encoding="UTF-8", standalone=True)


#: A template author can drop this bookmark on a body paragraph to mark which
#: page/section's geometry + running headers/footers we should lift. Multi-section
#: templates (e.g. a thesis template) commonly leave their *final* section -- the
#: body-level sectPr -- without the running headers/footers the main-text sections
#: carry, so lifting "the last section" yields none. With the marker present we
#: lift the marked paragraph's section instead; without it we fall back to the
#: final section (the historical behaviour).
_SECTION_MARKER = "tex2word_section"


def _target_sect_index(body: etree._Element, sects: list[etree._Element]) -> int | None:
    """Index into *sects* of the section to lift, or None when there are none.

    Sections appear in document order (paragraph-nested sectPrs first, the
    body-level final sectPr last). When a paragraph is bookmarked
    ``tex2word_section`` we return the section that governs it; otherwise the last.
    """
    if not sects:
        return None
    marked = _marked_sect(body)
    if marked is not None:
        try:
            return sects.index(marked)
        except ValueError:
            pass
    return len(sects) - 1


def _marked_sect(body: etree._Element) -> etree._Element | None:
    """The sectPr governing the paragraph bookmarked ``tex2word_section``, or None.

    A paragraph belongs to the first section whose ``sectPr`` appears at or after
    it -- a later paragraph's ``pPr/sectPr`` (the section's last paragraph) or, if
    none follows, the body-level final sectPr. We locate the top-level body block
    holding the marker, then scan forward for that governing sectPr.
    """
    name_attr = _w("name")
    children = list(body)
    marked_index = next(
        (i for i, c in enumerate(children)
         if any(bm.get(name_attr) == _SECTION_MARKER for bm in c.iter(_w("bookmarkStart")))),
        None,
    )
    if marked_index is None:
        return None
    for child in children[marked_index:]:
        if child.tag == _w("sectPr"):  # the body-level final section
            return child
        if child.tag == _w("p"):
            ppr = child.find(_w("pPr"))
            sect = ppr.find(_w("sectPr")) if ppr is not None else None
            if sect is not None:
                return sect
    return None


def _resolve_hf_refs(
    sects: list[etree._Element], target: int
) -> dict[tuple[str, str], str]:
    """``{(kind, w:type) -> relationship id}`` for the target section.

    Header/footer slots the target section does not state itself are inherited
    from the nearest preceding section that does, mirroring Word's section
    inheritance (so a main-text section that only overrides the default header
    still carries the inherited first/even ones).
    """
    rid_attr = f"{{{_R}}}id"
    resolved: dict[tuple[str, str], str] = {}
    for sect in reversed(sects[:target + 1]):  # target wins, then nearer predecessors
        for ref in sect:
            if ref.tag == _w("headerReference"):
                kind = "header"
            elif ref.tag == _w("footerReference"):
                kind = "footer"
            else:
                continue
            rid = ref.get(rid_attr)
            if rid:
                resolved.setdefault((kind, ref.get(_w("type")) or "default"), rid)
    return resolved


def _headers_footers(
    sects: list[etree._Element], target: int, zf: zipfile.ZipFile, names: set[str]
) -> tuple[list[HeaderFooter], int]:
    """Carry the target section's header/footer parts (with image sub-resources).

    The target section is the ``tex2word_section``-marked one (or the final
    section); references it omits are inherited from earlier sections (see
    :func:`_resolve_hf_refs`). Running-title text + page-number fields carry
    directly; a header/footer that references **images** (a logo) carries its
    media too (namespaced under ``media/tmpl/`` with the rels rewritten).
    Anything we can't represent safely (a non-image internal relationship, an
    unsupported image type, a missing target) is skipped so we never emit a
    dangling relationship; external (URL) relationships are kept as-is. Returns
    (carried, skipped_count).
    """
    refs = _resolve_hf_refs(sects, target)
    if not refs:
        return [], 0
    rid_target = _rel_targets(zf, names)
    carried: list[HeaderFooter] = []
    skipped = 0
    for (kind, w_type), rid in refs.items():
        target_rel = rid_target.get(rid)
        if not target_rel:
            continue
        base = target_rel.rsplit("/", 1)[-1]
        part = f"word/{base}"
        if part not in names:
            skipped += 1
            continue
        sub = _carry_subresources(zf, names, base)
        if sub is None:  # an unsupported sub-resource -> skip, don't dangle a rel
            skipped += 1
            continue
        rels_bytes, media = sub
        carried.append(HeaderFooter(
            kind=kind, w_type=w_type,
            part_name=base, content=zf.read(part),
            rels=rels_bytes or None, media=media,
        ))
    return carried, skipped


def _carry_subresources(
    zf: zipfile.ZipFile, names: set[str], base: str
) -> tuple[bytes, dict[str, bytes]] | None:
    """Rewrite a header/footer's ``.rels`` + collect its image media, or None.

    Returns ``(rewritten_rels_bytes_or_empty, {arcname: bytes})``. ``None`` means
    the part has a sub-resource we can't carry safely (skip the whole part).
    """
    rels_path = f"word/_rels/{base}.rels"
    if rels_path not in names:
        return b"", {}  # no sub-rels at all
    try:
        root = etree.fromstring(zf.read(rels_path))
    except Exception:
        return None
    stem = base.rsplit(".", 1)[0]
    media: dict[str, bytes] = {}
    for rel in root.findall(f"{{{_PKG_REL}}}Relationship"):
        if (rel.get("TargetMode") or "Internal") == "External":
            continue  # a URL: no part to carry, keep the relationship as-is
        rtype = rel.get("Type") or ""
        target = rel.get("Target") or ""
        ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        if not rtype.endswith("/image") or ext not in _CARRY_IMAGE_EXT or ".." in target:
            return None
        src = posixpath.normpath(f"word/{target.lstrip('/')}")
        if src not in names:
            return None
        new_base = f"{stem}_{target.rsplit('/', 1)[-1]}"
        media[f"word/media/tmpl/{new_base}"] = zf.read(src)
        rel.set("Target", f"media/tmpl/{new_base}")
    rels_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return rels_bytes, media


def _rel_targets(zf: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    """{relationship id -> target} from word/_rels/document.xml.rels."""
    path = "word/_rels/document.xml.rels"
    if path not in names:
        return {}
    root = etree.fromstring(zf.read(path))
    out: dict[str, str] = {}
    for r in root.findall(f"{{{_PKG_REL}}}Relationship"):
        rid, target = r.get("Id"), r.get("Target")
        if rid and target:
            out[rid] = target
    return out


def _page_geometry(
    sects: list[etree._Element], target: int
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """The target section's page size + margins, inheriting from earlier sections.

    ``w:pgSz`` / ``w:pgMar`` the target section does not state itself are inherited
    from the nearest preceding section that does (Word's section inheritance).
    """

    def attrs(el: etree._Element | None) -> dict[str, str] | None:
        if el is None:
            return None
        return {
            f"w:{etree.QName(k).localname}": v.decode() if isinstance(v, bytes) else v
            for k, v in el.attrib.items()
        }

    def inherit(local: str) -> etree._Element | None:
        for sect in reversed(sects[:target + 1]):
            el = sect.find(_w(local))
            if el is not None:
                return el
        return None

    return attrs(inherit("pgSz")), attrs(inherit("pgMar"))
