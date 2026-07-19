# Supplementary Item Plugin

`supp_plugin.py` is an example tex2word Python plugin for collecting
supplementary material in one place and printing it later in the order it is
referenced by the main text.

## Usage

Run tex2word with the plugin:

```bash
tex2word convert paper.tex -o paper.docx --plugin examples/supp_plugin.py
```

From Python:

```python
from tex2word import convert_source

result = convert_source(source, plugins=["examples/supp_plugin.py"])
```

The plugin depends on the pure Python frontend. User-defined macros are expanded
before the plugin rewrites its custom commands, so a macro such as
`\newcommand{\marka}{\supp{a}}` still records `a`. Plugin commands registered
with `registry.add_macro(...)` are protected from empty compatibility stubs such
as `\providecommand{\supp}[1]{}`.

## Commands

### `suppitem`

Define a keyed supplementary item:

```tex
\begin{suppitem}{Figure}{a}
abc
\end{suppitem}
```

The first argument is the item kind, such as `Figure` or `Table`. The second
argument is the key referenced by `\supp{...}`. The body may contain normal
tex2word-supported LaTeX.

`suppitem` definitions are removed from their original location. They are only
printed by `\printsupp{...}`.

### `\supp{key}`

Record that a key appears in the main text:

```tex
\supp{a}
\supp{b}
\supp{a}
\supp{c}
```

Printing deduplicates by first occurrence, so this order prints as `a`, `b`,
`c`. Duplicate keys remain in exported order files, but are skipped when
printing.

### `\printsupp{kind}`

Print all items of a given kind using the recorded `\supp` order:

```tex
\printsupp{Figure}
\printsupp{Table}
```

Example:

```tex
\begin{suppitem}{Table}{b}
xyz
\end{suppitem}

\begin{suppitem}{Figure}{c}
def
\end{suppitem}

\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\begin{document}
\supp{a}
\supp{b}
\supp{c}

\printsupp{Figure}
\printsupp{Table}
\end{document}
```

Output order:

```text
abc
def
xyz
```

## Item Separators

By default, printed items are separated by a blank line.

Set a global separator with `\suppitemsep{...}`:

```tex
\suppitemsep{\newpage}
\printsupp{Figure}
```

Override the separator for one print command:

```tex
\printsupp[\newpage]{Figure}
```

The separator is a raw TeX fragment. It can contain any command tex2word
understands, such as `\newpage`.

An empty separator is allowed:

```tex
\suppitemsep{}
```

Alias:

```tex
\suppitemseparator{...}
```

## Export And Import Order

Use `\exportsupp{path}` to write the `\supp` key order to a file:

```tex
\exportsupp{aaa.tmp}
\begin{document}
\supp{a}
\supp{b}
\supp{c}
\end{document}
```

The exported file is UTF-8 JSON:

```json
[
  "a",
  "b",
  "c"
]
```

Use `\importsupp{path}` in another source file to reuse that order:

```tex
\begin{suppitem}{Table}{b}
xyz
\end{suppitem}

\begin{suppitem}{Figure}{c}
def
\end{suppitem}

\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\importsupp{aaa.tmp}

\begin{document}
\printsupp{Figure}
\printsupp{Table}
\end{document}
```

This prints:

```text
abc
def
xyz
```

Relative export/import paths are resolved relative to the converted TeX file's
base directory.

For convenience, `\importsupp` also accepts a plain line-based file with one key
per line.

## Cross-Document Bookmark References

Use `\sreffile{file.docx}` to set a Word document, then `\sref{bookmark}` to
insert an `INCLUDETEXT` field that pulls a bookmark from that document:

```tex
\sreffile{supplement.docx}
\begin{document}
See \sref{fig_a}.
\end{document}
```

This expands to a native tex2word field:

```tex
\texwordfield{INCLUDETEXT "{FILENAME \p}/supplement.docx" fig_a \! \* CHARFORMAT}
```

The bookmark argument is sanitized the same way tex2word sanitizes `\label`
bookmarks, so `\sref{fig:a}` targets `fig_a` in the generated Word field.

Relative paths are written as:

```text
"{FILENAME \p}/relative/path.docx"
```

Word resolves this relative to the current Word file location.

Absolute paths remain absolute:

```tex
\srefdoc{C:/Users/UserName/My Documents/file.docx}
\sref{bookmarkname}
```

emits:

```text
INCLUDETEXT "C:/Users/UserName/My Documents/file.docx" bookmarkname \! \* CHARFORMAT
```

Aliases for setting the external document:

```tex
\sreffile{file.docx}
\srefdoc{file.docx}
\srefsource{file.docx}
```

If `\sref{...}` appears before any document has been configured, the plugin
emits a conversion-report warning and drops that reference.

## Notes

- Unknown `\supp{key}` entries warn during `\printsupp{...}` and are skipped.
- Repeated `\printsupp{...}` calls each render their own block content.
- `\printsupp` output is padded with blank lines so adjacent print commands do
  not merge into the same paragraph.
- The plugin is intentionally source-level: it does not modify tex2word's core
  parser rules beyond the plugin registry declarations.
