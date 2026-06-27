"""Content-injection mode for ``\\texwordtemplate[keep]{TEMPLATE.docx}``.

The default ``--reference-doc`` / ``\\texwordtemplate`` behaviour lifts only a
template's *styling* (styles, theme, page geometry, headers/footers) onto a
freshly generated document -- the template's own body content is discarded. The
``[keep]`` mode instead keeps the template package intact and splices the
converted body **at the template's ``tex2word_section`` bookmark**, so a cover
page, front-matter, fixed boilerplate and section breaks the template author
wrote all survive, with the converted document dropped in at the marked point.

Implementation: we use the template ``.docx`` as the base package and overlay
only what the conversion produced -- our merged ``styles.xml`` / ``numbering.xml``
(+ ``settings.xml``), the converted body blocks (inserted after the bookmarked
paragraph), our images (namespaced under ``media/t2w/`` so they never clash with
the template's media), and our footnotes/endnotes/comments (their ids offset past
the template's) and the round-trip manifest. Relationship ids that would collide
with the template's are renamed.
"""

from __future__ import annotations

import io
import zipfile

from lxml import etree

from ..backend.package import (
    _FIXED_DATE,
    MANIFEST_PART,
    MANIFEST_REL_TYPE,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

#: the bookmark a template author drops on the paragraph after which the
#: converted body is inserted (Insert -> Bookmark -> name ``tex2word_section``).
_SECTION_MARKER = "tex2word_section"

#: separator / continuation notes carry a ``w:type``; real content notes do not.
_SEPARATOR_NOTE_TYPES = frozenset(
    {"separator", "continuationSeparator", "continuationNotice"}
)

_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

#: main-document part content types. A ``.dotx`` template uses the *template*
#: type; a ``.docx`` (what we emit) must use the *document* type or Word reports
#: the file as corrupt.
_TEMPLATE_MAIN_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
_DOCUMENT_MAIN_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)

_CONTENT_TYPE = {
    "footnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    "endnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
    "comments": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    "numbering": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    "styles": "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
    "settings": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
}
_REL_TYPE = {
    "footnotes": f"{_R}/footnotes",
    "endnotes": f"{_R}/endnotes",
    "comments": f"{_R}/comments",
    "numbering": f"{_R}/numbering",
    "styles": f"{_R}/styles",
    "settings": f"{_R}/settings",
}


def _w(name: str) -> str:
    return f"{{{_W}}}{name}"


def _int_attr(el: etree._Element, attr: str) -> int | None:
    """The integer value of *attr* on *el*, or None when absent / non-numeric."""
    raw = el.get(attr)
    if raw is not None and raw.lstrip("-").isdigit():
        return int(raw)
    return None


#: minimal settings.xml (with updateFields) for a template that ships none.
_MINIMAL_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<w:settings xmlns:w="{_W}"><w:updateFields w:val="true"/></w:settings>\n'
).encode()


def build_injected_docx(
    template_bytes: bytes,
    *,
    document_xml: bytes,
    styles_xml: bytes,
    numbering_xml: bytes,
    settings_xml: bytes | None,
    document_rels: list[str],
    media: dict[str, bytes],
    footnotes: bytes | None,
    endnotes: bytes | None,
    comments: bytes | None,
    manifest: bytes | None,
    heading_rename: dict[str, str] | None = None,
) -> bytes | None:
    """Splice the converted body into the template at ``tex2word_section``.

    Returns the assembled ``.docx`` bytes, or ``None`` when the template cannot be
    used this way (not a readable docx, or it has no ``tex2word_section``
    bookmark) -- the caller then falls back to the styling-only path.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(template_bytes))
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            return None
        parts: dict[str, bytes] = {n: zf.read(n) for n in names}
    except Exception:
        return None

    tdoc = etree.fromstring(parts["word/document.xml"])
    tbody = tdoc.find(_w("body"))
    if tbody is None:
        return None
    anchor = _find_marked_child(tbody)
    if anchor is None:
        return None  # no bookmark -> caller warns + falls back to styling-only

    # The merged styles.xml normalises the template's localised built-in style ids
    # to our canonical ones (e.g. a cover title saved as "aff9" -> "Title"). The
    # template's *kept* parts still reference the originals, so remap their style
    # references to match, or those paragraphs (the template's own cover/heading
    # lines) would lose their style. The spliced fragment already uses the
    # canonical ids (the rename's values, not its keys), so it stays untouched.
    rename = heading_rename or {}
    if rename:
        _remap_style_refs(tdoc, rename)  # the body (before the fragment is added)
        for part_name in _style_ref_parts(names):
            try:
                root = etree.fromstring(parts[part_name])
            except etree.XMLSyntaxError:
                continue
            _remap_style_refs(root, rename)
            parts[part_name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    # the converted body's blocks, minus its trailing section properties.
    gbody = etree.fromstring(document_xml).find(_w("body"))
    if gbody is None:
        return None
    fragment = [c for c in gbody if c.tag != _w("sectPr")]

    # ---- relationships + media (namespace ours so they can't clash) --------- #
    rels_root = _document_rels_root(parts)
    used_ids: set[str] = {
        rid
        for r in rels_root.findall(f"{{{_PKG_REL}}}Relationship")
        if (rid := r.get("Id")) is not None
    }
    rid_rename, new_media, media_exts = _relocate_image_rels(
        document_rels, media, used_ids, rels_root
    )
    _apply_rid_rename(fragment, rid_rename)
    parts.update(new_media)

    # ---- styles / numbering / settings: overwrite the parts in place -------- #
    overrides: dict[str, str] = {}  # partname -> content-type (ensure declared)
    parts["word/styles.xml"] = styles_xml
    _ensure_part_wired(parts, names, rels_root, used_ids, overrides,
                       "word/styles.xml", "styles")
    parts["word/numbering.xml"] = numbering_xml
    _ensure_part_wired(parts, names, rels_root, used_ids, overrides,
                       "word/numbering.xml", "numbering")
    if settings_xml is not None:
        parts["word/settings.xml"] = settings_xml
        _ensure_part_wired(parts, names, rels_root, used_ids, overrides,
                           "word/settings.xml", "settings")
    elif "word/settings.xml" not in names:
        # the template ships no settings of its own: add a minimal one so Word
        # still refreshes SEQ/REF fields (the live-numbering feature) on open.
        parts["word/settings.xml"] = _MINIMAL_SETTINGS
        _ensure_part_wired(parts, names, rels_root, used_ids, overrides,
                           "word/settings.xml", "settings")

    # ---- footnotes / endnotes / comments: merge + offset our ids ------------ #
    _inject_notes(parts, names, rels_root, used_ids, overrides,
                  "word/footnotes.xml", "footnotes",
                  footnotes, fragment, _w("footnoteReference"))
    _inject_notes(parts, names, rels_root, used_ids, overrides,
                  "word/endnotes.xml", "endnotes",
                  endnotes, fragment, _w("endnoteReference"))
    # (notes/comments offsets already rewrote the spliced body's references)
    _inject_comments(parts, names, rels_root, used_ids, overrides,
                     comments, fragment)

    # ---- round-trip manifest ------------------------------------------------ #
    if manifest is not None:
        parts[MANIFEST_PART] = manifest
        rid = _fresh_rid(used_ids)
        _add_rel(rels_root, rid, MANIFEST_REL_TYPE, "tex2word/manifest.json")
        overrides[f"/{MANIFEST_PART}"] = "application/json"

    # splice the converted body in at the bookmarked paragraph, then commit the
    # rels + content-types we touched. The bookmarked placeholder paragraph is
    # *replaced* by the converted body (so it leaves no stray empty page); a
    # section break it carried is preserved on a trailing empty paragraph so the
    # template's page layout is kept. A non-paragraph anchor (e.g. a bookmark in a
    # table) is kept and the body inserted after it instead.
    idx = list(tbody).index(anchor)
    for offset, frag in enumerate(fragment):
        tbody.insert(idx + offset, frag)
    if anchor.tag == _w("p"):
        sectpr = anchor.find(f"{_w('pPr')}/{_w('sectPr')}")
        if sectpr is not None:
            keep = etree.Element(_w("p"))
            etree.SubElement(keep, _w("pPr")).append(sectpr)
            tbody.insert(idx + len(fragment), keep)
        tbody.remove(anchor)
    parts["word/document.xml"] = etree.tostring(
        tdoc, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    parts["word/_rels/document.xml.rels"] = etree.tostring(
        rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    parts["[Content_Types].xml"] = _ensure_content_types(
        parts["[Content_Types].xml"], media_exts, overrides
    )
    return _zip_parts(parts)


# --------------------------------------------------------------------------- #
# style-reference remapping (keep the template's own paragraph styles working)
# --------------------------------------------------------------------------- #
#: elements whose ``w:val`` names a paragraph/character/table style id.
_STYLE_REF_TAGS = (_w("pStyle"), _w("rStyle"), _w("tblStyle"))


def _style_ref_parts(names: set[str]) -> list[str]:
    """Kept template parts (besides document.xml) that reference main styles.

    Headers, footers and footnote/endnote parts bind to ``styles.xml`` too, so
    they need the same built-in-id remap. The glossary keeps its *own*
    ``styles.xml`` (we don't normalise it), so it is deliberately excluded.
    """
    keep = []
    for n in names:
        if not n.endswith(".xml") or "/glossary/" in n:
            continue
        base = n.rsplit("/", 1)[-1]
        if base.startswith(("header", "footer")) or base in ("footnotes.xml", "endnotes.xml"):
            keep.append(n)
    return keep


def _remap_style_refs(root: etree._Element, rename: dict[str, str]) -> None:
    """Rewrite ``w:pStyle``/``w:rStyle``/``w:tblStyle`` ids in *root* per *rename*."""
    val = _w("val")
    for tag in _STYLE_REF_TAGS:
        for el in root.iter(tag):
            v = el.get(val)
            if v is not None and v in rename:
                el.set(val, rename[v])


# --------------------------------------------------------------------------- #
# bookmark lookup
# --------------------------------------------------------------------------- #
def _find_marked_child(body: etree._Element) -> etree._Element | None:
    """The top-level body child containing the ``tex2word_section`` bookmark."""
    name_attr = _w("name")
    for child in list(body):
        for bm in child.iter(_w("bookmarkStart")):
            if bm.get(name_attr) == _SECTION_MARKER:
                return child
    return None


# --------------------------------------------------------------------------- #
# relationships / media
# --------------------------------------------------------------------------- #
def _document_rels_root(parts: dict[str, bytes]) -> etree._Element:
    """The template's ``word/_rels/document.xml.rels`` root (created if absent)."""
    raw = parts.get("word/_rels/document.xml.rels")
    if raw is not None:
        return etree.fromstring(raw)
    return etree.Element(f"{{{_PKG_REL}}}Relationships")


def _add_rel(root: etree._Element, rid: str, rel_type: str, target: str) -> None:
    el = etree.SubElement(root, f"{{{_PKG_REL}}}Relationship")
    el.set("Id", rid)
    el.set("Type", rel_type)
    el.set("Target", target)


def _fresh_rid(used: set[str]) -> str:
    """A relationship id not already used by the template (``rIdT2W…``)."""
    i = 1
    while f"rIdT2W{i}" in used:
        i += 1
    rid = f"rIdT2W{i}"
    used.add(rid)
    return rid


def _relocate_image_rels(
    document_rels: list[str],
    media: dict[str, bytes],
    used_ids: set[str],
    rels_root: etree._Element,
) -> tuple[dict[str, str], dict[str, bytes], set[str]]:
    """Add our image relationships under ``media/t2w/`` so nothing clashes.

    Returns ``(rid_rename, relocated_media, extensions)``: the id rename map to
    apply to the spliced body, the media parts under their new archive paths, and
    the set of file extensions used (to declare content-type Defaults).
    """
    rid_rename: dict[str, str] = {}
    new_media: dict[str, bytes] = {}
    exts: set[str] = set()
    for raw in document_rels:
        rel = etree.fromstring(raw)
        old_id = rel.get("Id")
        target = rel.get("Target") or ""
        if not old_id or not target:
            continue
        base = target.rsplit("/", 1)[-1]
        new_target = f"media/t2w/{base}"
        old_part = f"word/{target.lstrip('/')}"
        if old_part in media:
            new_media[f"word/{new_target}"] = media[old_part]
        if "." in base:
            exts.add(base.rsplit(".", 1)[-1].lower())
        new_id = old_id if old_id not in used_ids else _fresh_rid(used_ids)
        if new_id != old_id:
            rid_rename[old_id] = new_id
        used_ids.add(new_id)
        _add_rel(rels_root, new_id, rel.get("Type") or _IMAGE_REL, new_target)
    return rid_rename, new_media, exts


def _apply_rid_rename(fragment: list[etree._Element], rename: dict[str, str]) -> None:
    """Rewrite ``r:*`` reference attributes in the spliced body per *rename*."""
    if not rename:
        return
    for top in fragment:
        for el in top.iter():
            for attr, val in list(el.attrib.items()):
                if etree.QName(attr).namespace == _R and val in rename:
                    el.set(attr, rename[val])


# --------------------------------------------------------------------------- #
# part wiring (rels + content-types)
# --------------------------------------------------------------------------- #
def _ensure_part_wired(
    parts: dict[str, bytes],
    template_names: set[str],
    rels_root: etree._Element,
    used_ids: set[str],
    overrides: dict[str, str],
    part_path: str,
    kind: str,
) -> None:
    """Make sure *part_path* has a document relationship + content-type Override.

    When the template already shipped the part its existing wiring is reused
    (we overwrote the bytes in place); otherwise we add the relationship and the
    Override so the freshly-added part resolves.
    """
    if part_path in template_names:
        return  # template already declares it; overwriting the bytes is enough
    target = part_path[len("word/"):]
    _add_rel(rels_root, _fresh_rid(used_ids), _REL_TYPE[kind], target)
    overrides[f"/{part_path}"] = _CONTENT_TYPE[kind]


def _inject_notes(
    parts: dict[str, bytes],
    template_names: set[str],
    rels_root: etree._Element,
    used_ids: set[str],
    overrides: dict[str, str],
    part_path: str,
    kind: str,
    generated: bytes | None,
    fragment: list[etree._Element],
    ref_tag: str,
) -> None:
    """Merge our footnotes/endnotes part into the template's, offsetting ids.

    No-op when the conversion produced no such notes. When the template has none
    of its own we add the part wholesale; otherwise our content notes are
    appended with their ids shifted past the template's, and the matching
    references in the spliced body are shifted by the same offset.
    """
    if generated is None:
        return
    existing = parts.get(part_path) if part_path in template_names else None
    if existing is None:
        parts[part_path] = generated
        _ensure_part_wired(parts, template_names, rels_root, used_ids,
                           overrides, part_path, kind)
        return
    merged, offset = _merge_notes(existing, generated)
    parts[part_path] = merged
    if offset:
        _shift_fragment_ids(fragment, ref_tag, _w("id"), offset)


def _merge_notes(template_part: bytes, generated: bytes) -> tuple[bytes, int]:
    """Append generated content notes to *template_part*, returning (bytes, offset)."""
    id_attr = _w("id")
    type_attr = _w("type")
    tpl_root = etree.fromstring(template_part)
    gen_root = etree.fromstring(generated)
    tpl_ids = [
        nid
        for n in tpl_root
        if isinstance(n.tag, str) and n.get(type_attr) is None
        and (nid := _int_attr(n, id_attr)) is not None and nid >= 1
    ]
    offset = max(tpl_ids) if tpl_ids else 0
    for note in gen_root:
        if not isinstance(note.tag, str) or note.get(type_attr) is not None:
            continue
        old = _int_attr(note, id_attr)
        if old is None:
            continue
        note.set(id_attr, str(old + offset))
        tpl_root.append(note)
    return (
        etree.tostring(tpl_root, xml_declaration=True, encoding="UTF-8", standalone=True),
        offset,
    )


def _inject_comments(
    parts: dict[str, bytes],
    template_names: set[str],
    rels_root: etree._Element,
    used_ids: set[str],
    overrides: dict[str, str],
    generated: bytes | None,
    fragment: list[etree._Element],
) -> None:
    """Merge our comments into the template's, offsetting our comment ids."""
    if generated is None:
        return
    part_path = "word/comments.xml"
    existing = parts.get(part_path) if part_path in template_names else None
    if existing is None:
        parts[part_path] = generated
        _ensure_part_wired(parts, template_names, rels_root, used_ids,
                           overrides, part_path, "comments")
        return
    id_attr = _w("id")
    tpl_root = etree.fromstring(existing)
    gen_root = etree.fromstring(generated)
    tpl_ids = [
        cid for c in tpl_root.findall(_w("comment"))
        if (cid := _int_attr(c, id_attr)) is not None
    ]
    offset = (max(tpl_ids) + 1) if tpl_ids else 0
    for c in gen_root.findall(_w("comment")):
        old = _int_attr(c, id_attr)
        if old is None:
            continue
        c.set(id_attr, str(old + offset))
        tpl_root.append(c)
    parts[part_path] = etree.tostring(
        tpl_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    if offset:
        for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
            _shift_fragment_ids(fragment, _w(tag), id_attr, offset)


def _shift_fragment_ids(
    fragment: list[etree._Element], tag: str, attr: str, offset: int
) -> None:
    for top in fragment:
        for ref in top.iter(tag):
            val = ref.get(attr)
            if val is not None and val.lstrip("-").isdigit():
                ref.set(attr, str(int(val) + offset))


# --------------------------------------------------------------------------- #
# content types + packaging
# --------------------------------------------------------------------------- #
def _ensure_content_types(
    ct_bytes: bytes, media_exts: set[str], overrides: dict[str, str]
) -> bytes:
    """Add any missing Default (image extension) / Override (part) declarations.

    Also normalise the main-document part type: a ``.dotx`` template declares
    ``/word/document.xml`` as the *template* main part (``…template.main+xml``);
    our output is a ``.docx``, so it must be the *document* main part, otherwise
    Word reports the file as corrupt.
    """
    root = etree.fromstring(ct_bytes)
    for o in root.findall(f"{{{_CT}}}Override"):
        if o.get("PartName") == "/word/document.xml" and o.get("ContentType") == _TEMPLATE_MAIN_CT:
            o.set("ContentType", _DOCUMENT_MAIN_CT)
    have_default = {
        (d.get("Extension") or "").lower()
        for d in root.findall(f"{{{_CT}}}Default")
    }
    have_override = {
        o.get("PartName") for o in root.findall(f"{{{_CT}}}Override")
    }
    ext_ct = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "emf": "image/x-emf", "tif": "image/tiff", "tiff": "image/tiff",
        "svg": "image/svg+xml",
    }
    for ext in sorted(media_exts):
        if ext not in have_default and ext in ext_ct:
            el = etree.SubElement(root, f"{{{_CT}}}Default")
            el.set("Extension", ext)
            el.set("ContentType", ext_ct[ext])
    for part_name, content_type in overrides.items():
        if part_name not in have_override:
            el = etree.SubElement(root, f"{{{_CT}}}Override")
            el.set("PartName", part_name)
            el.set("ContentType", content_type)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _zip_parts(parts: dict[str, bytes]) -> bytes:
    """Deterministic OPC zip (fixed timestamps + sorted members), as package.py."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, parts[name])
    return buf.getvalue()
