from __future__ import annotations

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
