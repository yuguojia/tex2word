from __future__ import annotations

import json
from pathlib import Path

from tex2word import convert_source, ir

ROOT = Path(__file__).resolve().parents[1]


def _paragraph_texts(doc: ir.Document) -> list[str]:
    return [
        "".join(t.value for t in block.inlines if isinstance(t, ir.Text))
        for block in doc.blocks
        if isinstance(block, ir.Paragraph)
    ]


def test_external_python_plugin_reorders_suppitems():
    src = r"""
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
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    assert _paragraph_texts(result.document) == ["abc", "def", "xyz"]
    assert result.report.errors == []


def test_plugin_preprocessor_runs_after_newcommand_expansion():
    src = r"""
\newcommand{\MarkSuppA}{\supp{a}}

\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\begin{document}
\MarkSuppA
\printsupp{Figure}
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    assert _paragraph_texts(result.document) == ["abc"]
    assert result.report.errors == []


def test_plugin_commands_survive_latex_compatibility_stubs():
    src = r"""
\providecommand{\supp}[1]{}
\providecommand{\printsupp}[2][]{}

\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\begin{document}
\supp{a}
\printsupp{Figure}
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    assert _paragraph_texts(result.document) == ["abc"]
    assert result.report.errors == []


def test_suppitem_separator_can_be_configured_as_tex():
    src = r"""
\begin{suppitem}{Figure}{c}
def
\end{suppitem}

\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\begin{document}
\supp{a}
\supp{c}
\suppitemsep{\newpage}
\printsupp{Figure}
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    blocks = result.document.blocks
    assert isinstance(blocks[0], ir.Paragraph)
    assert "".join(t.value for t in blocks[0].inlines if isinstance(t, ir.Text)) == "abc"
    assert isinstance(blocks[1], ir.PageBreak)
    assert isinstance(blocks[2], ir.Paragraph)
    assert "".join(t.value for t in blocks[2].inlines if isinstance(t, ir.Text)) == "def"


def test_printsupp_separator_override_wins_for_one_print():
    src = r"""
\begin{suppitem}{Figure}{a}
abc
\end{suppitem}

\begin{suppitem}{Figure}{c}
def
\end{suppitem}

\begin{document}
\supp{a}
\supp{c}
\printsupp[\newpage]{Figure}
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    assert [type(block) for block in result.document.blocks] == [
        ir.Paragraph,
        ir.PageBreak,
        ir.Paragraph,
    ]


def test_exports_and_imports_supp_order_between_files(tmp_path):
    plugin = str(ROOT / "examples" / "supp_plugin.py")
    main = r"""
\exportsupp{aaa.tmp}
\begin{document}
\supp{a}
\supp{b}
\supp{c}
\end{document}
"""

    convert_source(main, base_dir=str(tmp_path), plugins=[plugin])

    order_file = tmp_path / "aaa.tmp"
    assert json.loads(order_file.read_text(encoding="utf-8")) == ["a", "b", "c"]

    supp = r"""
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
\begin{document}\printsupp{Figure}
\printsupp{Table}
\end{document}
"""

    result = convert_source(supp, base_dir=str(tmp_path), plugins=[plugin])

    assert _paragraph_texts(result.document) == ["abc", "def", "xyz"]


def test_sref_expands_to_include_text_word_field():
    src = r"""
\sreffile{../Image.png}
\begin{document}
See \sref{bookmarkname}.
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])
    para = next(block for block in result.document.blocks if isinstance(block, ir.Paragraph))
    field = next(inline for inline in para.inlines if isinstance(inline, ir.WordField))

    assert field.code == r'INCLUDETEXT "{FILENAME \p}/../Image.png" bookmarkname \! \* CHARFORMAT'


def test_sref_sanitizes_label_like_bookmark_name():
    src = r"""
\sreffile{supplement.docx}
\begin{document}
See \sref{fig:demo}.
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])
    para = next(block for block in result.document.blocks if isinstance(block, ir.Paragraph))
    field = next(inline for inline in para.inlines if isinstance(inline, ir.WordField))

    assert field.code == r'INCLUDETEXT "{FILENAME \p}/supplement.docx" fig_demo \! \* CHARFORMAT'


def test_sref_absolute_docx_path_stays_absolute():
    src = r"""
\srefdoc{C:/Users/UserName/My Documents/file.docx}
\begin{document}
\sref{bookmarkname}
\end{document}
"""

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])
    para = next(block for block in result.document.blocks if isinstance(block, ir.Paragraph))
    field = next(inline for inline in para.inlines if isinstance(inline, ir.WordField))

    assert (
        field.code
        == r'INCLUDETEXT "C:/Users/UserName/My Documents/file.docx" bookmarkname \! \* CHARFORMAT'
    )


def test_sref_warns_without_configured_docx_file():
    src = r"\begin{document}\sref{bookmarkname}\end{document}"

    result = convert_source(src, plugins=[str(ROOT / "examples" / "supp_plugin.py")])

    assert any(entry.construct == "sref" for entry in result.report.warnings)


def test_plugin_can_register_macro_signature(tmp_path):
    plugin = tmp_path / "eat_plugin.py"
    plugin.write_text(
        "def register(registry):\n"
        "    registry.add_macro('eat', '{')\n",
        encoding="utf-8",
    )
    src = r"\begin{document}\eat{hidden}shown\end{document}"

    result = convert_source(src, plugins=str(plugin))

    assert _paragraph_texts(result.document) == ["shown"]
