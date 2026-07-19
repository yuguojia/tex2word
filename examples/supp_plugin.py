r"""Example tex2word plugin for supplementary item ordering.

Usage:
    tex2word convert paper.tex -o paper.docx --plugin examples/supp_plugin.py

The plugin supports:
    \begin{suppitem}{Figure}{a}
    ...
    \end{suppitem}

    \supp{a}
    \exportsupp{aaa.tmp}
    \importsupp{aaa.tmp}
    \suppitemsep{\newpage}
    \printsupp{Figure}
    \sreffile{file.docx}
    \sref{bookmarkname}

By default, printed items are separated by a blank line. Use
    \suppitemsep{\newpage}
to separate every printed item with a page break, or
    \printsupp[\newpage]{Figure}
to override the separator for one print command.

Use \exportsupp{aaa.tmp} in a source document to write the \supp order, then
\importsupp{aaa.tmp} in another document to reuse that order when printing
suppitem content. The file stores a JSON list of keys and is resolved relative
to the current TeX base directory.

Use \sreffile{file.docx} to set the external Word document used by \sref.
\sref{bookmarkname} expands to a Word INCLUDETEXT field that pulls the named
bookmark from that document. Relative paths are written as
"{FILENAME \p}/relative/path.docx" so they resolve next to the current Word file.
"""

from __future__ import annotations

import json
import os

from tex2word import PluginRegistry
from tex2word.report import ConversionReport


def register(registry: PluginRegistry) -> None:
    registry.add_environment("suppitem", "{{")
    registry.add_macro("supp", "{")
    registry.add_macro("exportsupp", "{")
    registry.add_macro("importsupp", "{")
    registry.add_macro("suppitemsep", "{")
    registry.add_macro("suppitemseparator", "{")
    registry.add_macro("printsupp", "[{")
    registry.add_macro("sref", "{")
    registry.add_macro("sreffile", "{")
    registry.add_macro("srefdoc", "{")
    registry.add_macro("srefsource", "{")
    registry.add_preprocessor(preprocess_source)


def preprocess_source(source: str, base_dir: str, report: ConversionReport) -> str:
    source, items = _collect_suppitems(source)
    source, separator = _collect_separator(source)
    order: list[str] = []
    export_paths: list[str] = []
    source = _replace_order_macros(source, order, export_paths, base_dir, report)
    _write_exports(export_paths, order, base_dir, report)
    source = _replace_printsupp_macro(
        source,
        lambda kind, sep: _render(
            kind,
            order,
            items,
            report,
            separator if sep is None else _normalize_separator(sep),
        ),
    )
    return _replace_sref_macros(source, report)


def _record(order: list[str], key: str) -> str:
    if key:
        order.append(key)
    return ""


def _record_export(paths: list[str], path: str) -> str:
    if path:
        paths.append(path)
    return ""


def _import_order(
    order: list[str],
    path: str,
    base_dir: str,
    report: ConversionReport,
) -> str:
    if not path:
        return ""
    resolved = _resolve_path(path, base_dir)
    try:
        with open(resolved, encoding="utf-8") as fh:
            keys = _decode_order(fh.read())
    except OSError as exc:
        report.warn("importsupp", f"could not read {path!r}: {exc}")
        return ""
    order.extend(key for key in keys if key)
    return ""


def _write_exports(
    paths: list[str],
    order: list[str],
    base_dir: str,
    report: ConversionReport,
) -> None:
    for path in paths:
        resolved = _resolve_path(path, base_dir)
        try:
            parent = os.path.dirname(resolved)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as fh:
                json.dump(order, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            report.info("exportsupp", f"wrote {path!r}")
        except OSError as exc:
            report.warn("exportsupp", f"could not write {path!r}: {exc}")


def _decode_order(content: str) -> list[str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [line.strip() for line in content.splitlines() if line.strip()]
    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]
    if isinstance(data, dict) and isinstance(data.get("order"), list):
        return [str(item).strip() for item in data["order"] if str(item).strip()]
    return []


def _resolve_path(path: str, base_dir: str) -> str:
    path = path.strip()
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def _render(
    kind: str,
    order: list[str],
    items: dict[str, tuple[str, str]],
    report: ConversionReport,
    separator: str,
) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for key in order:
        if key in seen:
            continue
        seen.add(key)
        item = items.get(key)
        if item is None:
            report.warn("supp", f"unknown supplementary item key: {key}")
            continue
        item_kind, body = item
        if item_kind == kind:
            parts.append(body.strip("\n"))
    return separator.join(part for part in parts if part.strip())


def _collect_separator(source: str) -> tuple[str, str]:
    separator = "\n\n"

    def set_separator(sep: str) -> str:
        nonlocal separator
        separator = _normalize_separator(sep)
        return ""

    for name in ("suppitemsep", "suppitemseparator"):
        source = _replace_one_arg_macro(source, name, set_separator)
    return source, separator


def _normalize_separator(separator: str) -> str:
    if not separator:
        return ""
    return "\n" + separator + "\n"


def _collect_suppitems(source: str) -> tuple[str, dict[str, tuple[str, str]]]:
    begin = r"\begin{suppitem}"
    end = r"\end{suppitem}"
    items: dict[str, tuple[str, str]] = {}
    out: list[str] = []
    pos = 0
    while True:
        start = source.find(begin, pos)
        if start == -1:
            out.append(source[pos:])
            break
        out.append(source[pos:start])
        arg_pos = start + len(begin)
        kind, arg_pos = _read_group(source, arg_pos)
        key, body_start = _read_group(source, arg_pos)
        finish = source.find(end, body_start)
        if finish == -1:
            out.append(source[start:])
            break
        items[key.strip()] = (kind.strip(), source[body_start:finish])
        pos = finish + len(end)
    return "".join(out), items


def _replace_order_macros(
    source: str,
    order: list[str],
    export_paths: list[str],
    base_dir: str,
    report: ConversionReport,
) -> str:
    handlers = {
        "supp": lambda value: _record(order, value),
        "importsupp": lambda value: _import_order(order, value, base_dir, report),
        "exportsupp": lambda value: _record_export(export_paths, value),
    }
    out: list[str] = []
    pos = 0
    while True:
        found = _find_next_macro(source, pos, tuple(handlers))
        if found is None:
            out.append(source[pos:])
            break
        start, name = found
        marker_end = start + len(name) + 1
        arg, end = _read_group(source, marker_end)
        if end == marker_end:
            out.append(source[pos:marker_end])
            pos = marker_end
            continue
        out.append(source[pos:start])
        out.append(handlers[name](arg.strip()))
        pos = end
    return "".join(out)


def _find_next_macro(source: str, pos: int, names: tuple[str, ...]) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for name in names:
        marker = "\\" + name
        search = pos
        while True:
            start = source.find(marker, search)
            if start == -1:
                break
            after = start + len(marker)
            if after < len(source) and source[after].isalpha():
                search = after
                continue
            if best is None or start < best[0]:
                best = (start, name)
            break
    return best


def _replace_sref_macros(source: str, report: ConversionReport) -> str:
    docx_file: str | None = None
    setter_names = ("sreffile", "srefdoc", "srefsource")
    handlers = (*setter_names, "sref")
    out: list[str] = []
    pos = 0
    while True:
        found = _find_next_macro(source, pos, handlers)
        if found is None:
            out.append(source[pos:])
            break
        start, name = found
        marker_end = start + len(name) + 1
        arg, end = _read_group(source, marker_end)
        if end == marker_end:
            out.append(source[pos:marker_end])
            pos = marker_end
            continue
        out.append(source[pos:start])
        value = arg.strip()
        if name in setter_names:
            docx_file = value
        elif docx_file:
            out.append(_sref_field(docx_file, value))
        else:
            report.warn("sref", f"\\sref{{{value}}} ignored: no \\sreffile{{...}} set")
        pos = end
    return "".join(out)


def _sref_field(docx_file: str, bookmark: str) -> str:
    return rf'\texwordfield{{INCLUDETEXT "{_field_quote(_field_path(docx_file))}" {bookmark} \!}}'


def _field_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    if _is_relative_path(path):
        return r"{FILENAME \p}/" + path
    return path


def _is_relative_path(path: str) -> bool:
    if not path:
        return True
    if path.startswith(("/", "\\")):
        return False
    if "://" in path:
        return False
    drive, _ = os.path.splitdrive(path)
    return not drive


def _field_quote(value: str) -> str:
    return value.replace('"', r'\"')


def _replace_one_arg_macro(source: str, name: str, repl) -> str:
    marker = "\\" + name
    out: list[str] = []
    pos = 0
    while True:
        start = source.find(marker, pos)
        if start == -1:
            out.append(source[pos:])
            break
        after = start + len(marker)
        if after < len(source) and source[after].isalpha():
            out.append(source[pos:after])
            pos = after
            continue
        arg, end = _read_group(source, after)
        if end == after:
            out.append(source[pos:after])
            pos = after
            continue
        out.append(source[pos:start])
        out.append(repl(arg.strip()))
        pos = end
    return "".join(out)


def _replace_printsupp_macro(source: str, repl) -> str:
    marker = r"\printsupp"
    out: list[str] = []
    pos = 0
    while True:
        start = source.find(marker, pos)
        if start == -1:
            out.append(source[pos:])
            break
        after = start + len(marker)
        if after < len(source) and source[after].isalpha():
            out.append(source[pos:after])
            pos = after
            continue
        separator, arg_pos = _read_optional(source, after)
        kind, end = _read_group(source, arg_pos)
        if end == arg_pos:
            out.append(source[pos:after])
            pos = after
            continue
        out.append(source[pos:start])
        out.append(_block_fragment(repl(kind.strip(), separator)))
        pos = end
    return "".join(out)


def _block_fragment(text: str) -> str:
    text = text.strip("\n")
    if not text.strip():
        return ""
    return "\n\n" + text + "\n\n"


def _read_optional(source: str, pos: int) -> tuple[str | None, int]:
    while pos < len(source) and source[pos] in " \t\r\n":
        pos += 1
    if pos >= len(source) or source[pos] != "[":
        return None, pos
    depth = 0
    i = pos
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return source[pos + 1 : i], i + 1
        i += 1
    return source[pos + 1 :], len(source)


def _read_group(source: str, pos: int) -> tuple[str, int]:
    while pos < len(source) and source[pos] in " \t\r\n":
        pos += 1
    if pos >= len(source) or source[pos] != "{":
        return "", pos
    depth = 0
    i = pos
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[pos + 1 : i], i + 1
        i += 1
    return source[pos + 1 :], len(source)
