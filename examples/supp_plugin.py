r"""Example tex2word plugin for supplementary item ordering.

Usage:
    tex2word convert paper.tex -o paper.docx --plugin examples/supp_plugin.py

The plugin supports:
    \begin{suppitem}{Figure}{a}
    ...
    \end{suppitem}

    \supp{a}
    \suppitemsep{\newpage}
    \printsupp{Figure}

By default, printed items are separated by a blank line. Use
    \suppitemsep{\newpage}
to separate every printed item with a page break, or
    \printsupp[\newpage]{Figure}
to override the separator for one print command.
"""

from __future__ import annotations

from tex2word import PluginRegistry
from tex2word.report import ConversionReport


def register(registry: PluginRegistry) -> None:
    registry.add_environment("suppitem", "{{")
    registry.add_macro("supp", "{")
    registry.add_macro("suppitemsep", "{")
    registry.add_macro("suppitemseparator", "{")
    registry.add_macro("printsupp", "[{")
    registry.add_preprocessor(preprocess_source)


def preprocess_source(source: str, base_dir: str, report: ConversionReport) -> str:
    source, items = _collect_suppitems(source)
    source, separator = _collect_separator(source)
    order: list[str] = []
    source = _replace_one_arg_macro(source, "supp", lambda key: _record(order, key))
    return _replace_printsupp_macro(
        source,
        lambda kind, sep: _render(
            kind,
            order,
            items,
            report,
            separator if sep is None else _normalize_separator(sep),
        ),
    )


def _record(order: list[str], key: str) -> str:
    if key:
        order.append(key)
    return ""


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
        out.append(repl(kind.strip(), separator))
        pos = end
    return "".join(out)


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
