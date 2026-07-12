"""A small BibTeX parser producing CSL-JSON items.

Not a full bibtex implementation: it handles the common entry/field grammar
(``@type{key, field = {value} | "value" | bare, ...}``), brace-aware values,
``@string`` macros, and a heuristic bibtex->CSL field/type mapping. As the PRD
notes, bibtex<->CSL conversion is inherently heuristic and lossy on casing.
"""

from __future__ import annotations

import re

from pylatexenc.latex2text import LatexNodes2Text

from .. import ir

_L2T = LatexNodes2Text(keep_comments=False, strict_latex_spaces=False)

# bibtex entry type -> CSL type
_TYPE_MAP = {
    "article": "article-journal",
    "book": "book",
    "inbook": "chapter",
    "incollection": "chapter",
    "inproceedings": "paper-conference",
    "conference": "paper-conference",
    "proceedings": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "thesis": "thesis",  # biblatex generic thesis entry
    "techreport": "report",
    "manual": "book",
    "misc": "document",
    "unpublished": "manuscript",
    "online": "webpage",
    "electronic": "webpage",
    "booklet": "pamphlet",
}

# bibtex field -> CSL field (simple string fields)
_FIELD_MAP = {
    "title": "title",
    "journal": "container-title",
    "journaltitle": "container-title",  # biblatex (e.g. Zotero export)
    "shortjournal": "container-title-short",  # biblatex
    "booktitle": "container-title",
    "shorttitle": "title-short",  # biblatex
    "publisher": "publisher",
    "school": "publisher",
    "institution": "publisher",
    "volume": "volume",
    "number": "issue",
    "pages": "page",
    "doi": "DOI",
    "url": "URL",
    "edition": "edition",
    "series": "collection-title",
    "note": "note",
    "abstract": "abstract",
    "address": "publisher-place",
    "location": "publisher-place",  # biblatex
    "isbn": "ISBN",
    "issn": "ISSN",
    "chapter": "chapter-number",
}


def _strip_braces(value: str) -> str:
    """Convert a bibtex field value to plain Unicode (accents, dashes, &, ...)."""
    try:
        text = _L2T.latex_to_text(value)
    except Exception:
        text = value.replace("{", "").replace("}", "").replace("\\&", "&")
    return re.sub(r"\s+", " ", text).strip()


def _split_names(raw: str) -> list[dict[str, str]]:
    """Parse a bibtex ``author``/``editor`` field into CSL name parts."""
    names: list[dict[str, str]] = []
    for chunk in re.split(r"\s+and\s+", raw.strip()):
        chunk = _strip_braces(chunk)
        if not chunk:
            continue
        if "," in chunk:
            family, _, given = chunk.partition(",")
            names.append({"family": family.strip(), "given": given.strip()})
        else:
            parts = chunk.split()
            if len(parts) == 1:
                names.append({"family": parts[0]})
            else:
                names.append({"family": parts[-1], "given": " ".join(parts[:-1])})
    return names


def _tokenize_entry_body(body: str) -> dict[str, str]:
    """Parse ``field = value, ...`` honouring {} and "" delimiters."""
    fields: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        # field name
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9_+-]*)\s*=\s*", body[i:])
        if not m:
            break
        name = m.group(1).lower()
        i += m.end()
        if i >= n:
            break
        ch = body[i]
        if ch == "{":
            depth = 0
            j = i
            while j < n:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            value = body[i + 1 : j]
            i = j + 1
        elif ch == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            value = body[i + 1 : j]
            i = j + 1
        else:
            j = i
            while j < n and body[j] not in ",\n":
                j += 1
            value = body[i:j]
            i = j
        fields[name] = value
        # consume trailing comma/whitespace
        while i < n and body[i] in ", \n\t\r":
            i += 1
    return fields


def _to_csl(entry_type: str, key: str, raw_fields: dict[str, str]) -> ir.CSLItem:
    is_preprint = _is_online_preprint(entry_type, raw_fields)
    csl_type = "article" if is_preprint else _TYPE_MAP.get(entry_type, "document")
    out: dict[str, object] = {}
    for name, value in raw_fields.items():
        if name == "author":
            out["author"] = _split_names(value)
        elif name == "editor":
            out["editor"] = _split_names(value)
        elif name == "year":
            out.setdefault("issued", {})
            out["issued"] = {"date-parts": [[_int_or(value)]]}
        elif name == "month":
            continue
        elif name == "date":
            parts = _date_parts(value)
            if parts:
                out["issued"] = {"date-parts": [parts]}
        elif name == "urldate":
            parts = _date_parts(value)
            if parts:
                out["accessed"] = {"date-parts": [parts]}
        elif name in ("langid", "language"):
            language = _language_code(value)
            if language:
                out["language"] = language
        elif name == "type" and csl_type == "thesis":
            genre = _strip_braces(value)
            if genre:
                out["genre"] = genre
        elif name in _FIELD_MAP:
            out[_FIELD_MAP[name]] = _strip_braces(value)
    if is_preprint:
        _apply_online_preprint(out, key, raw_fields)
    else:
        _apply_eprint(out, raw_fields)
    return ir.CSLItem(id=key, type=csl_type, csl_fields=out)


def _is_online_preprint(entry_type: str, raw_fields: dict[str, str]) -> bool:
    """Recognise Zotero/BibLaTeX preprints without retyping every online item."""
    pubstate = _strip_braces(raw_fields.get("pubstate", "")).strip().lower()
    return entry_type == "online" and pubstate == "prepublished"


def _date_parts(value: str) -> list[int]:
    """Turn a BibLaTeX ISO date (possibly year-only) into CSL date parts."""
    match = re.match(r"\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", _strip_braces(value))
    if not match:
        return []
    return [int(part) for part in match.groups() if part is not None]


def _language_code(value: str) -> str:
    language = _strip_braces(value).strip().lower()
    aliases = {
        "english": "en",
        "american": "en-US",
        "british": "en-GB",
        "chinese": "zh",
        "simplifiedchinese": "zh-CN",
        "traditionalchinese": "zh-TW",
    }
    return aliases.get(language, language)


def _apply_online_preprint(
    out: dict[str, object], key: str, raw_fields: dict[str, str]
) -> None:
    """Add the CSL/Zotero metadata used for a BibLaTeX online preprint."""
    out["citation-key"] = key

    genre = _strip_braces(raw_fields.get("type", "")).strip()
    if genre:
        out["genre"] = genre

    eprint = _strip_braces(raw_fields.get("eprint", "")).strip()
    prefix = _strip_braces(
        raw_fields.get("archiveprefix") or raw_fields.get("eprinttype") or ""
    ).strip()
    is_arxiv = bool(eprint) and prefix.lower() in ("", "arxiv")
    if is_arxiv:
        out["number"] = f"arXiv:{eprint}"
        out["publisher"] = "arXiv"
        out["source"] = "arXiv.org"
        if "URL" not in out and "DOI" not in out:
            out["URL"] = f"https://arxiv.org/abs/{eprint}"

    notes: list[str] = []
    annotation = _strip_braces(raw_fields.get("annotation", "")).strip()
    if annotation:
        notes.append(annotation)
    if eprint:
        label = f"arXiv:{eprint}" if is_arxiv else eprint
        eprint_class = _strip_braces(
            raw_fields.get("eprintclass") or raw_fields.get("primaryclass") or ""
        ).strip()
        if eprint_class:
            label += f" [{eprint_class}]"
        notes.append(label)
    if notes:
        existing = str(out.get("note", "")).strip()
        out["note"] = "\n".join(([existing] if existing else []) + notes)


def _apply_eprint(out: dict[str, object], raw_fields: dict[str, str]) -> None:
    """Make a biblatex ``eprint`` resolvable: arXiv eprints become an abs URL.

    CSL has no standard eprint field, so we expose it as a ``URL`` (the most
    useful rendering) when the entry carries no ``doi``/``url`` already, and
    keep the bare identifier in ``note`` so nothing is silently dropped.
    """
    eprint = _strip_braces(raw_fields.get("eprint", "")).strip()
    if not eprint:
        return
    prefix = (raw_fields.get("archiveprefix") or raw_fields.get("eprinttype") or "").strip().lower()
    is_arxiv = prefix in ("", "arxiv")
    if "URL" not in out and "DOI" not in out and is_arxiv:
        out["URL"] = f"https://arxiv.org/abs/{eprint}"
    label = f"arXiv:{eprint}" if is_arxiv else eprint
    if "note" not in out:
        out["note"] = label


def _int_or(value: str) -> int | str:
    m = re.search(r"\d{4}", value)
    return int(m.group(0)) if m else _strip_braces(value)


def parse_bibtex(source: str) -> dict[str, ir.CSLItem]:
    """Parse BibTeX ``source`` into ``{citekey: CSLItem}`` (insertion order)."""
    # expand simple @string macros
    macros: dict[str, str] = {}
    for sm in re.finditer(r"@string\s*\{\s*([A-Za-z0-9_]+)\s*=\s*[\"{](.+?)[\"}]\s*\}", source,
                          re.IGNORECASE | re.DOTALL):
        macros[sm.group(1).lower()] = sm.group(2)

    items: dict[str, ir.CSLItem] = {}
    for m in re.finditer(r"@(\w+)\s*\{", source):
        entry_type = m.group(1).lower()
        if entry_type in ("string", "comment", "preamble"):
            continue
        start = m.end()
        depth = 1
        j = start
        while j < len(source) and depth:
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
            j += 1
        inner = source[start : j - 1]
        key, _, body = inner.partition(",")
        key = key.strip()
        if not key:
            continue
        raw_fields = _tokenize_entry_body(body)
        # substitute @string macros for bare values
        for fname, fval in list(raw_fields.items()):
            if fval.strip().lower() in macros:
                raw_fields[fname] = macros[fval.strip().lower()]
        items[key] = _to_csl(entry_type, key, raw_fields)
    return items
