"""LaTeX -> IR conversion built on pylatexenc's walker.

Macro expansion (``\\newcommand``/``\\def``) is done *before* walking, so custom
commands become standard content. Unknown constructs degrade gracefully into
``RawInline``/``RawPassthrough`` nodes plus a coverage-report entry -- never an
abort.
"""

from __future__ import annotations

import os
import re

from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexSpecialsNode,
    LatexWalker,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import EnvironmentSpec, MacroSpec

from .. import ir
from ..plugins import PluginRefs, load_plugins
from ..report import ConversionReport
from . import siunitx
from .colors import ColorTable
from .macros import expand_macros, local_package_sources
from .preprocess import flatten_inputs, preprocess, replace_inline_tikz, strip_comments

# --------------------------------------------------------------------------- #
# Static maps
# --------------------------------------------------------------------------- #

_SECTION_LEVELS = {
    "part": 1, "chapter": 1, "section": 1, "subsection": 2,
    "subsubsection": 3, "paragraph": 4, "subparagraph": 4,
}
# book/report classes: \chapter is the top numbered level, sections nest under it
_SECTION_LEVELS_BOOK = {
    "part": 1, "chapter": 1, "section": 2, "subsection": 3,
    "subsubsection": 4, "paragraph": 5, "subparagraph": 5,
}
# the sectioning commands LaTeX auto-numbers (\part is shown unnumbered here)
_NUMBERED_SECTIONS = {"chapter", "section", "subsection", "subsubsection"}

# spacing/box macros that print nothing (their argument is consumed)
_DROP_MACROS = {"phantom", "hphantom", "vphantom", "rule"}
# box wrappers that are visually transparent: emit the (last) content group
_TRANSPARENT_BOX = {"mbox", "fbox", "framebox", "makebox", "raisebox"}
# IEEEtran author-block wrappers: render their content (name / affiliation text).
_AUTHOR_BLOCK = {"IEEEauthorblockN", "IEEEauthorblockA"}

_EMPHASIS = {
    "textbf": "bold", "textit": "italic", "emph": "italic",
    "textsl": "italic", "underline": "underline",
    "texttt": "typewriter", "textsc": "smallcaps",
    "textsuperscript": "superscript", "textsubscript": "subscript",
    # ulem strike/underline, soul highlight
    "sout": "strike", "st": "strike", "xout": "strike",
    "uline": "underline", "uuline": "underline",
    "hl": "highlight",
}

# Declaration-form font switches (no argument): ``{\bfseries ...}`` / ``{\bf ...}``
# apply to the rest of the enclosing group, like ``\color``. The short forms are
# the old plain-TeX equivalents that still appear in real documents; left
# unhandled they leaked literally (``\bf1.``) or silently dropped their effect.
_EMPHASIS_DECL = {
    "bfseries": "bold", "bf": "bold",
    "itshape": "italic", "it": "italic", "em": "italic",
    "slshape": "italic", "sl": "italic",
    "ttfamily": "typewriter", "tt": "typewriter",
    "scshape": "smallcaps", "sc": "smallcaps",
}

# Upright/roman/sans/medium font resets: render the content with no added
# emphasis (\textrm/\textnormal are *upright*, not italic). True cancellation of
# a surrounding emphasis isn't modelled; a transparent passthrough is the closest
# faithful behaviour and avoids the previous "\textrm -> italic" inversion.
_FONT_RESET = {"textnormal", "textrm", "textsf", "textmd", "textup", "text"}

# Font-size declarations (10pt base) -> w:sz half-points.
_FONT_SIZE_HP = {
    "tiny": 10, "scriptsize": 14, "footnotesize": 16, "small": 18,
    "normalsize": 20, "large": 24, "Large": 29, "LARGE": 34,
    "huge": 41, "Huge": 50,
}

_MATH_ENVS = {
    "equation", "align", "gather", "multline", "eqnarray", "displaymath",
    "math", "alignat", "flalign",
    # amsmath inner-alignment envs that real (often OCR'd / copy-pasted) papers
    # use *bare* at block level, outside $$/\[ -- treat them as display math
    # rather than parsing the body as text (which leaks \frac, \int, … as raw).
    "aligned", "gathered", "split", "multlined",
}
# Of the above, these never carry equation numbers.
_UNNUMBERED_MATH_ENVS = {"displaymath", "math", "aligned", "gathered", "split", "multlined"}

# Environments we cannot translate to Word primitives -> graphics placeholder.
_OPAQUE_ENVS = {
    "tikzpicture", "pspicture", "pgfpicture", "circuitikz", "tikzcd",
    "tikzcd*", "forest", "pgfplots", "axis",
}

_CITE_MODES = {
    "cite": "paren", "citep": "paren", "Citep": "paren", "parencite": "paren",
    "citealp": "paren", "footcite": "foot", "smartcite": "paren",
    "autocite": "paren", "Autocite": "paren", "Parencite": "paren", "Cite": "paren",
    "citet": "text", "Citet": "text", "textcite": "text", "citealt": "text",
    "Textcite": "text",
    "citeauthor": "author", "Citeauthor": "author", "citeyear": "year",
    "citeyearpar": "year", "citenum": "num", "citenumber": "num",
}

_REF_KINDS = {
    "ref": "generic", "eqref": "equation", "pageref": "page",
    "autoref": "generic", "cref": "generic", "Cref": "generic", "vref": "generic",
    "crefrange": "generic", "Crefrange": "generic", "labelcref": "generic",
    "nameref": "name", "Nameref": "name",
}
# cleveref-style commands carry a type prefix; \ref/\eqref/\pageref stay bare.
_REF_STYLE = {
    "autoref": "full", "cref": "abbrev", "Cref": "full", "vref": "full",
    "crefrange": "abbrev", "Crefrange": "full",
}

_TEXT_SYMBOLS = {
    "LaTeX": "LaTeX", "TeX": "TeX", "ldots": "…", "dots": "…",
    "textellipsis": "…", "textemdash": "—", "textendash": "–",
    "textasciitilde": "~", "textbackslash": "\\", "textasciicircum": "^",
    "textbar": "|", "textless": "<", "textgreater": ">", "S": "§",
    "P": "¶", "copyright": "©", "textregistered": "®", "texttrademark": "™",
    "dag": "†", "ddag": "‡", "pounds": "£", "euro": "€", "%": "%",
    "textdegree": "°", "textbullet": "•", "textmu": "µ", "textperthousand": "‰",
    "textquotedblleft": "“", "textquotedblright": "”",
    "textquoteleft": "‘", "textquoteright": "’",
    "textquotesingle": "'",  # straight typewriter apostrophe (U+0027)
    "guillemotleft": "«", "guillemotright": "»", "textsection": "§",
    "textparagraph": "¶",
    # vulgar fractions, currencies, and assorted text symbols
    "textonehalf": "½", "textonequarter": "¼", "textthreequarters": "¾",
    "textonesuperior": "¹", "texttwosuperior": "²", "textthreesuperior": "³",
    "texteuro": "€", "textcent": "¢", "textsterling": "£", "textyen": "¥",
    "textdollar": "$", "textnumero": "№", "textcelsius": "℃", "textohm": "Ω",
    "textmho": "℧", "textdiv": "÷", "texttimes": "×", "textpm": "±",
    "textminus": "−", "textperiodcentered": "·", "textdaggerdbl": "‡",
    "textdagger": "†", "checkmark": "✓", "slash": "/", "nobreakspace": " ",
    # gensymb package text/math symbols
    "degree": "°", "celsius": "℃", "ohm": "Ω", "micro": "µ", "perthousand": "‰",
    "&": "&", "_": "_", "#": "#", "$": "$", "{": "{", "}": "}",
    " ": " ", ",": " ", ";": " ", ":": " ", "!": "", "@": "",
    "quad": " ", "qquad": "  ", "hfill": " ", "newline": "\n",
    # \xspace re-inserts the space pylatexenc gobbles after a control word,
    # so adjacent words don't fuse ("Cache\\xspace Practical").
    "xspace": " ",
    # common math-ish operators that also appear in text / pseudocode
    "gets": "←", "to": "→", "leftarrow": "←", "rightarrow": "→",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "land": "∧", "lor": "∨", "lnot": "¬", "neg": "¬", "neq": "≠", "ne": "≠",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "times": "×", "cdot": "·",
    "infty": "∞", "star": "★", "ast": "∗", "approx": "≈", "equiv": "≡",
    "in": "∈", "forall": "∀", "exists": "∃", "emptyset": "∅",
}

# pifont \ding{N}: Zapf-Dingbats slot -> Unicode. Common check/cross marks plus
# the three circled-digit ranges (used as level markers ❶❷… in many tables).
_DING_MAP: dict[int, str] = {
    51: "✓", 52: "✔", 53: "✗", 54: "✘", 55: "✗", 56: "✘",
}
for _i in range(10):
    _DING_MAP[172 + _i] = chr(0x2780 + _i)  # ➀..➉ circled sans-serif digit
    _DING_MAP[182 + _i] = chr(0x2776 + _i)  # ❶..❿ negative circled digit
    _DING_MAP[192 + _i] = chr(0x278A + _i)  # ➊..➓ negative circled sans-serif


def _ding_char(code: str) -> str:
    """Map a pifont ``\\ding{N}`` code to a Unicode dingbat (``•`` if unknown)."""
    try:
        return _DING_MAP.get(int(code.strip()), "•")
    except ValueError:
        return "•"

# Structural wrappers with no Word-visible formatting -> pass content through
# silently (no "unknown environment" warning). subequations groups display math;
# samepage/sloppypar/spacing affect TeX layout only; center/... handled earlier.
_TRANSPARENT_ENVS = {
    "subequations", "samepage", "sloppypar", "spacing", "singlespace",
    "doublespace", "onehalfspace", "noindent", "raggedright", "raggedleft",
    "small", "footnotesize", "large", "Large", "flushleftright",
}

# theorem-like environment name -> display name. proof is handled specially.
_THEOREM_ENVS = {
    "theorem": "Theorem", "thm": "Theorem", "lemma": "Lemma",
    "corollary": "Corollary", "cor": "Corollary", "proposition": "Proposition",
    "prop": "Proposition", "definition": "Definition", "defn": "Definition",
    "remark": "Remark", "example": "Example", "conjecture": "Conjecture",
    "claim": "Claim", "fact": "Fact", "observation": "Observation",
    "notation": "Notation", "assumption": "Assumption", "axiom": "Axiom",
    "exercise": "Exercise", "problem": "Problem", "question": "Question",
}

_TOC_MACROS = {
    "tableofcontents": "contents",
    "listoffigures": "figures",
    "listoftables": "tables",
}

_IGNORE_MACROS = {
    "maketitle", "frontmatter", "mainmatter", "backmatter",
    "newpage", "clearpage", "pagebreak", "noindent", "centering",
    "small", "large", "Large", "LARGE", "normalsize", "footnotesize",
    "scriptsize", "tiny", "huge", "Huge", "vspace", "hspace", "vfill",
    "bigskip", "medskip", "smallskip", "indent", "par", "protect",
    "displaystyle", "unskip", "ignorespaces", "leavevmode",
    "/",  # italic correction -> no output
    "sloppy", "fussy", "raggedright", "raggedleft", "flushbottom",
    "samepage", "frenchspacing", "boldmath", "unboldmath",
    "selectfont", "rmfamily", "sffamily", "normalfont", "floatbarrier",
    "rm", "sf", "md", "up", "mdseries", "upshape",  # upright/reset declarations
    "FloatBarrier", "height", "width", "depth", "totalheight",
    "fontfamily", "fontsize", "nohyphens",
    # preamble / packaging macros -- silently dropped
    "documentclass", "usepackage", "RequirePackage", "pagestyle",
    "thispagestyle", "setlength", "setcounter", "hypersetup",
    "graphicspath", "definecolor", "pagenumbering",
    "renewcommand", "newcommand", "providecommand",
    "DeclareFloatingEnvironment",
    "setitemize", "setenumerate", "hyphenation", "settopmatter",
    "texwordstyle",  # tex2word style-binding directive: scanned separately, no output
    "texwordtemplate",  # tex2word reference-template directive: scanned separately
    "texwordparstyle",  # tex2word per-paragraph style: handled at block level, else dropped
    "defbibheading",  # biblatex bibliography heading definition: handled at block level
    # grouping / layout / counter declarations -> drop (args consumed by specs)
    "begingroup", "endgroup", "bgroup", "egroup",
    "AddToShipoutPicture", "ClearShipoutPicture",
    "newcounter", "addtocounter", "refstepcounter", "stepcounter",
    # ACM/IEEE front-matter + layout macros with no body-visible output. The
    # affiliation sub-fields take an argument (consumed by their MacroSpec below).
    "authornote", "authornotemark", "balance", "acmnote",
    "institution", "department", "city", "state", "country",
    "postcode", "streetaddress", "position",
}


# --------------------------------------------------------------------------- #
# Node helpers
# --------------------------------------------------------------------------- #


def _flatten_stacked_tables(blocks: list[ir.Block]) -> list[ir.Block]:
    """Flatten a single-column nested ``tabular`` (the ``{@{}c@{}}a\\\\b`` line-
    stacking idiom common inside cells) into one paragraph with line breaks,
    instead of emitting a nested table per cell.

    Only flattened when every row is a single cell whose content is plain
    paragraphs -- a cell holding a block (display math, nested list, another
    table) keeps its nested-table form so that content isn't dropped."""
    out: list[ir.Block] = []
    for b in blocks:
        if (
            isinstance(b, ir.Table)
            and b.rows
            and all(len(r.cells) == 1 for r in b.rows)
            and all(
                isinstance(cb, ir.Paragraph)
                for r in b.rows for cb in r.cells[0].blocks
            )
        ):
            inlines: list[ir.Inline] = []
            for i, row in enumerate(b.rows):
                if i:
                    inlines.append(ir.LineBreak())
                for cb in row.cells[0].blocks:
                    inlines.extend(cb.inlines)  # type: ignore[union-attr]
            out.append(ir.Paragraph(inlines))
        else:
            out.append(b)
    return out


def _brace_groups(node: LatexMacroNode) -> list[list]:
    """The node-lists of a macro's ``{}`` mandatory groups (skips ``[]`` options)."""
    argd = node.nodeargd
    if argd is None or not argd.argnlist:
        return []
    return [
        a.nodelist for a in argd.argnlist
        if isinstance(a, LatexGroupNode) and (not a.delimiters or a.delimiters[0] == "{")
    ]


def _optional_group(node: LatexMacroNode) -> list | None:
    """The nodelist of a macro's first ``[]`` optional argument, or None."""
    argd = node.nodeargd
    if argd is None or not argd.argnlist:
        return None
    for a in argd.argnlist:
        if isinstance(a, LatexGroupNode) and a.delimiters and a.delimiters[0] == "[":
            return a.nodelist
    return None


def _join_and(items: list[str]) -> str:
    """``a`` / ``a and b`` / ``a, b and c`` (siunitx list separators)."""
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _split_on_and(nodes: list) -> list[list]:
    """Split a node list at ``\\and`` macros (one ``\\author`` -> several authors)."""
    segments: list[list] = [[]]
    for n in nodes:
        if isinstance(n, LatexMacroNode) and n.macroname == "and":
            segments.append([])
        else:
            segments[-1].append(n)
    return [s for s in segments if s]


def _group_nodes(node: LatexMacroNode, which: int = -1) -> list:
    """Return the nodelist of a macro's mandatory group argument.

    ``which == -1`` picks the last group argument (the mandatory one for
    sectioning/emphasis); otherwise the n-th non-None argument.
    """
    argd = node.nodeargd
    if argd is None or not argd.argnlist:
        return []
    groups = [a for a in argd.argnlist if isinstance(a, LatexGroupNode)]
    if not groups:
        # a single non-group argument (e.g. \ref e)
        for a in argd.argnlist:
            if a is not None:
                return [a]
        return []
    if which == -1:
        return groups[-1].nodelist
    if which < len(groups):
        return groups[which].nodelist
    return []


def _verbatim_inner(env: LatexEnvironmentNode) -> str:
    """Inner LaTeX of an environment, without the \\begin/\\end wrappers."""
    # pylatexenc captures verbatim/lstlisting bodies as nodeargd.verbatim_text,
    # leaving nodelist empty -- check there first or the code block comes back blank.
    argd = env.nodeargd
    verb = getattr(argd, "verbatim_text", None) if argd is not None else None
    if verb is not None:
        return verb.strip("\n")
    if not env.nodelist:
        return ""
    first = env.nodelist[0]
    last = env.nodelist[-1]
    return env.parsing_state.s[first.pos : last.pos + last.len]


def _chars_of(nodes: list) -> str:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, LatexCharsNode):
            out.append(n.chars)
        elif isinstance(n, LatexGroupNode):
            out.append(_chars_of(n.nodelist))
    return "".join(out).strip()


def _latex_of(nodes: list) -> str:
    """The verbatim LaTeX source of a node list (for \\ensuremath bodies)."""
    return "".join(n.latex_verbatim() for n in nodes).strip()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


class _Builder:
    def __init__(self, report: ConversionReport, latex_context=None) -> None:
        self.report = report
        self.latex_context = latex_context
        self.meta = ir.DocumentMeta()
        self.bib_files: list[str] = []
        self.bib_style: str = "plain"
        self.bibstyle_set: bool = False  # an explicit \bibliographystyle was seen
        self.thebib_items: dict[str, ir.CSLItem] = {}
        # biblatex \defbibheading{name}[default title]{heading template}
        self.bib_headings: dict[str, tuple[str | None, str]] = {}
        self.colors = ColorTable()
        # glossaries/acronyms: label -> (short, long); track first use of \gls
        self.acronyms: dict[str, tuple[str, str]] = {}
        # plain glossary terms (\newglossaryentry): label -> name (display text)
        self.glossary: dict[str, str] = {}
        self._acr_seen: set[str] = set()
        self.nocite_keys: list[str] = []  # \nocite{key} / \nocite{*}
        self.book_mode = False  # book/report class: \chapter is the top level
        self.in_appendix = False  # seen \appendix -> later sections use letters
        # \texwordparstyle{name}: Word style for the next paragraph flushed (then cleared)
        self._pending_par_style: str | None = None
        # \texwordstyle{noindent}{name}: Word style every \noindent paragraph adopts
        self.noindent_style: str | None = None
        # \footnotemark placeholders awaiting their \footnotetext{...} content
        self._pending_footmarks: list[ir.Footnote] = []
        # theorem-like environment name -> display title (built-ins + \newtheorem)
        self.theorem_envs: dict[str, str] = dict(_THEOREM_ENVS)
        self.unnumbered_theorems: set[str] = set()  # \newtheorem*-defined names
        # env name -> counter display title (shared counters resolve here)
        self.theorem_counters: dict[str, str] = {}
        # user \newtcolorbox callout environments -> rendered as Quote blocks
        self.box_envs: set[str] = set()
        # custom float env name -> caption/SEQ kind from \DeclareFloatingEnvironment
        self.custom_floats: dict[str, str] = {}

    # -- inline ----------------------------------------------------------- #

    def inlines(self, nodes: list) -> list[ir.Inline]:
        return _clean_inlines(self._scoped_inlines(nodes))

    def _scoped_inlines(self, nodes: list) -> list[ir.Inline]:
        """Build inlines, honouring ``\\color`` as a scope switch.

        ``\\color{c}`` (and a leading ``\\color`` inside ``{...}``) tints every
        following sibling in the current group, so we wrap the remainder rather
        than treating it as a standalone macro.
        """
        out: list[ir.Inline] = []
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if isinstance(node, LatexMacroNode) and node.macroname == "texwordcharstyle":
                name = _chars_of(_group_nodes(node)).strip()
                next_node = nodes[i + 1] if i + 1 < len(nodes) else None
                if name and isinstance(next_node, LatexGroupNode):
                    out.append(ir.CharStyle(self.inlines(next_node.nodelist), name))
                    i += 2
                    continue
                rest = self._scoped_inlines(nodes[i + 1:])
                if name:
                    out.append(ir.CharStyle(rest, name))
                else:
                    out.extend(rest)
                return out
            if isinstance(node, LatexMacroNode) and node.macroname in ("color", "normalcolor"):
                rest = self._scoped_inlines(nodes[i + 1:])
                fg = self._color_of(node) if node.macroname == "color" else None
                if fg:
                    out.append(ir.Colored(rest, fg=fg))
                else:  # unresolved colour or \normalcolor reset -- emit untinted
                    out.extend(rest)
                return out
            if isinstance(node, LatexMacroNode) and node.macroname in _FONT_SIZE_HP:
                rest = self._scoped_inlines(nodes[i + 1:])
                hp = _FONT_SIZE_HP[node.macroname]
                if hp == 20:  # \normalsize -- no span needed
                    out.extend(rest)
                else:
                    out.append(ir.FontSize(rest, half_points=hp))
                return out
            if isinstance(node, LatexMacroNode) and node.macroname in _EMPHASIS_DECL:
                rest = self._scoped_inlines(nodes[i + 1:])  # {\bfseries ...} scope
                out.append(ir.Emphasis(rest, _EMPHASIS_DECL[node.macroname]))  # type: ignore[arg-type]
                return out
            self._inline_node(node, out)
            i += 1
        return out

    def _color_of(self, node: LatexMacroNode) -> str | None:
        """Resolve the colour of a ``\\color``/``\\textcolor``-style macro arg."""
        model: str | None = None
        color: str | None = None
        for a in node.nodeargd.argnlist if node.nodeargd else []:
            if a is None:
                continue
            if isinstance(a, LatexGroupNode) and a.delimiters and a.delimiters[0] == "[":
                model = _chars_of(a.nodelist)
            elif color is None:
                color = _chars_of(a.nodelist) if isinstance(a, LatexGroupNode) else _chars_of([a])
        if not color:
            return None
        return self.colors.resolve(color, model or None)

    def _inline_node(self, node, out: list[ir.Inline]) -> None:  # noqa: C901
        if isinstance(node, LatexCharsNode):
            text = _normalize_ws(node.chars)
            if text:
                out.extend(_typed_quote_runs(text))
            return
        if isinstance(node, LatexCommentNode):
            return
        if isinstance(node, LatexMathNode):
            latex = _strip_math_delims(node.latex_verbatim(), "math")
            out.append(ir.Math(latex))
            return
        if isinstance(node, LatexGroupNode):
            out.extend(self._scoped_inlines(node.nodelist))  # contain \color scope
            return
        if isinstance(node, LatexSpecialsNode):
            self._inline_special(node, out)
            return
        if isinstance(node, LatexMacroNode):
            self._inline_macro(node, out)
            return

    def _inline_special(self, node: LatexSpecialsNode, out: list[ir.Inline]) -> None:
        spec = node.specials_chars
        if spec == "~":
            out.append(ir.Text(" "))
        elif spec == "``":
            out.append(ir.Text("“"))  # LaTeX-command quote: curly, but English font
        elif spec == "''":
            out.append(ir.Text("”"))
        elif spec == "`":
            out.append(ir.Text("‘"))
        elif spec == "'":
            out.append(ir.Text("’"))
        elif spec in ("--", "---"):
            out.append(ir.Text("–" if spec == "--" else "—"))
        # otherwise ignore

    def _inline_macro(self, node: LatexMacroNode, out: list[ir.Inline]) -> None:  # noqa: C901
        name = node.macroname
        if name == "nocite":
            keys = [
                key.strip()
                for key in _chars_of(_group_nodes(node)).split(",")
                if key.strip()
            ]
            self.nocite_keys.extend(keys)
            if keys:
                out.append(ir.Cite(keys, hidden=True))
            return
        if name in _FONT_RESET:  # \textrm/\textnormal/... -> upright passthrough
            out.extend(self.inlines(_group_nodes(node)))
            return
        if name in _EMPHASIS:
            inner = self.inlines(_group_nodes(node))
            out.append(ir.Emphasis(inner, _EMPHASIS[name]))  # type: ignore[arg-type]
            return
        if name == "texwordcharstyle":
            return
        if name == "texwordfield":
            groups = _brace_groups(node)
            code = _latex_of(groups[0]) if groups else ""
            if code:
                optional = _optional_group(node)
                cached = _field_cached_text(self.inlines(optional)) if optional is not None else ""
                out.append(ir.WordField(code=code, cached=cached))
            return
        if name in ("textcolor", "colorbox", "fcolorbox"):
            self._inline_color(node, name, out)
            return
        if name in ("\\", "newline"):
            out.append(ir.LineBreak())
            return
        if name == "verb":
            text = getattr(node.nodeargd, "verbatim_text", None)
            if text is not None:
                out.append(ir.Emphasis([ir.Text(text)], "typewriter"))
                return
        if name in ("lstinline", "mintinline"):  # brace form -> typewriter text
            groups = _brace_groups(node)  # \mintinline{lang}{code} -> last group
            if groups:
                out.append(ir.Emphasis([ir.Text(_chars_of(groups[-1]))], "typewriter"))
            return
        if name in ("crefrange", "Crefrange"):
            # \crefrange{a}{b} -> "secs. 1 to 3": both endpoints, joined by "to"
            groups = _brace_groups(node)
            kind = _REF_KINDS[name]
            style = _REF_STYLE.get(name, "plain")
            if len(groups) >= 2:
                out.append(ir.Ref(_chars_of(groups[0]).strip(), kind, style=style))  # type: ignore[arg-type]
                out.append(ir.Text(" to "))
                out.append(ir.Ref(_chars_of(groups[1]).strip(), kind, style="plain"))  # type: ignore[arg-type]
            elif groups:
                out.append(ir.Ref(_chars_of(groups[-1]).strip(), kind, style=style))  # type: ignore[arg-type]
            return
        if name in _REF_KINDS:
            kind = _REF_KINDS[name]
            style = _REF_STYLE.get(name, "plain")
            keys = [k.strip() for k in _chars_of(_group_nodes(node)).split(",") if k.strip()]
            for idx, key in enumerate(keys):
                if idx > 0:  # cleveref joins multi-labels with ", " / " and "
                    out.append(ir.Text(" and " if idx == len(keys) - 1 else ", "))
                # only the first reference carries the cleveref type prefix
                out.append(ir.Ref(key, kind, style=(style if idx == 0 else "plain")))  # type: ignore[arg-type]
            if not keys:
                out.append(ir.Ref("", kind, style=style))  # type: ignore[arg-type]
            return
        if name in _CITE_MODES:
            key = _chars_of(_group_nodes(node))
            keys = [k.strip() for k in key.split(",") if k.strip()]
            prefix, suffix = self._cite_locators(node)
            out.append(
                ir.Cite(keys, _CITE_MODES[name], prefix=prefix, suffix=suffix)  # type: ignore[arg-type]
            )
            return
        if name == "label":
            label = _chars_of(_group_nodes(node))
            if self._labelable is not None and label:
                self._labelable.label = label
            return
        if name in ("href", "url"):
            self._inline_link(node, out)
            return
        if name == "hyperref":  # \hyperref[label]{text} -> internal link to label
            opt = _optional_group(node)
            groups = _brace_groups(node)
            text = self.inlines(groups[-1]) if groups else []
            if opt is not None:
                anchor = _chars_of(opt).strip()
                out.append(ir.Link(text or [ir.Text(anchor)], "", anchor=anchor))
            else:
                out.extend(text)  # \hyperref{url}{cat}{name}{text} -> just the text
            return
        if name in ("footnote", "thanks", "marginpar", "sidenote"):
            # \thanks (title/author note), \marginpar/\sidenote (margin asides)
            # all degrade to a Word footnote -- the closest aside Word offers.
            out.append(ir.Footnote(self.inlines(_group_nodes(node))))
            return
        if name == "footnotemark":
            # split footnote: a mark here, its text supplied later by \footnotetext.
            fn = ir.Footnote([])
            self._pending_footmarks.append(fn)
            out.append(fn)
            return
        if name == "footnotetext":
            # fill the oldest pending \footnotemark; if none, emit inline.
            content = self.inlines(_group_nodes(node))
            if self._pending_footmarks:
                self._pending_footmarks.pop(0).inlines = content
            else:
                out.append(ir.Footnote(content))
            return
        if name in ("enquote", "textquote", "foreignquote", "hyphenquote"):
            # csquotes inline quotes -> curly quotes around the quoted group
            # (\foreignquote/\hyphenquote carry a leading {lang}; quote the last).
            inner = self.inlines(_group_nodes(node))
            lq, rq = ("‘", "’") if _has_star(node) else ("“", "”")
            out.append(ir.Text(lq))
            out.extend(inner)
            out.append(ir.Text(rq))
            return
        if name == "endnote":
            out.append(ir.Endnote(self.inlines(_group_nodes(node))))
            return
        if name == "theendnotes":  # endnotes render natively at the doc end
            return
        if name == "index":  # -> a hidden Word XE index-entry field
            term = _chars_of(_group_nodes(node)).strip().replace('"', "'")
            if term:
                out.append(ir.IndexEntry(term))
            return
        if name in ("todo", "comment", "note"):  # review annotations -> Word comment
            text = _chars_of(_group_nodes(node)).strip()
            if text:
                out.append(ir.Comment(text=text))
            return
        if name in ("nicefrac", "sfrac"):  # text-mode fraction a/b
            groups = _brace_groups(node)
            if len(groups) >= 2:
                out.append(ir.Text(_chars_of(groups[0])))
                out.append(ir.Text("/"))
                out.append(ir.Text(_chars_of(groups[1])))
            return
        if name in ("si", "unit"):
            out.append(ir.Text(siunitx.units_to_text(_group_nodes(node))))
            return
        if name in ("SI", "qty"):
            groups = _brace_groups(node)
            num = siunitx.num_to_text(_chars_of(groups[0])) if groups else ""
            unit = siunitx.units_to_text(groups[1]) if len(groups) > 1 else ""
            out.append(ir.Text(f"{num}{siunitx.THIN}{unit}".strip()))
            return
        if name == "num":
            out.append(ir.Text(siunitx.num_to_text(_chars_of(_group_nodes(node)))))
            return
        if name == "ang":
            out.append(ir.Text(siunitx.ang_to_text(_chars_of(_group_nodes(node)))))
            return
        if name in ("numrange", "SIrange", "qtyrange"):  # a to b [unit]
            groups = _brace_groups(node)
            if len(groups) >= 2:
                a = siunitx.num_to_text(_chars_of(groups[0]))
                b = siunitx.num_to_text(_chars_of(groups[1]))
                unit = siunitx.units_to_text(groups[2]) if len(groups) > 2 else ""
                out.append(ir.Text(f"{a} to {b}" + (f"{siunitx.THIN}{unit}" if unit else "")))
            return
        if name in ("numlist", "SIlist", "qtylist"):  # a, b and c [unit]
            groups = _brace_groups(node)
            if groups:
                items = [siunitx.num_to_text(x.strip())
                         for x in _chars_of(groups[0]).split(";") if x.strip()]
                unit = siunitx.units_to_text(groups[1]) if len(groups) > 1 else ""
                out.append(ir.Text(_join_and(items) + (f"{siunitx.THIN}{unit}" if unit else "")))
            return
        if name == "includegraphics":  # an inline image (icon/logo in text)
            out.append(_make_image(node))
            return
        if name in _DROP_MACROS:  # phantom/rule: reserve space, print nothing
            return
        if name == "IEEEPARstart":  # \IEEEPARstart{T}{he} -> "The" (IEEE drop cap)
            groups = _brace_groups(node)
            for g in groups:
                out.extend(self.inlines(g))
            return
        if name == "ensuremath":  # \ensuremath{x} -> inline math
            out.append(ir.Math(_latex_of(_group_nodes(node))))
            return
        if name == "texorpdfstring":  # \texorpdfstring{TeX}{PDF} -> the TeX form
            groups = _brace_groups(node)
            if groups:
                out.extend(self.inlines(groups[0]))
            return
        if name in _TRANSPARENT_BOX or name in _AUTHOR_BLOCK:  # ...{X} -> X
            groups = _brace_groups(node)
            if groups:
                out.extend(self.inlines(groups[-1]))
            return
        if name in ("inst", "IEEEauthorrefmark"):  # affiliation marker -> superscript
            out.append(ir.Emphasis(self.inlines(_group_nodes(node)), "superscript"))
            return
        if name in ("newacronym", "newglossaryentry", "acro", "acrodef"):
            return  # definitions are collected separately; emit nothing
        if name.lower() in _AC_TO_GLS:  # acronym package: \ac/\acs/\acl/\acf/...
            label = _chars_of(_group_nodes(node)).strip()
            tname = _AC_TO_GLS[name.lower()]
            if name[:1].isupper():  # \Ac/\Acl/... -> capitalise the result
                tname = tname[:1].upper() + tname[1:]
            out.append(ir.Text(self._acronym_text(tname, label)))
            return
        if name.lower() in _GLS_MACROS:
            label = _chars_of(_group_nodes(node)).strip()
            out.append(ir.Text(self._acronym_text(name, label)))
            return
        if name == "ding":  # pifont dingbat: \ding{51}=✓, \ding{182}=❶, ...
            out.append(ir.Text(_ding_char(_chars_of(_group_nodes(node)))))
            return
        if name in ("shortstack", "stackanchor", "Shortstack"):
            # \shortstack[pos]{a \\ b}: stacked lines -> the content with \\ kept
            # as line breaks (the LatexMacroNode "\\" becomes ir.LineBreak).
            out.extend(self.inlines(_group_nodes(node)))
            return
        if name in _TEXT_SYMBOLS:
            out.append(ir.Text(_TEXT_SYMBOLS[name]))
            return
        if name in _IGNORE_MACROS:
            return
        if _accent_char(name) is not None:
            base = _chars_of(_group_nodes(node))
            out.append(ir.Text(_apply_text_accent(name, base)))
            return
        # graceful degradation
        self.report.warn(f"\\{name}", f"unsupported inline macro \\{name}")
        out.append(ir.RawInline(node.latex_verbatim(), f"unsupported macro \\{name}"))

    def _acronym_text(self, name: str, label: str) -> str:
        """Expand a glossaries/acronym reference (\\gls/\\acrshort/…) to text."""
        entry = self.acronyms.get(label)
        if entry is None:
            # a plain \newglossaryentry term: \gls/\glspl just print its name
            term = self.glossary.get(label)
            if term is not None:
                low = name.lower()
                text = term + ("s" if low.endswith("pl") else "")
                if name[:1].isupper() and text:
                    text = text[:1].upper() + text[1:]
                return text
            return label  # undefined acronym -> emit the key, never crash
        short, long = entry
        low = name.lower()
        plural = low.endswith("pl")
        key = low[:-2] if plural else low
        s = short + ("s" if plural else "")
        lg = long + ("s" if plural else "")
        full = f"{lg} ({s})"
        if key == "gls":
            text = s if label in self._acr_seen else full
            self._acr_seen.add(label)
        elif key in ("acrshort", "glsentryshort"):
            text = s
        elif key in ("acrlong", "glsentrylong"):
            text = lg
        elif key == "acrfull":
            text = full
        else:
            text = s
        if name[:1].isupper() and text:  # \Gls/\Acrlong/... capitalise
            text = text[:1].upper() + text[1:]
        return text

    def _glossary_list(self) -> ir.Block | None:
        """\\printglossaries/\\printacronyms -> a description list of the entries."""
        items: list[ir.ListItem] = []
        for short, long in self.acronyms.values():
            items.append(ir.ListItem([ir.Paragraph([ir.Text(long)])], term=[ir.Text(short)]))
        for key, name in self.glossary.items():
            items.append(ir.ListItem([ir.Paragraph([ir.Text(name)])], term=[ir.Text(key)]))
        if not items:
            return None
        return ir.ItemList(ordered=False, items=items, description=True)

    def _inline_color(self, node: LatexMacroNode, name: str, out: list[ir.Inline]) -> None:
        model: str | None = None
        groups: list[list] = []
        for a in node.nodeargd.argnlist if node.nodeargd else []:
            if a is None:
                continue
            if isinstance(a, LatexGroupNode) and a.delimiters and a.delimiters[0] == "[":
                model = _chars_of(a.nodelist)
            elif isinstance(a, LatexGroupNode):
                groups.append(a.nodelist)
        if not groups:
            return
        inner = self.inlines(groups[-1])  # last mandatory group is the content
        # the colour group: \textcolor{C}{t} / \colorbox{C}{t} -> groups[0];
        # \fcolorbox{frame}{C}{t} -> groups[-2] (the background colour).
        color_group = groups[-2] if len(groups) >= 2 else None
        hexval = self.colors.resolve(_chars_of(color_group), model or None) if color_group else None
        if hexval is None:
            out.extend(inner)
        elif name == "textcolor":
            out.append(ir.Colored(inner, fg=hexval))
        else:
            out.append(ir.Colored(inner, bg=hexval))

    def _inline_link(self, node: LatexMacroNode, out: list[ir.Inline]) -> None:
        argd = node.nodeargd
        groups = [a for a in (argd.argnlist if argd else []) if isinstance(a, LatexGroupNode)]
        if node.macroname == "url" and groups:
            url = _chars_of(groups[0].nodelist)
            out.append(ir.Link([ir.Text(url)], url))
        elif len(groups) >= 2:
            url = _chars_of(groups[0].nodelist)
            out.append(ir.Link(self.inlines(groups[1].nodelist), url))
        elif groups:
            url = _chars_of(groups[0].nodelist)
            out.append(ir.Link([ir.Text(url)], url))

    # -- blocks ----------------------------------------------------------- #

    def blocks(self, nodes: list) -> list[ir.Block]:
        out: list[ir.Block] = []
        inline_buf: list[ir.Inline] = []
        # block-level declaration scopes (\color{c} / {\bfseries} used *unbraced*
        # at block level): (index into inline_buf, kind, value). Applied at flush
        # so the declaration wraps the run of inlines that followed it, like the
        # braced form does via _scoped_inlines.
        scope_marks: list[tuple[int, str, object]] = []

        def flush() -> None:
            for idx, kind, val in reversed(scope_marks):
                seg = inline_buf[idx:]
                if not seg:
                    continue
                if kind == "color" and val:
                    inline_buf[idx:] = [ir.Colored(seg, fg=val)]  # type: ignore[arg-type]
                elif kind == "emph":
                    inline_buf[idx:] = [ir.Emphasis(seg, val)]  # type: ignore[arg-type]
                elif kind == "charstyle" and val:
                    inline_buf[idx:] = [ir.CharStyle(seg, val)]  # type: ignore[arg-type]
            scope_marks.clear()
            if any(not (isinstance(x, ir.Text) and not x.value.strip()) for x in inline_buf):
                cleaned = _clean_inlines(inline_buf.copy(), trim=True)
                if cleaned:
                    _flush_cleaned(cleaned, out, style=self._pending_par_style)
            inline_buf.clear()
            # \texwordparstyle only styles the paragraph it precedes, so its scope
            # ends at this flush (a heading/environment between it and a paragraph
            # flushes an empty buffer and clears the pending style too).
            self._pending_par_style = None

        skip_next = False
        for idx, node in enumerate(nodes):
            if skip_next:
                skip_next = False
                continue
            if isinstance(node, LatexCharsNode):
                self._chars_into_blocks(node.chars, inline_buf, out, flush)
                continue
            if isinstance(node, LatexCommentNode):
                continue
            if isinstance(node, LatexMathNode) and _is_display(node):
                # buffer as an inline display equation; flush() decides whether it
                # stays with surrounding text or degrades to a standalone block.
                inline_buf.append(
                    self._display_math(node.latex_verbatim(), "displaymath", False)
                )
                continue
            if isinstance(node, LatexEnvironmentNode):
                base = node.environmentname.rstrip("*")
                if base in _MATH_ENVS:
                    starred = node.environmentname.endswith("*")
                    numbered = (base not in _UNNUMBERED_MATH_ENVS) and not starred
                    inline_buf.append(
                        self._display_math(_verbatim_inner(node), base, numbered)
                    )
                    continue
                flush()
                self._environment(node, out)
                continue
            # a bare {...} group wrapping a block environment (e.g. the common
            # {\footnotesize \begin{verbatim}...\end{verbatim}}) -> descend as blocks
            if isinstance(node, LatexGroupNode) and any(
                isinstance(c, LatexEnvironmentNode) for c in node.nodelist
            ):
                flush()
                out.extend(self.blocks(node.nodelist))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "label":
                label = _chars_of(_group_nodes(node))
                # Don't overwrite a label the target already captured (e.g. a
                # figure's own \label, when a stray \label follows \end{figure}).
                if self._labelable is not None and label and self._labelable.label is None:
                    self._labelable.label = label
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in ("appendix", "beginappendix"):
                # \appendix (and class wrappers like fairmeta's \beginappendix)
                # switch later sections to lettered numbering.
                self.in_appendix = True
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in (
                "newpage", "clearpage", "pagebreak",
            ):
                flush()
                out.append(ir.PageBreak(command=node.macroname))  # type: ignore[arg-type]
                continue
            if isinstance(node, LatexMacroNode) and node.macroname.rstrip("*") in _SECTION_LEVELS:
                flush()
                self._heading(node, out)
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in _TOC_MACROS:
                flush()
                out.append(ir.TableOfContents(kind=_TOC_MACROS[node.macroname]))  # type: ignore[arg-type]
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in (
                "title", "author", "date", "keywords", "IEEEkeywords",
                "institute", "affiliation", "affil", "address", "email", "orcid",
                "markboth", "markright", "runninghead", "shorttitle",
            ):
                self._meta_macro(node)
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in ("resizebox", "scalebox"):
                # box-scaling wrappers are transparent: process their content as
                # blocks so nested tabulars/minipages survive (\resizebox{w}{h}{X}).
                flush()
                groups = _brace_groups(node)
                if groups:
                    out.extend(self.blocks(groups[-1]))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "includegraphics":
                # mid-paragraph (text already buffered) -> inline image (icon);
                # otherwise a standalone bare image -> a centered Figure
                if any(not (isinstance(x, ir.Text) and not x.value.strip())
                       for x in inline_buf):
                    self._inline_node(node, inline_buf)
                else:
                    flush()
                    out.append(self._figure_from_graphics(node))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "bibliographystyle":
                self.bib_style = _chars_of(_group_nodes(node)) or self.bib_style
                self.bibstyle_set = True
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "nocite":
                self._inline_macro(node, inline_buf)
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "bibliography":
                flush()
                for name in _chars_of(_group_nodes(node)).split(","):
                    if name.strip():
                        self.bib_files.append(name.strip())
                out.append(ir.Bibliography(entries=[], style="numeric"))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "addbibresource":
                for name in _chars_of(_group_nodes(node)).split(","):  # biblatex
                    if name.strip():
                        self.bib_files.append(name.strip())
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "defbibheading":
                self._defbibheading(node)
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "printbibliography":
                flush()
                title, heading_level, heading_numbered = self._printbibliography_heading(node)
                out.append(
                    ir.Bibliography(
                        entries=[],
                        style="numeric",
                        title=title,
                        heading_level=heading_level,
                        heading_numbered=heading_numbered,
                    )
                )
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "printindex":
                flush()
                out.append(ir.Index())
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in _PRINTGLOSSARY_MACROS:
                flush()
                glossary = self._glossary_list()
                if glossary is not None:
                    out.append(glossary)
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in (
                "blockquote", "blockcquote", "foreignblockquote",
            ):
                # csquotes block quote -> a Quote block (the last {} group is the
                # quoted body; \blockcquote/\foreignblockquote carry a leading arg)
                flush()
                groups = _brace_groups(node)
                body = self.blocks(groups[-1]) if groups else []
                out.append(ir.Quote(body or [ir.Paragraph([])]))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname == "epigraph":
                # \epigraph{quote}{source} -> a Quote with a right-aligned italic
                # attribution line underneath.
                flush()
                groups = _brace_groups(node)
                quote = self.inlines(groups[0]) if groups else []
                blocks: list[ir.Block] = [ir.Paragraph(quote)]
                if len(groups) > 1:
                    src = _clean_inlines(self.inlines(groups[1]), trim=True)
                    if src:
                        blocks.append(ir.Paragraph([ir.Emphasis(src, "italic")], align="right"))
                out.append(ir.Quote(blocks))
                continue
            # block-level declaration scopes: \color{c} / \normalcolor and the
            # font switches {\bfseries}/{\bf}/... used unbraced -- record where
            # they start so flush() wraps the inlines that follow.
            if isinstance(node, LatexMacroNode) and node.macroname in ("color", "normalcolor"):
                fg = self._color_of(node) if node.macroname == "color" else None
                scope_marks.append((len(inline_buf), "color", fg))
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in _EMPHASIS_DECL:
                scope_marks.append((len(inline_buf), "emph", _EMPHASIS_DECL[node.macroname]))
                continue
            # \texwordparstyle{name}: remember the Word style for the paragraph this
            # directive sits in; flush() stamps it on that paragraph and clears it.
            if isinstance(node, LatexMacroNode) and node.macroname == "texwordparstyle":
                name = _chars_of(_group_nodes(node)).strip()
                if name:
                    self._pending_par_style = name
                continue
            # \texwordcharstyle{name}{text}: apply a Word character style to the
            # following group. Declaration form applies until the paragraph flush.
            if isinstance(node, LatexMacroNode) and node.macroname == "texwordcharstyle":
                name = _chars_of(_group_nodes(node)).strip()
                next_node = nodes[idx + 1] if idx + 1 < len(nodes) else None
                if name and isinstance(next_node, LatexGroupNode):
                    inline_buf.append(ir.CharStyle(self.inlines(next_node.nodelist), name))
                    skip_next = True
                elif name:
                    scope_marks.append((len(inline_buf), "charstyle", name))
                continue
            # \noindent: dropped as before, unless \texwordstyle{noindent}{name} bound
            # it to a Word style -- then the paragraph it precedes adopts that style
            # (an explicit \texwordparstyle on the same paragraph still wins).
            if isinstance(node, LatexMacroNode) and node.macroname == "noindent":
                if self.noindent_style and self._pending_par_style is None:
                    self._pending_par_style = self.noindent_style
                continue
            # otherwise inline content
            self._inline_node(node, inline_buf)
        flush()
        return out

    def _chars_into_blocks(self, chars: str, buf: list[ir.Inline], out, flush) -> None:
        # split on blank lines (paragraph breaks)
        parts = chars.split("\n\n")
        for idx, part in enumerate(parts):
            if idx > 0:
                flush()
            text = _normalize_ws(part)
            if text:
                buf.extend(_typed_quote_runs(text))

    def _meta_macro(self, node: LatexMacroNode) -> None:
        if node.macroname == "author":
            # one \author may list several authors separated by \and
            for seg in _split_on_and(_group_nodes(node)):
                inner = _clean_inlines(self.inlines(seg), trim=True)
                if inner:
                    self.meta.authors.append(inner)
            return
        if node.macroname in ("institute", "affiliation", "affil", "address"):
            for seg in _split_on_and(_group_nodes(node)):
                inner = _clean_inlines(self.inlines(seg), trim=True)
                if inner:
                    self.meta.affiliations.append(inner)
            return
        if node.macroname in ("markboth", "markright", "runninghead", "shorttitle"):
            # \markboth{even}{odd} -> the odd (recto) head; others take their group
            groups = _brace_groups(node)
            head_nodes = groups[-1] if groups else _group_nodes(node)
            head = _normalize_ws(_chars_of(head_nodes)).strip()
            if head and not self.meta.running_head:
                self.meta.running_head = head
            return
        inner = _clean_inlines(self.inlines(_group_nodes(node)), trim=True)
        if node.macroname == "title":
            self.meta.title = inner
            opt = _optional_group(node)  # \title[running head]{full title}
            if opt is not None and not self.meta.running_head:
                head = _normalize_ws(_chars_of(opt)).strip()
                if head:
                    self.meta.running_head = head
        elif node.macroname == "date":
            self.meta.date = inner
        elif node.macroname in ("keywords", "IEEEkeywords"):
            self.meta.keywords = inner
        elif node.macroname == "email" and inner:
            self.meta.affiliations.append([ir.Text("Email: "), *inner])
        elif node.macroname == "orcid":
            oid = _chars_of(_group_nodes(node)).strip()
            if oid:
                url = oid if oid.startswith("http") else f"https://orcid.org/{oid}"
                self.meta.affiliations.append([ir.Link([ir.Text(f"ORCID: {oid}")], url)])

    def _defbibheading(self, node: LatexMacroNode) -> None:
        """Register a biblatex ``\\defbibheading`` definition.

        We only need the visible heading text. The optional argument is treated as
        the default value for ``#1``; ``\\printbibliography[title=...]`` overrides it.
        """
        groups = [
            a for a in (node.nodeargd.argnlist if node.nodeargd else [])
            if isinstance(a, LatexGroupNode) and a.delimiters == ("{", "}")
        ]
        if len(groups) < 2:
            return
        name = _chars_of(groups[0].nodelist).strip()
        if not name:
            return
        default_title: str | None = None
        for arg in node.nodeargd.argnlist if node.nodeargd else []:
            if isinstance(arg, LatexGroupNode) and arg.delimiters == ("[", "]"):
                default_title = _latex_of(arg.nodelist).strip()
                break
        self.bib_headings[name] = (default_title, _latex_of(groups[1].nodelist).strip())

    def _printbibliography_heading(
        self, node: LatexMacroNode
    ) -> tuple[list[ir.Inline] | None, int, bool]:
        """Return visible text, document level and numbering for a biblatex heading."""
        opts = _parse_printbibliography_options(node)
        heading = opts.get("heading", "bibliography").strip() or "bibliography"
        title = opts.get("title")
        if title is not None:
            title = _strip_outer_braces(title.strip())

        if heading in self.bib_headings:
            default_title, template = self.bib_headings[heading]
            arg = title if title is not None else default_title
            rendered = template.replace("#1", arg or "")
            return self._parse_bib_heading(rendered)
        if title is not None:
            inlines, level, numbered = self._parse_bib_heading(title)
            return inlines, level, numbered
        return None, 1, False

    def _parse_bib_heading(
        self, latex: str
    ) -> tuple[list[ir.Inline] | None, int, bool]:
        r"""Parse the sectioning command embedded in a ``\defbibheading`` body."""
        if not latex.strip():
            return None, 1, False
        try:
            nodes, _, _ = LatexWalker(
                latex, latex_context=self.latex_context, tolerant_parsing=True
            ).get_latex_nodes()
        except Exception:
            return [ir.Text(_normalize_ws(latex))], 1, False
        levels = _SECTION_LEVELS_BOOK if self.book_mode else _SECTION_LEVELS
        for n in nodes:
            if isinstance(n, LatexMacroNode) and n.macroname.rstrip("*") in _SECTION_LEVELS:
                name = n.macroname.rstrip("*")
                is_part = name == "part"
                numbered = not _has_star(n) and (name in _NUMBERED_SECTIONS or is_part)
                return (
                    _clean_inlines(self.inlines(_group_nodes(n)), trim=True),
                    levels.get(name, 1),
                    numbered,
                )
        return _clean_inlines(self.inlines(nodes), trim=True), 1, False

    def _heading(self, node: LatexMacroNode, out: list[ir.Block]) -> None:
        name = node.macroname.rstrip("*")
        levels = _SECTION_LEVELS_BOOK if self.book_mode else _SECTION_LEVELS
        level = levels.get(name, 1)
        inner = _clean_inlines(self.inlines(_group_nodes(node)), trim=True)
        # LaTeX numbers \chapter..\subsubsection by default; starred forms and the
        # run-in \paragraph/\subparagraph are not. \part is numbered too, but with
        # an independent upper-roman counter ("Part I"), so it gets its own flag.
        is_part = name == "part"
        numbered = not _has_star(node) and (name in _NUMBERED_SECTIONS or is_part)
        heading = ir.Heading(
            level,
            inner,
            numbered=numbered,
            appendix=self.in_appendix and not is_part,
            part=is_part,
        )
        out.append(heading)
        self._labelable = heading

    def _math_block(self, verbatim: str, env: str, numbered: bool) -> ir.MathBlock:
        latex = _strip_math_delims(verbatim, env)
        label = _extract_label(latex) if "\\label" in latex else None
        latex = _LABEL_TAG_RE.sub("", latex).strip()
        block = ir.MathBlock(latex=latex, numbered=numbered, env=env, label=label)
        self._labelable = block
        return block

    def _display_math(self, verbatim: str, env: str, numbered: bool) -> ir.DisplayMath:
        """A display equation buffered as an inline (see :func:`_flush_cleaned`)."""
        latex = _strip_math_delims(verbatim, env)
        label = _extract_label(latex) if "\\label" in latex else None
        latex = _LABEL_TAG_RE.sub("", latex).strip()
        node = ir.DisplayMath(latex=latex, numbered=numbered, env=env, label=label)
        self._labelable = node
        return node

    _labelable: (
        ir.Heading | ir.MathBlock | ir.DisplayMath | ir.Figure | ir.Table
        | ir.Theorem | ir.Algorithm | ir.ListItem | None
    ) = None

    def _environment(self, node: LatexEnvironmentNode, out: list[ir.Block]) -> None:  # noqa: C901
        name = node.environmentname
        base = name.rstrip("*")
        starred = name.endswith("*")
        if base in _MATH_ENVS:
            numbered = (base not in _UNNUMBERED_MATH_ENVS) and not starred
            out.append(self._math_block(_verbatim_inner(node), base, numbered))
            return
        if base in ("itemize", "enumerate", "description"):
            out.append(self._list(
                node, ordered=(base == "enumerate"), description=(base == "description")
            ))
            return
        if base in ("figure", "wrapfigure"):
            out.append(self._figure(node, spanning=starred))
            return
        if base in ("table", "wraptable"):
            self._table_float(node, out, spanning=starred)
            return
        if base in _TABULAR_ENVS:
            out.append(self._tabular(node))
            return
        if base in (
            "quote", "quotation", "verse", "displayquote", "displaycquote",
            # boxed-content environments -> a set-off Quote block (the closest IR)
            "framed", "shaded", "mdframed", "tcolorbox", "boxedminipage", "leftbar",
        ) or base in self.box_envs:
            out.append(ir.Quote(self.blocks(node.nodelist)))
            return
        if base in ("verbatim", "lstlisting", "minted"):
            out.append(ir.CodeBlock(_verbatim_inner(node), lang=None))
            return
        if base == "abstract":
            self.meta.abstract = self.blocks(node.nodelist)
            return
        if base in ("center", "flushleft", "flushright"):
            align = {"center": "center", "flushleft": "left", "flushright": "right"}[base]
            for blk in self.blocks(node.nodelist):
                if isinstance(blk, ir.Paragraph) and blk.align is None:
                    blk.align = align
                out.append(blk)
            return
        if base in ("document", "minipage"):
            out.extend(self.blocks(node.nodelist))
            return
        if base == "thebibliography":
            out.append(self._thebibliography(node))
            return
        if base in self.theorem_envs or base == "proof":
            out.append(self._theorem(node, base, starred))
            return
        if base in ("algorithm", "algorithm2e"):
            out.append(self._algorithm(node))
            return
        if base in self.custom_floats:
            out.append(self._custom_float(node, base))
            return
        if base in _OPAQUE_ENVS:
            # graphics we cannot translate -> placeholder + warning (PRD: TikZ).
            self.report.warn(name, f"{name} kept as a graphics placeholder")
            out.append(ir.Figure(image=None, source=node.latex_verbatim()))
            return
        # A custom wrapper that contains \item is a list (e.g. a user-defined
        # `ul`/`enum` wrapping itemize/enumerate) -> render as a list so the
        # items keep their structure instead of leaking "\item".
        if any(
            isinstance(c, LatexMacroNode) and c.macroname == "item" for c in node.nodelist
        ):
            ordered = "enum" in base or "ordered" in base or "number" in base
            self.report.info(name, f"custom list environment {name}: rendered as a list")
            out.append(self._list(node, ordered=ordered))
            return
        # Known structural wrappers that carry no Word-visible formatting of their
        # own: pass the content through silently (no warning).
        if base in _TRANSPARENT_ENVS:
            out.extend(self.blocks(node.nodelist))
            return
        # Otherwise treat the unknown environment as a transparent wrapper so its
        # content is preserved (boxes, algorithm, subfig, ...).
        self.report.warn(name, f"unknown environment {name}: treated as transparent")
        out.extend(self.blocks(node.nodelist))

    def _theorem(self, node: LatexEnvironmentNode, base: str, starred: bool) -> ir.Theorem:
        is_proof = base == "proof"
        display = "Proof" if is_proof else self.theorem_envs.get(base, base.capitalize())
        title = self._env_optional_title(node)
        # proof is unnumbered; so are starred uses (\begin{theorem*}) and any
        # environment defined with \newtheorem* (no counter).
        unnumbered = is_proof or starred or base in self.unnumbered_theorems
        # a shared-counter env (\newtheorem{LEM}[THM]{Lemma}) numbers against the
        # counter it shares (THM's "Theorem"), so they form one running sequence.
        counter = None if unnumbered else self.theorem_counters.get(base, display)
        theorem = ir.Theorem(kind=display, blocks=[], title=title, counter=counter)
        # become the label target *before* parsing the body, so a \label at the
        # start of the environment attaches here, not to the preceding block.
        self._labelable = theorem
        theorem.blocks = self.blocks(node.nodelist)
        return theorem

    def _algorithm(self, node: LatexEnvironmentNode) -> ir.Algorithm:
        from .algorithms import parse_algorithm_body

        caption: list[ir.Inline] | None = None
        label: str | None = None
        for child in _walk_macros(node.nodelist):
            if child.macroname == "caption":
                caption = self.inlines(_group_nodes(child))
            elif child.macroname == "label":
                label = _chars_of(_group_nodes(child))
        lines = parse_algorithm_body(node.nodelist, self.inlines)
        alg = ir.Algorithm(lines=lines, caption=caption, label=label)
        self._labelable = alg
        return alg

    def _custom_float(self, node: LatexEnvironmentNode, base: str) -> ir.Float:
        caption: list[ir.Inline] | None = None
        caption_numbered = True
        caption_above = False
        label: str | None = None
        align: ir.TableAlign | None = None
        content: list = []
        seen_body = False
        for child in node.nodelist:
            if isinstance(child, LatexMacroNode) and child.macroname == "caption":
                caption = self.inlines(_group_nodes(child))
                caption_numbered = not _has_star(child)
                caption_above = not seen_body
            elif isinstance(child, LatexMacroNode) and child.macroname == "label":
                label = _chars_of(_group_nodes(child))
            elif (
                isinstance(child, LatexMacroNode)
                and child.macroname in _FLOAT_ALIGN_MACROS
            ):
                align = _FLOAT_ALIGN_MACROS[child.macroname]
            else:
                if not seen_body and _float_body_node_visible(child):
                    seen_body = True
                content.append(child)
        blocks = self.blocks(content)
        if align is not None:
            _apply_float_align(blocks, align)
        flt = ir.Float(
            kind=base,
            counter=self.custom_floats.get(base, base.capitalize()),
            blocks=blocks,
            caption=caption,
            label=label,
            caption_numbered=caption_numbered,
            caption_above=caption_above,
            source=node.latex_verbatim(),
        )
        self._labelable = flt
        return flt

    def _env_optional_title(self, node: LatexEnvironmentNode) -> list[ir.Inline] | None:
        argd = node.nodeargd
        if argd and argd.argnlist:
            opt = argd.argnlist[0]
            if isinstance(opt, LatexGroupNode) and opt.nodelist:
                return self.inlines(opt.nodelist)
        return None

    def _thebibliography(self, node: LatexEnvironmentNode) -> ir.Bibliography:
        """Parse a ``thebibliography`` env's ``\\bibitem`` entries into CSL items."""
        key: str | None = None
        buf: list = []
        order = 0

        def flush() -> None:
            nonlocal order
            if key is not None:
                text = _inlines_to_text(self.inlines(buf))
                order += 1
                self.thebib_items[key] = ir.CSLItem(
                    id=key, type="document", csl_fields={"note": text, "_order": order}
                )
            buf.clear()

        for child in node.nodelist:
            if isinstance(child, LatexMacroNode) and child.macroname == "bibitem":
                flush()
                key = _chars_of(_group_nodes(child))
            elif key is not None:
                buf.append(child)
        flush()
        return ir.Bibliography(entries=[], style="numeric")

    def _list(
        self, node: LatexEnvironmentNode, ordered: bool, description: bool = False
    ) -> ir.ItemList:
        items: list[ir.ListItem] = []
        current: list = []
        term: list[ir.Inline] | None = None
        started = False

        def flush_item() -> None:
            nonlocal term
            # become the label target before parsing the body, so an \item\label{}
            # attaches to this item (referenceable list number), not the preceding
            # block -- mirroring how _theorem captures its label.
            item = ir.ListItem([], term=term)
            self._labelable = item
            item.blocks = self.blocks(current.copy())
            items.append(item)
            current.clear()
            term = None

        for child in node.nodelist:
            if isinstance(child, LatexMacroNode) and child.macroname == "item":
                if started:
                    flush_item()
                started = True
                term = self._item_term(child)
            elif started:
                current.append(child)
            # content before the first \item is discarded (it isn't an item)
        if started:
            flush_item()
        return ir.ItemList(ordered=ordered, items=items, description=description)

    def _cite_locators(self, node: LatexMacroNode) -> tuple[str | None, str | None]:
        """Extract ``\\citep[pre][post]{key}`` locators.

        natbib: a single optional is the *post*-note (suffix); two optionals are
        (pre, post).
        """
        argd = node.nodeargd
        opts = [
            _chars_of(a.nodelist)
            for a in (argd.argnlist if argd else [])
            if isinstance(a, LatexGroupNode) and a.delimiters == ("[", "]")
        ]
        if len(opts) >= 2:
            return (opts[0] or None, opts[1] or None)
        if len(opts) == 1:
            return (None, opts[0] or None)
        return (None, None)

    def _item_term(self, node: LatexMacroNode) -> list[ir.Inline] | None:
        """Extract the optional ``\\item[term]`` label (description lists)."""
        argd = node.nodeargd
        for arg in argd.argnlist if argd else []:
            if isinstance(arg, LatexGroupNode) and arg.delimiters == ("[", "]"):
                return _clean_inlines(self.inlines(arg.nodelist), trim=True)
        return None

    def _figure(self, node: LatexEnvironmentNode, spanning: bool = False) -> ir.Figure:
        fig = ir.Figure(image=None, source=node.latex_verbatim(), spanning=spanning)
        self._collect_figure_parts(node.nodelist, fig, top=True)
        if fig.image is None and not fig.subfigures:
            self.report.warn("figure", "figure without convertible graphics (e.g. TikZ)")
        self._labelable = fig
        return fig

    def _collect_figure_parts(self, nodes: list, fig: ir.Figure, top: bool) -> None:
        """Walk a figure body, pulling out the parent image/caption/label and
        any ``subfigure`` environments / ``\\subfloat`` commands."""
        for child in nodes:
            if isinstance(child, LatexEnvironmentNode):
                if child.environmentname.rstrip("*") == "subfigure":
                    fig.subfigures.append(self._subfigure_from_env(child))
                else:
                    self._collect_figure_parts(child.nodelist, fig, top=False)
            elif isinstance(child, LatexGroupNode):
                self._collect_figure_parts(child.nodelist, fig, top=False)
            elif isinstance(child, LatexMacroNode):
                if child.macroname in ("subfloat", "subfigure"):
                    fig.subfigures.append(self._subfigure_from_macro(child))
                elif child.macroname == "includegraphics":
                    fig.image = _make_image(child)
                elif child.macroname == "caption":
                    fig.caption = self.inlines(_group_nodes(child))
                    fig.caption_numbered = not _has_star(child)
                    # caption before any graphic/subfigure -> render above the image
                    fig.caption_above = fig.image is None and not fig.subfigures
                elif child.macroname == "label" and fig.label is None:
                    fig.label = _chars_of(_group_nodes(child))

    def _subfigure_from_env(self, node: LatexEnvironmentNode) -> ir.SubFigure:
        sub = ir.SubFigure(image=None)
        for child in _walk_macros(node.nodelist):
            if child.macroname == "includegraphics":
                sub.image = _make_image(child)
            elif child.macroname == "caption":
                sub.caption = self.inlines(_group_nodes(child))
                # caption before the graphic -> render above the image
                sub.caption_above = sub.image is None
            elif child.macroname == "label":
                sub.label = _chars_of(_group_nodes(child))
        return sub

    def _subfigure_from_macro(self, node: LatexMacroNode) -> ir.SubFigure:
        # \subfloat[caption]{content}; content holds the \includegraphics.
        groups = [
            a for a in (node.nodeargd.argnlist if node.nodeargd else [])
            if isinstance(a, LatexGroupNode)
        ]
        caption = self._env_optional_title(node)
        sub = ir.SubFigure(image=None, caption=caption)
        if groups:
            for child in _walk_macros(groups[-1].nodelist):
                if child.macroname == "includegraphics":
                    sub.image = _make_image(child)
                elif child.macroname == "label":
                    sub.label = _chars_of(_group_nodes(child))
        return sub

    def _figure_from_graphics(self, node: LatexMacroNode) -> ir.Figure:
        return ir.Figure(image=_make_image(node), source=node.latex_verbatim())

    def _table_float(
        self, node: LatexEnvironmentNode, out: list[ir.Block], spanning: bool = False
    ) -> None:
        # Pull the float's own caption/label, then process the rest as blocks --
        # which descends \resizebox/\scalebox and minipage so nested tabulars (a
        # grid of sub-tables) survive instead of being dumped as raw LaTeX.
        caption: list[ir.Inline] | None = None
        caption_numbered = True
        caption_above = False
        seen_tabular = False
        label: str | None = None
        align: ir.TableAlign | None = None
        content: list = []
        for child in node.nodelist:
            if isinstance(child, LatexMacroNode) and child.macroname == "caption":
                caption = self.inlines(_group_nodes(child))
                caption_numbered = not _has_star(child)
                # caption before the table body -> render above the table
                caption_above = not seen_tabular
            elif isinstance(child, LatexMacroNode) and child.macroname == "label":
                label = _chars_of(_group_nodes(child))
            elif (isinstance(child, LatexMacroNode)
                  and child.macroname in _FLOAT_ALIGN_MACROS):
                # \centering/\raggedright/\raggedleft inside the float aligns the
                # table body horizontally on the page.
                align = _FLOAT_ALIGN_MACROS[child.macroname]
            else:
                if not seen_tabular and _subtree_has_tabular(child):
                    seen_tabular = True
                content.append(child)
        sub_blocks = self.blocks(content)
        tables = [b for b in sub_blocks if isinstance(b, ir.Table)]
        if not tables:
            self.report.warn("table", "table float without tabular")
            out.append(ir.RawPassthrough(node.latex_verbatim(), "table without tabular"))
            return
        # attach the float caption/label to the first table (for \ref); a plain
        # single-tabular float behaves exactly as before.
        tables[0].caption = caption
        tables[0].caption_numbered = caption_numbered
        tables[0].caption_above = caption_above
        tables[0].label = label
        tables[0].spanning = spanning
        # a float-level \centering aligns every tabular it contains
        if align is not None:
            for table in tables:
                table.align = align
        self._labelable = tables[0]
        out.extend(sub_blocks)

    def _tabular(self, node: LatexEnvironmentNode) -> ir.Table:
        # column spec is the first mandatory group argument of the environment
        colspec, colwidths = _parse_colspec(_env_colspec(node))
        booktabs = "\\toprule" in node.latex_verbatim() or "\\midrule" in node.latex_verbatim()
        three_line = _first_command_is_toprule(node.nodelist)
        rows = self._tabular_rows(node.nodelist, len(colspec) or 1, colspec)
        return ir.Table(rows=rows, colspec=colspec, booktabs=booktabs,
                        three_line=three_line, colwidths=colwidths)

    def _tabular_rows(self, nodes: list, ncols: int, colspec: list) -> list[ir.TableRow]:
        rows: list[ir.TableRow] = []
        cur_cells: list[list] = [[]]
        row_started = [False]
        row_shade: list[str | None] = [None]  # \rowcolor applies to the whole row
        # number of leading rows that form the (repeatable) header; None until a
        # header delimiter (\midrule / \endhead) is seen.
        header_end: list[int | None] = [None]

        def end_row() -> None:
            if row_started[0]:
                cells = []
                for ci, cell_nodes in enumerate(cur_cells):
                    align = colspec[ci] if ci < len(colspec) else "left"
                    cell = self._make_cell(cell_nodes, align)
                    if cell.shade is None:  # \cellcolor overrides the row colour
                        cell.shade = row_shade[0]
                    cells.append(cell)
                rows.append(ir.TableRow(cells))
            cur_cells.clear()
            cur_cells.append([])
            row_started[0] = False
            row_shade[0] = None

        for child in nodes:
            if isinstance(child, LatexSpecialsNode) and child.specials_chars == "&":
                cur_cells.append([])
                row_started[0] = True
                continue
            if isinstance(child, LatexMacroNode) and child.macroname == "rowcolor":
                row_shade[0] = self._color_of(child)
                continue
            if isinstance(child, LatexMacroNode) and child.macroname in ("\\", "tabularnewline"):
                end_row()
                continue
            if isinstance(child, LatexMacroNode) and child.macroname in (
                "midrule", "endhead", "endfirsthead",
            ):
                end_row()
                if header_end[0] is None:
                    header_end[0] = len(rows)
                continue
            if isinstance(child, LatexMacroNode) and child.macroname in ("cline", "cmidrule"):
                rng = _cmidrule_range(child)
                if rng is not None and rows:
                    _apply_partial_rule(rows[-1], rng)
                continue
            if isinstance(child, LatexMacroNode) and child.macroname in (
                "hline", "toprule", "bottomrule", "endfoot", "endlastfoot",
                "morecmidrules", "addlinespace",
            ):
                continue
            if isinstance(child, LatexCharsNode) and not child.chars.strip():
                cur_cells[-1].append(child)
                continue
            cur_cells[-1].append(child)
            row_started[0] = True
        end_row()

        if header_end[0]:
            for row in rows[: header_end[0]]:
                row.is_header = True
        return rows

    def _strip_cellcolor(self, nodes: list) -> tuple[str | None, list]:
        """Pull any ``\\cellcolor[model]{c}`` out of a node list; return (shade, rest)."""
        shade: str | None = None
        content: list = []
        for n in nodes:
            if isinstance(n, LatexMacroNode) and n.macroname == "cellcolor":
                shade = self._color_of(n) or shade
            else:
                content.append(n)
        return shade, content

    def _make_cell(self, cell_nodes: list, align) -> ir.TableCell:
        shade, cell_nodes = self._strip_cellcolor(cell_nodes)
        colspan, rowspan = 1, 1
        # Peel \multicolumn / \multirow wrappers. They nest in either order in
        # generated colour tables, e.g. \multicolumn{1}{c|}{\multirow{-2}{*}{X}};
        # loop so both the horizontal span (gridSpan) and vertical span (vMerge)
        # plus the innermost content are recovered instead of one being lost.
        peeled = True
        while peeled:
            peeled = False
            for m in cell_nodes:
                if not isinstance(m, LatexMacroNode):
                    continue
                groups = [
                    a for a in (m.nodeargd.argnlist if m.nodeargd else [])
                    if isinstance(a, LatexGroupNode)
                ]
                if m.macroname == "multicolumn" and len(groups) >= 3:
                    colspan = _int_or_default(_chars_of(groups[0].nodelist), 1)
                    cspec, _ = _parse_colspec(_chars_of(groups[1].nodelist))
                    align = cspec[0] if cspec else "center"
                    inner_shade, cell_nodes = self._strip_cellcolor(groups[2].nodelist)
                    shade = shade or inner_shade
                    peeled = True
                    break
                if m.macroname == "multirow" and len(groups) >= 2:
                    # \multirow{n}{width}{content}; the width is irrelevant and is
                    # often the bare ``*`` (\multirow{6}*{X}, only two groups), so
                    # take span from the first group and content from the last.
                    # A negative span (\multirow{-2}{*}{X}, generated tables anchor
                    # the content in the *bottom* row) can't drive a top-down
                    # w:vMerge, so we keep the content without merging.
                    span = _int_or_default(_chars_of(groups[0].nodelist), 1)
                    if span > 1:
                        rowspan = span
                    inner_shade, cell_nodes = self._strip_cellcolor(groups[-1].nodelist)
                    shade = shade or inner_shade
                    peeled = True
                    break
        return ir.TableCell(
            _flatten_stacked_tables(self.blocks(cell_nodes)),
            colspan=colspan,
            rowspan=rowspan,
            align=align,
            shade=shade,
        )

def _has_star(node: LatexMacroNode) -> bool:
    """True if a macro was invoked with a starred form (e.g. ``\\section*``)."""
    argd = node.nodeargd
    if not argd or not argd.argnlist:
        return False
    first = argd.argnlist[0]
    return first is not None and getattr(first, "chars", None) == "*"


def _int_or_default(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default


def _inlines_to_text(inlines: list[ir.Inline]) -> str:
    out: list[str] = []
    for node in inlines:
        if isinstance(node, ir.Text):
            out.append(node.value)
        elif isinstance(node, ir.Emphasis | ir.CharStyle | ir.Link | ir.Footnote | ir.Endnote):
            out.append(_inlines_to_text(node.inlines))
        elif isinstance(node, ir.Math):
            out.append(node.latex)
    return " ".join(" ".join(out).split())


def _field_cached_text(inlines: list[ir.Inline]) -> str:
    """Flatten a field's cached-result argument without inserting extra spaces."""
    out: list[str] = []
    for node in inlines:
        if isinstance(node, ir.Text):
            out.append(node.value)
        elif isinstance(node, ir.Emphasis | ir.CharStyle | ir.Link | ir.Colored | ir.FontSize):
            out.append(_field_cached_text(node.inlines))
        elif isinstance(node, ir.Math):
            out.append(node.latex)
        elif isinstance(node, ir.LineBreak):
            out.append("\n")
        elif isinstance(node, ir.RawInline):
            out.append(node.latex)
    return "".join(out).strip()


def _walk_macros(nodes: list):
    for n in nodes:
        if isinstance(n, LatexMacroNode):
            yield n
        elif isinstance(n, LatexGroupNode):
            yield from _walk_macros(n.nodelist)
        elif isinstance(n, LatexEnvironmentNode):
            yield from _walk_macros(n.nodelist)


def _float_body_node_visible(node) -> bool:
    """Whether a custom-float child is body content for caption placement."""
    if isinstance(node, LatexCommentNode):
        return False
    if isinstance(node, LatexCharsNode):
        return bool(node.chars.strip())
    if isinstance(node, LatexMacroNode):
        return (
            node.macroname not in {"caption", "label", *_FLOAT_ALIGN_MACROS}
            and node.macroname not in _IGNORE_MACROS
        )
    if isinstance(node, LatexGroupNode):
        return any(_float_body_node_visible(c) for c in node.nodelist)
    return True


def _apply_float_align(blocks: list[ir.Block], align: ir.TableAlign) -> None:
    """Apply a float-level alignment declaration to block types that support it."""
    for block in blocks:
        if isinstance(block, ir.Paragraph) and block.align is None:
            block.align = align
        elif isinstance(block, ir.Table) and block.align is None:
            block.align = align


# --------------------------------------------------------------------------- #
# Free helpers
# --------------------------------------------------------------------------- #


# CJK codepoints: a line break (single newline) between two such characters
# must not leave a space, matching xeCJK behaviour. Covers unified ideographs
# (+ ext A), compatibility ideographs, kana, Hangul, and CJK/fullwidth symbols.
_CJK_CHAR = (
    "⺀-⻿"   # CJK radicals supplement
    "　-〿"   # CJK symbols & punctuation
    "぀-ヿ"   # hiragana + katakana
    "㐀-䶿"   # CJK ext A
    "一-鿿"   # CJK unified ideographs
    "가-힯"   # Hangul syllables
    "豈-﫿"   # CJK compatibility ideographs
    "＀-￯"   # halfwidth & fullwidth forms
)
_CJK_GAP = re.compile("(?<=[" + _CJK_CHAR + "]) (?=[" + _CJK_CHAR + "])")


# Directly-typed CJK-style curly quotes. Split off into their own ``ir.Text``
# (tagged ``cjk_quote``) so the back-end can give them an East-Asian font hint in
# a Chinese document -- LaTeX-command quotes stay untagged (English font).
_TYPED_QUOTES = "“”‘’"  # “ ” ‘ ’
# LaTeX single-quote syntax delivered as plain chars (the double ``/'' forms are
# specials handled in _inline_special): ` -> ‘ and ' -> ’. These are a LaTeX
# command, so they render curly but keep the default (Latin) font -> untagged.
_ASCII_QUOTE_MAP = {"`": "‘", "'": "’"}


def _typed_quote_runs(text: str) -> list[ir.Text]:
    runs: list[ir.Text] = []
    buf: list[str] = []
    for ch in text:
        if ch in _TYPED_QUOTES:
            if buf:
                runs.append(ir.Text("".join(buf)))
                buf = []
            runs.append(ir.Text(ch, cjk_quote=True))
        else:
            # ``/'' singles -> curly but English (stay in the untagged buffer).
            buf.append(_ASCII_QUOTE_MAP.get(ch, ch))
    if buf:
        runs.append(ir.Text("".join(buf)))
    return runs


def _normalize_ws(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # Drop the space left where a newline joined two CJK characters.
    return _CJK_GAP.sub("", text)


# Punctuation that should not be preceded by a space (e.g. a space inserted by
# \xspace before a colon).
_SPACE_BEFORE_PUNCT = re.compile(r" +([,.;:!?)\]}])")


def _clean_inlines(nodes: list[ir.Inline], *, trim: bool = False) -> list[ir.Inline]:
    """Tidy a freshly built inline list.

    Merges adjacent Text runs (so spacing introduced by dropped/space-emitting
    macros like ``\\xspace`` collapses), squeezes repeated spaces, and removes a
    space sitting before closing punctuation. With ``trim`` (used at block
    level) it also strips leading/trailing whitespace of the paragraph.
    """
    merged: list[ir.Inline] = []
    for node in nodes:
        if (isinstance(node, ir.Text) and merged and isinstance(merged[-1], ir.Text)
                and merged[-1].cjk_quote == node.cjk_quote):
            merged[-1] = ir.Text(merged[-1].value + node.value, cjk_quote=node.cjk_quote)
        else:
            merged.append(node)
    for node in merged:
        if isinstance(node, ir.Text):
            value = re.sub(r" {2,}", " ", node.value)
            node.value = _SPACE_BEFORE_PUNCT.sub(r"\1", value)
    if trim:
        if merged and isinstance(merged[0], ir.Text):
            merged[0].value = merged[0].value.lstrip()
        if merged and isinstance(merged[-1], ir.Text):
            merged[-1].value = merged[-1].value.rstrip()
        merged = [n for n in merged if not (isinstance(n, ir.Text) and n.value == "")]
    return merged


def _flush_cleaned(
    cleaned: list[ir.Inline], out: list[ir.Block], *, style: str | None = None
) -> None:
    """Emit a finished paragraph's inlines as one or more blocks.

    A paragraph that carries a display equation *alongside* text becomes a single
    :class:`ir.Paragraph` -- the back-end keeps the equation in the same Word
    paragraph, joined to the text by soft line breaks. A paragraph whose only real
    content is display math degrades back to standalone :class:`ir.MathBlock`s, so
    an isolated equation (blank line on both sides) renders exactly as before.

    ``style`` is a ``\\texwordparstyle`` per-paragraph Word style name; it is set on
    every :class:`ir.Paragraph` this flush produces (display math stays unstyled).
    """
    if not any(isinstance(x, ir.DisplayMath) for x in cleaned):
        out.append(ir.Paragraph(cleaned, style=style))
        return
    # only keep the equation inline with its paragraph when genuine prose text
    # surrounds it (the "说明文字" case); otherwise an equation flanked solely by
    # other inlines (e.g. inline math) keeps the historical block split.
    has_prose = any(isinstance(x, ir.Text) and x.value.strip() for x in cleaned)
    if has_prose:
        out.append(ir.Paragraph(_trim_around_display_math(cleaned), style=style))
        return
    group: list[ir.Inline] = []
    for x in cleaned:
        if isinstance(x, ir.DisplayMath):
            if any(not (isinstance(g, ir.Text) and not g.value.strip()) for g in group):
                out.append(ir.Paragraph(group, style=style))
            group = []
            out.append(x.to_block())
        else:
            group.append(x)
    if any(not (isinstance(g, ir.Text) and not g.value.strip()) for g in group):
        out.append(ir.Paragraph(group, style=style))


def _trim_around_display_math(inlines: list[ir.Inline]) -> list[ir.Inline]:
    """Drop the whitespace a line break left between text and a display equation.

    The newline that separated the explanatory text from the equation in the
    source collapses to a space; with the equation now joined by a soft break the
    space is redundant, so trim text touching a :class:`ir.DisplayMath`."""
    for i, node in enumerate(inlines):
        if not isinstance(node, ir.DisplayMath):
            continue
        prev = inlines[i - 1] if i > 0 else None
        if isinstance(prev, ir.Text):
            prev.value = prev.value.rstrip()
        nxt = inlines[i + 1] if i + 1 < len(inlines) else None
        if isinstance(nxt, ir.Text):
            nxt.value = nxt.value.lstrip()
    return [n for n in inlines if not (isinstance(n, ir.Text) and n.value == "")]


def _is_display(node: LatexMathNode) -> bool:
    delims = getattr(node, "delimiters", ("$", "$"))
    return delims[0] in ("\\[", "$$")


def _strip_math_delims(verbatim: str, env: str) -> str:
    v = verbatim.strip()
    for op, cl in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)"), ("$", "$")):
        if v.startswith(op) and v.endswith(cl):
            return v[len(op) : len(v) - len(cl)].strip()
    if v.startswith("\\begin"):
        # \begin{env} ... \end{env}
        start = v.find("}")
        end = v.rfind("\\end")
        if start != -1 and end != -1:
            return v[start + 1 : end].strip()
    return v


_LABEL_TAG_RE = re.compile(r"\\(?:label|tag|nonumber|notag)\b\s*(?:\{[^{}]*\})?")


def _extract_label(latex: str) -> str | None:
    m = re.search(r"\\label\{([^}]*)\}", latex)
    return m.group(1) if m else None


def _env_colspec(node: LatexEnvironmentNode) -> str:
    # The column spec is the *last* mandatory group: plain tabular has just
    # {colspec}, but tabularx/tabulary carry a leading {width} we must skip.
    argd = node.nodeargd
    if argd and argd.argnlist:
        groups = [a for a in argd.argnlist if isinstance(a, LatexGroupNode)]
        if groups:
            verb = groups[-1].latex_verbatim().strip()
            if verb.startswith("{") and verb.endswith("}"):
                return verb[1:-1]  # keep raw text so p{3cm} braces survive
            return _chars_of(groups[-1].nodelist)
        for a in argd.argnlist:
            if isinstance(a, LatexCharsNode):
                return a.chars
    return ""


_CMIDRULE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def _first_command_is_toprule(nodes: list) -> bool:
    """Whether the first non-blank command inside a tabular body is ``\\toprule``.

    Marks a booktabs three-line table (三线表): leading whitespace/comments are
    skipped and the first macro encountered must be ``\\toprule``.
    """
    for child in nodes:
        if isinstance(child, LatexCommentNode):
            continue
        if isinstance(child, LatexCharsNode) and not child.chars.strip():
            continue
        return isinstance(child, LatexMacroNode) and child.macroname == "toprule"
    return False


def _cmidrule_range(node: LatexMacroNode) -> tuple[int, int] | None:
    """The 1-based ``{a-b}`` column range of a ``\\cmidrule``/``\\cline``.

    Tolerates the ``\\cmidrule(lr){a-b}`` trim form by scanning the verbatim.
    """
    text = _chars_of(_group_nodes(node)) or node.latex_verbatim()
    m = _CMIDRULE_RE.search(text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if a <= b else (b, a)


def _apply_partial_rule(row: ir.TableRow, rng: tuple[int, int]) -> None:
    """Set ``border_bottom`` on the cells of ``row`` overlapping 1-based ``rng``."""
    a, b = rng
    col = 1  # 1-based start column of the current cell
    for cell in row.cells:
        cell_end = col + cell.colspan - 1
        if col <= b and cell_end >= a:  # overlaps [a, b]
            cell.border_bottom = True
        col = cell_end + 1


def _braced_group(spec: str, start: int) -> tuple[str, int]:
    """Read a ``{...}`` group beginning at ``spec[start] == '{'``; return (body, end)."""
    depth, i = 0, start
    while i < len(spec):
        if spec[i] == "{":
            depth += 1
        elif spec[i] == "}":
            depth -= 1
            if depth == 0:
                return spec[start + 1:i], i
        i += 1
    return spec[start + 1:], len(spec)


_COL_PROCESSOR_ALIGN = {
    "centering": "center", "raggedright": "left", "raggedleft": "right",
}

#: Float-level alignment declarations (\centering etc.) inside a table/figure.
_FLOAT_ALIGN_MACROS: dict[str, ir.TableAlign] = {
    "centering": "center", "raggedright": "left", "raggedleft": "right",
}


def _parse_colspec(spec: str) -> tuple[list, list[float | None]]:
    """Parse a column spec into (alignments, per-column EMU widths or None)."""
    aligns: list = []
    widths: list[float | None] = []
    pending_align: str | None = None  # set by a >{...} column processor
    i = 0
    while i < len(spec):
        c = spec[i]
        if c == ">" and i + 1 < len(spec) and spec[i + 1] == "{":
            body, i = _braced_group(spec, i + 1)
            for key, al in _COL_PROCESSOR_ALIGN.items():
                if f"\\{key}" in body:
                    pending_align = al
            i += 1
            continue
        if c in "lcr":
            aligns.append(pending_align or {"l": "left", "c": "center", "r": "right"}[c])
            widths.append(None)
            pending_align = None
        elif c in "pmb" and i + 1 < len(spec) and spec[i + 1] == "{":
            aligns.append(pending_align or "left")
            pending_align = None
            body, i = _braced_group(spec, i + 1)
            widths.append(_length_to_emu(body))
        # ignore |, @{...}, etc.
        i += 1
    return aligns, widths


#: LaTeX length units -> EMU (914400 per inch). bp/pt approximated as points.
_EMU_PER_INCH = 914400
_UNIT_EMU = {
    "in": _EMU_PER_INCH, "pt": _EMU_PER_INCH / 72.27, "bp": _EMU_PER_INCH / 72.0,
    "cm": _EMU_PER_INCH / 2.54, "mm": _EMU_PER_INCH / 25.4, "px": _EMU_PER_INCH / 96.0,
    "em": _EMU_PER_INCH / 72.27 * 10, "ex": _EMU_PER_INCH / 72.27 * 4.3,
}
_LEN_RE = re.compile(r"^\s*([-\d.]+)\s*([a-zA-Z]*)")


def _length_to_emu(value: str) -> float | None:
    """Convert a LaTeX length (``2.5cm``, ``300pt``) to EMU; relative widths skip."""
    value = value.strip()
    if "\\" in value:
        # relative to \linewidth/\textwidth/\columnwidth -- let the backend fit
        return None
    m = _LEN_RE.match(value)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "pt").lower()
    if unit not in _UNIT_EMU:
        return None
    return num * _UNIT_EMU[unit]


#: Environment names that hold a table body (used to place a float's caption).
_TABULAR_ENVS = frozenset({
    "tabular", "array", "tabularx", "tabulary", "longtable",
    "supertabular", "xtabular", "mpsupertabular",
})


def _subtree_has_tabular(node) -> bool:
    """True if *node* is, or contains anywhere below it, a tabular environment.

    Used to tell whether a table float's body precedes its ``\\caption`` (it may
    be wrapped in ``\\resizebox``/``\\centering``/minipage), so the caption is
    placed above only when ``\\caption`` truly comes first in the source.
    """
    if isinstance(node, LatexEnvironmentNode):
        if node.environmentname.rstrip("*") in _TABULAR_ENVS:
            return True
        return any(_subtree_has_tabular(c) for c in node.nodelist)
    children = getattr(node, "nodelist", None)
    if children is not None:
        return any(_subtree_has_tabular(c) for c in children)
    argd = getattr(node, "nodeargd", None)
    if argd is not None and getattr(argd, "argnlist", None):
        return any(a is not None and _subtree_has_tabular(a) for a in argd.argnlist)
    return False


def _parse_graphics_options(opts: str) -> dict[str, str]:
    """Parse the ``[key=val,key=val,flag]`` option list of \\includegraphics."""
    out: dict[str, str] = {}
    depth = 0
    buf: list[str] = []
    parts: list[str] = []
    for ch in opts:  # split on commas not inside braces/brackets
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    for part in parts:
        if "=" in part:
            key, _, val = part.partition("=")
            out[key.strip().lower()] = val.strip()
        elif part.strip():
            out[part.strip().lower()] = ""
    return out


def _parse_printbibliography_options(node: LatexMacroNode) -> dict[str, str]:
    opt = _optional_group(node)
    if opt is None:
        return {}
    return _parse_graphics_options(_latex_of(opt))


def _strip_outer_braces(value: str) -> str:
    if not (value.startswith("{") and value.endswith("}")):
        return value
    depth = 0
    for i, ch in enumerate(value):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and i != len(value) - 1:
                return value
    return value[1:-1]


def _make_image(node: LatexMacroNode) -> ir.Image:
    path = _chars_of(_group_nodes(node))
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    image = ir.Image(path=path, original_format=ext, alt=path)
    opts = _graphics_option_text(node)
    if opts:
        parsed = _parse_graphics_options(opts)
        if "width" in parsed:
            image.width = _length_to_emu(parsed["width"])
        if "height" in parsed:
            image.height = _length_to_emu(parsed["height"])
        if "scale" in parsed:
            try:
                image.scale = float(parsed["scale"])
            except ValueError:
                pass
        if "angle" in parsed:
            try:
                image.angle = float(parsed["angle"])
            except ValueError:
                pass
        if "clip" in parsed:
            image.clip = True
        if "trim" in parsed:
            trims = [_length_to_emu(v) for v in parsed["trim"].split()]
            if len(trims) == 4 and all(t is not None for t in trims):
                image.trim = [t for t in trims if t is not None]
    return image


def _graphics_option_text(node: LatexMacroNode) -> str:
    """The raw ``[...]`` optional-argument text of \\includegraphics, if any.

    Uses verbatim text, not ``_chars_of``: the latter drops macro tokens, so
    ``width=0.5\\linewidth`` would collapse to ``width=0.5`` and be misread as
    0.5pt instead of a relative width (which must fall back to fit-to-column).
    """
    for a in node.nodeargd.argnlist if node.nodeargd else []:
        if isinstance(a, LatexGroupNode) and a.delimiters and a.delimiters[0] == "[":
            verb = a.latex_verbatim().strip()
            if verb.startswith("[") and verb.endswith("]"):
                return verb[1:-1]
            return _chars_of(a.nodelist)
    return ""


# Text-mode accents via combining characters.
_ACCENT_COMBINING = {
    "'": "́", "`": "̀", "^": "̂", '"': "̈",
    "~": "̃", "=": "̄", ".": "̇", "v": "̌",
    "u": "̆", "c": "̧", "H": "̋", "r": "̊",
}


def _accent_char(name: str) -> str | None:
    return _ACCENT_COMBINING.get(name)


def _apply_text_accent(name: str, base: str) -> str:
    import unicodedata

    comb = _ACCENT_COMBINING.get(name, "")
    if not base:
        return comb
    return unicodedata.normalize("NFC", base[0] + comb + base[1:])


# User-defined tcolorbox callout environments -> rendered as set-off Quote blocks.
# \newtcolorbox[init opts]{name}{style}, \DeclareTColorBox{name}{spec}{style},
# \newtcbox / \NewTColorBox / \ProvideTColorBox variants.
_NEWTCOLORBOX_RE = re.compile(
    r"\\(?:new|renew|provide|Declare|New|Renew|Provide)?"
    r"(?:tcolorbox|TColorBox|tcbox)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}"
)


def _collect_tcolorbox_envs(source: str) -> set[str]:
    """Names of user ``\\newtcolorbox`` environments (rendered as Quote blocks)."""
    return {m.group(1).strip() for m in _NEWTCOLORBOX_RE.finditer(source) if m.group(1).strip()}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


# \newtheorem{env}{Display}, \newtheorem{env}[shared]{Display}{,
# \newtheorem{env}{Display}[parent], and the unnumbered \newtheorem*{env}{Display}.
# The display group may itself wrap the title in a font command -- real preambles
# write \newtheorem{THM}{\textbf{Theorem}} -- so allow one level of nested braces
# and clean the result below.
_NEWTHEOREM_RE = re.compile(
    r"\\newtheorem(\*?)\s*\{([^}]*)\}\s*(?:\[([^\]]*)\])?\s*"
    r"\{((?:[^{}]|\{[^{}]*\})*)\}"
)
# Font/format declarations to peel off a \newtheorem display title.
_TITLE_DECL_RE = re.compile(
    r"\\(?:textbf|textit|textrm|textsf|textsc|textnormal|texttt|emph|mathrm|"
    r"bfseries|itshape|scshape|sffamily|mdseries|normalfont|upshape|rmfamily)\b"
)


def _clean_theorem_title(raw: str) -> str:
    """Strip wrapping font commands/braces from a ``\\newtheorem`` display title."""
    title = _TITLE_DECL_RE.sub("", raw)
    title = title.replace("{", "").replace("}", "")
    return _normalize_ws(title).strip()


def _collect_newtheorems(
    source: str,
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Map user ``\\newtheorem`` environment names to their display title.

    Returns ``(envs, unnumbered, shared)`` where ``unnumbered`` holds the names
    defined with the starred ``\\newtheorem*`` (no counter) and ``shared`` maps an
    environment to the environment whose counter it shares
    (``\\newtheorem{LEM}[THM]{Lemma}`` -> ``{"LEM": "THM"}``).
    """
    envs: dict[str, str] = {}
    unnumbered: set[str] = set()
    shared: dict[str, str] = {}
    for m in _NEWTHEOREM_RE.finditer(source):
        name = m.group(2).strip()
        display = _clean_theorem_title(m.group(4))
        if name and display:
            envs[name] = display
            if m.group(3):  # \newtheorem{env}[shared]{Display}: share a counter
                shared[name] = m.group(3).strip()
            if m.group(1):  # \newtheorem* -> unnumbered
                unnumbered.add(name)
    return envs, unnumbered, shared


def _resolve_theorem_counters(
    envs: dict[str, str], shared: dict[str, str]
) -> dict[str, str]:
    """Counter *display name* each theorem env numbers against.

    An env that shares another's counter (``LEM[THM]``) numbers against that
    env's display title, so they share one running sequence; otherwise it
    numbers against its own title.
    """
    counters: dict[str, str] = {}
    for name, display in envs.items():
        root, seen = name, {name}
        while root in shared and shared[root] not in seen:
            root = shared[root]
            seen.add(root)
        counters[name] = envs.get(root, display)
    return counters


_DECLARE_FLOAT_RE = re.compile(r"\\DeclareFloatingEnvironment\b")


def _read_balanced_group(
    source: str, i: int, open_ch: str, close_ch: str
) -> tuple[str, int] | None:
    if i >= len(source) or source[i] != open_ch:
        return None
    depth = 0
    escaped = False
    for j in range(i, len(source)):
        ch = source[j]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return source[i + 1:j], j + 1
    return source[i + 1:], len(source)


def _collect_declared_floats(source: str) -> dict[str, str]:
    """Map ``\\DeclareFloatingEnvironment`` declarations to env -> caption kind."""
    floats: dict[str, str] = {}
    pos = 0
    while True:
        m = _DECLARE_FLOAT_RE.search(source, pos)
        if m is None:
            break
        i = m.end()
        while i < len(source) and source[i].isspace():
            i += 1
        opts = ""
        if i < len(source) and source[i] == "[":
            group = _read_balanced_group(source, i, "[", "]")
            if group is None:
                pos = i + 1
                continue
            opts, i = group
        while i < len(source) and source[i].isspace():
            i += 1
        group = _read_balanced_group(source, i, "{", "}")
        if group is None:
            pos = i + 1
            continue
        env, pos = group
        env = env.strip()
        if not env:
            continue
        parsed = _parse_graphics_options(opts)
        display = _strip_outer_braces(parsed.get("name", "")).strip()
        display = _normalize_ws(display) if display else env.capitalize()
        floats[env] = display
    return floats


def _build_context(
    extra_theorem_envs: tuple[str, ...] = (),
    *,
    extra_macros: tuple[MacroSpec, ...] = (),
    extra_environments: tuple[EnvironmentSpec, ...] = (),
):
    """Augment pylatexenc's default DB with arg signatures it lacks."""
    ctx = get_default_latex_context_db()
    ctx.add_context_category(
        "tex2word",
        macros=[
            *extra_macros,
            # sectioning: \cmd*[short]{title} -- pylatexenc's defaults omit the
            # run-in \paragraph/\subparagraph, dropping their titles into the body
            MacroSpec("section", "*[{"),
            MacroSpec("subsection", "*[{"),
            MacroSpec("subsubsection", "*[{"),
            MacroSpec("paragraph", "*[{"),
            MacroSpec("subparagraph", "*[{"),
            MacroSpec("chapter", "*[{"),
            MacroSpec("part", "*[{"),
            MacroSpec("caption", "*[{"),
            MacroSpec("pagebreak", "["),
            # layout / front-matter commands: consume their args so they don't
            # leak as text (e.g. full-page cover \AddToShipoutPicture{\put...}).
            MacroSpec("AddToShipoutPicture", "*{"),
            # tex2word-only: bind a logical role to a Word style (consume 2 args).
            MacroSpec("texwordstyle", "{{"),
            # tex2word-only: name a Word reference template (optional [mode] +
            # the path arg). The optional [keep] selects content-injection mode.
            MacroSpec("texwordtemplate", "[{"),
            # tex2word-only: set the current paragraph's Word style (consume 1 arg).
            MacroSpec("texwordparstyle", "{"),
            # tex2word-only: set a Word character style (one style-name arg; an
            # optional following group is consumed by the builder as content).
            MacroSpec("texwordcharstyle", "{"),
            # tex2word-only: emit an arbitrary native Word field. The optional
            # argument is the cached result shown before Word refreshes fields.
            MacroSpec("texwordfield", "[{"),
            MacroSpec("DeclareFloatingEnvironment", "[{"),
            MacroSpec("newcounter", "{["),
            MacroSpec("addtocounter", "{{"),
            MacroSpec("refstepcounter", "{"),
            MacroSpec("stepcounter", "{"),
            MacroSpec("textsuperscript", "{"),
            MacroSpec("textsubscript", "{"),
            MacroSpec("sout", "{"),
            MacroSpec("uline", "{"),
            MacroSpec("uuline", "{"),
            MacroSpec("xout", "{"),
            MacroSpec("st", "{"),
            MacroSpec("hl", "{"),
            MacroSpec("href", "{{"),
            MacroSpec("url", "{"),
            MacroSpec("hyperref", "[{"),
            MacroSpec("multicolumn", "{{{"),
            MacroSpec("multirow", "{{{"),
            MacroSpec("citep", "[[{"),
            MacroSpec("citet", "[[{"),
            MacroSpec("Citep", "[[{"),
            MacroSpec("Citet", "[[{"),
            MacroSpec("citealp", "[[{"),
            MacroSpec("citealt", "[[{"),
            MacroSpec("citeauthor", "[[{"),
            MacroSpec("Citeauthor", "[[{"),
            MacroSpec("citeyear", "[[{"),
            MacroSpec("citeyearpar", "[[{"),
            MacroSpec("citenum", "[[{"),
            MacroSpec("smartcite", "[[{"),
            MacroSpec("parencite", "[[{"),
            MacroSpec("textcite", "[[{"),
            MacroSpec("autocite", "[[{"),
            MacroSpec("Autocite", "[[{"),
            MacroSpec("Parencite", "[[{"),
            MacroSpec("Textcite", "[[{"),
            MacroSpec("Cite", "[[{"),
            MacroSpec("IEEEPARstart", "{{"),
            MacroSpec("autoref", "{"),
            MacroSpec("cref", "{"),
            MacroSpec("Cref", "{"),
            MacroSpec("crefrange", "{{"),
            MacroSpec("Crefrange", "{{"),
            MacroSpec("eqref", "{"),
            MacroSpec("hypersetup", "{"),
            MacroSpec("graphicspath", "{"),
            MacroSpec("definecolor", "{{{"),
            MacroSpec("colorlet", "{{"),
            MacroSpec("textcolor", "[{{"),
            MacroSpec("colorbox", "[{{"),
            MacroSpec("fcolorbox", "[{{{"),
            MacroSpec("color", "[{"),
            MacroSpec("cellcolor", "[{"),
            MacroSpec("rowcolor", "[{"),
            MacroSpec("pagenumbering", "{"),
            MacroSpec("keywords", "{"),
            MacroSpec("IEEEkeywords", "{"),
            MacroSpec("endnote", "{"),
            MacroSpec("index", "{"),
            MacroSpec("enquote", "*{"),
            MacroSpec("textquote", "*[[{"),
            MacroSpec("foreignquote", "*{{"),
            MacroSpec("hyphenquote", "*{{"),
            MacroSpec("blockquote", "*[[{"),
            MacroSpec("blockcquote", "*[[{{"),
            MacroSpec("foreignblockquote", "*{[[{"),
            MacroSpec("epigraph", "{{"),
            MacroSpec("thanks", "{"),
            MacroSpec("marginpar", "[{"),
            MacroSpec("sidenote", "[{"),
            MacroSpec("footnotemark", "["),
            MacroSpec("footnotetext", "[{"),
            MacroSpec("nicefrac", "{{"),
            MacroSpec("sfrac", "{{"),
            MacroSpec("numrange", "[{{"),
            MacroSpec("SIrange", "[{{{"),
            MacroSpec("qtyrange", "[{{{"),
            MacroSpec("numlist", "[{"),
            MacroSpec("SIlist", "[{{"),
            MacroSpec("qtylist", "[{{"),
            MacroSpec("institute", "{"),
            MacroSpec("affiliation", "[{"),
            MacroSpec("affil", "[{"),
            MacroSpec("address", "{"),
            MacroSpec("email", "{"),
            MacroSpec("orcid", "{"),
            MacroSpec("inst", "{"),
            MacroSpec("IEEEauthorrefmark", "{"),
            MacroSpec("IEEEauthorblockN", "{"),
            MacroSpec("IEEEauthorblockA", "{"),
            # review annotations -> Word comments (todonotes / changes)
            MacroSpec("todo", "[{"),
            MacroSpec("comment", "[{"),
            MacroSpec("note", "[{"),
            MacroSpec("bibitem", "[{"),
            MacroSpec("bibliography", "{"),
            MacroSpec("bibliographystyle", "{"),
            MacroSpec("addbibresource", "[{"),
            MacroSpec("defbibheading", "{[{"),
            MacroSpec("printbibliography", "["),
            MacroSpec("setitemize", "{"),
            MacroSpec("setenumerate", "{"),
            MacroSpec("hyphenation", "{"),
            MacroSpec("cmidrule", "{"),
            MacroSpec("cline", "{"),
            MacroSpec("resizebox", "{{{"),
            MacroSpec("scalebox", "{{"),
            MacroSpec("setlength", "{{"),
            MacroSpec("phantom", "{"),
            MacroSpec("hphantom", "{"),
            MacroSpec("vphantom", "{"),
            MacroSpec("rule", "[{{"),
            MacroSpec("mbox", "{"),
            MacroSpec("fbox", "{"),
            MacroSpec("framebox", "[[{"),
            MacroSpec("makebox", "[[{"),
            MacroSpec("raisebox", "{[[{"),
            MacroSpec("fontsize", "{{"),
            MacroSpec("fontfamily", "{"),
            MacroSpec("si", "[{"),
            MacroSpec("unit", "[{"),
            MacroSpec("num", "[{"),
            MacroSpec("ang", "[{"),
            MacroSpec("SI", "[{{"),
            MacroSpec("qty", "[{{"),
            MacroSpec("newacronym", "[{{{"),
            MacroSpec("newglossaryentry", "{{"),
            MacroSpec("nocite", "{"),
            MacroSpec("lstinline", "[{"),
            MacroSpec("mintinline", "[{{"),
            MacroSpec("ensuremath", "{"),
            MacroSpec("ding", "{"),
            MacroSpec("shortstack", "[{"),
            # ACM affiliation sub-fields: consume the {…} so it isn't body text.
            MacroSpec("institution", "{"),
            MacroSpec("department", "{"),
            MacroSpec("city", "{"),
            MacroSpec("state", "{"),
            MacroSpec("country", "{"),
            MacroSpec("postcode", "{"),
            MacroSpec("streetaddress", "{"),
            MacroSpec("position", "{"),
            MacroSpec("authornote", "{"),
            MacroSpec("texorpdfstring", "{{"),
            MacroSpec("labelcref", "{"),
            MacroSpec("nameref", "{"),
            MacroSpec("Nameref", "{"),
            MacroSpec("title", "[{"),  # \title[running head]{full title}
            MacroSpec("markboth", "{{"),
            MacroSpec("markright", "{"),
            MacroSpec("runninghead", "[{"),
            MacroSpec("shorttitle", "{"),
            *(MacroSpec(m, "[{") for m in (
                "gls", "Gls", "glspl", "Glspl",
                "acrshort", "Acrshort", "acrshortpl", "Acrshortpl",
                "acrlong", "Acrlong", "acrlongpl", "Acrlongpl",
                "acrfull", "Acrfull", "acrfullpl", "Acrfullpl",
                "glsentryshort", "glsentrylong",
            )),
            *(MacroSpec(m, "{") for m in (  # acronym package references
                "ac", "Ac", "acp", "Acp", "acs", "Acs", "acsp", "Acsp",
                "acl", "Acl", "aclp", "Aclp", "acf", "Acf", "acfp", "Acfp",
            )),
            MacroSpec("acro", "{[{"),
            MacroSpec("acrodef", "{[{"),
            *(MacroSpec(m, "[") for m in _PRINTGLOSSARY_MACROS),
            MacroSpec("pagestyle", "{"),
            MacroSpec("thispagestyle", "{"),
            MacroSpec("settopmatter", "{"),
            MacroSpec("vspace", "*{"),
            MacroSpec("hspace", "*{"),
            MacroSpec("subfloat", "[{"),
            MacroSpec("subfigure", "[{"),
        ],
        environments=[*extra_environments] + [
            EnvironmentSpec(name, "[")
            for name in (*_THEOREM_ENVS, "proof", *extra_theorem_envs)
        ] + [EnvironmentSpec("subfigure", "[{")] + [EnvironmentSpec("minipage", "[{")] + [
            EnvironmentSpec(name, "[")
            for name in ("algorithmic", "algorithmicx", "algpseudocode", "algpseudocodex")
        ] + [
            # tabularx/tabulary carry a leading {width}; supertabular/xtabular take
            # just the {colspec}. Declaring the args keeps them out of the body.
            EnvironmentSpec("tabularx", "{{"), EnvironmentSpec("tabulary", "{{"),
            EnvironmentSpec("supertabular", "{"), EnvironmentSpec("xtabular", "{"),
            EnvironmentSpec("mpsupertabular", "{"),
            # wrapfig: \begin{wrapfigure}[lines]{placement}{width} -- consume the
            # placement/width args so they don't leak into the float body.
            EnvironmentSpec("wrapfigure", "[{{"), EnvironmentSpec("wraptable", "[{{"),
            # boxed environments with an optional [options] argument
            EnvironmentSpec("mdframed", "["), EnvironmentSpec("tcolorbox", "["),
            EnvironmentSpec("leftbar", "["),
        ],
        prepend=True,
    )
    return ctx


def _split_document(source: str) -> tuple[str, str]:
    """Return (body, preamble).

    Only the content between ``\\begin{document}`` and ``\\end{document}`` is
    parsed: preamble macro/environment definitions (``\\newenvironment``,
    ``\\AtBeginDocument``, ...) routinely break a static parser's brace and
    \\begin/\\end matching. If there is no ``\\begin{document}`` the whole input
    is treated as body (used for fragments and tests).
    """
    begin = source.find(r"\begin{document}")
    if begin == -1:
        return source, ""
    preamble = source[:begin]
    body_start = begin + len(r"\begin{document}")
    end = source.rfind(r"\end{document}")
    body = source[body_start : end if end != -1 else len(source)]
    return body, preamble


def _running_head_from_preamble(preamble: str) -> str | None:
    """Recover a running head declared in the preamble (best effort, plain text)."""
    m = re.search(r"\\title\s*\[([^\]]*)\]", preamble)  # \title[short]{long}
    if m:
        return _normalize_ws(m.group(1)).strip() or None
    for macro in ("runninghead", "shorttitle", "markright"):
        content = _braced_content(preamble, macro)
        if content:
            return _normalize_ws(content).strip() or None
    mb = re.search(r"\\markboth\s*\{[^{}]*\}\s*\{([^{}]*)\}", preamble)  # \markboth{l}{r}
    if mb:
        return _normalize_ws(mb.group(1)).strip() or None
    return None


def _braced_content(source: str, name: str) -> str | None:
    """Extract the first ``\\name{...}`` argument from ``source`` (brace-aware).

    An optional ``[...]`` before the mandatory group (e.g. ``\\title[short]{…}``)
    is skipped so the mandatory argument is still found.
    """
    m = re.search(r"\\" + name + r"\s*(?:\[[^\]]*\])?\s*\{", source)
    if not m:
        return None
    depth = 0
    start = m.end() - 1
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : i]
    return None


_DEFINECOLOR_RE = re.compile(
    r"\\definecolor\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}"
)
_COLORLET_RE = re.compile(r"\\colorlet\s*\{([^}]*)\}\s*\{([^}]*)\}")


def _collect_color_defs(source: str, table: ColorTable) -> None:
    for m in _DEFINECOLOR_RE.finditer(source):
        table.define(m.group(1), m.group(2), m.group(3))
    for m in _COLORLET_RE.finditer(source):
        table.define_alias(m.group(1), m.group(2))


# glossaries acronym references; capitalised (\Gls) and plural (\glspl) variants
# are matched case-insensitively / by the "pl" suffix in _acronym_text.
_GLS_MACROS = {
    "gls", "glspl", "acrshort", "acrshortpl", "acrlong", "acrlongpl",
    "acrfull", "acrfullpl", "glsentryshort", "glsentrylong",
}
# acronym package: \ac/\acs/\acl/\acf (+ plural -p, + capitalised) map onto the
# glossaries expansion in _acronym_text.
# glossary/acronym list output -> a description list of the collected entries.
_PRINTGLOSSARY_MACROS = {
    "printglossaries", "printglossary", "printnoidxglossaries", "printnoidxglossary",
    "printacronyms", "printabbreviations",
}
_AC_TO_GLS = {
    "ac": "gls", "acp": "glspl",
    "acs": "acrshort", "acsp": "acrshortpl",
    "acl": "acrlong", "aclp": "acrlongpl",
    "acf": "acrfull", "acfp": "acrfullpl",
}
_NEWACRONYM_RE = re.compile(
    r"\\newacronym\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}"
)
# acronym package: \acro{KEY}[short]{long} (or \acrodef{KEY}[short]{long});
# the optional [short] overrides the displayed abbreviation (default: the key).
_ACRO_RE = re.compile(
    r"\\acro(?:def)?\s*\{([^}]*)\}\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}"
)


def _collect_acronyms(source: str, acronyms: dict[str, tuple[str, str]]) -> None:
    for m in _NEWACRONYM_RE.finditer(source):
        acronyms[m.group(1).strip()] = (m.group(2).strip(), m.group(3).strip())
    for m in _ACRO_RE.finditer(source):  # acronym package \acro/\acrodef
        key = m.group(1).strip()
        short = (m.group(2) or key).strip()
        acronyms.setdefault(key, (short, m.group(3).strip()))


_NEWGLOSSARYENTRY_RE = re.compile(r"\\newglossaryentry\s*\{([^}]*)\}\s*\{")
# name={...} | name=word inside a \newglossaryentry option list
_GLS_NAME_RE = re.compile(r"\bname\s*=\s*(?:\{([^{}]*)\}|([^,}]+))")


def _collect_glossary_entries(source: str, glossary: dict[str, str]) -> None:
    """Register \\newglossaryentry{key}{name=…, description=…} term names.

    Only the ``name`` (the display text \\gls prints) is needed; the option
    list is brace-balanced so a ``description={…}`` with commas is skipped over.
    """
    for m in _NEWGLOSSARYENTRY_RE.finditer(source):
        key = m.group(1).strip()
        i = m.end()  # just past the opening "{" of the options group
        depth, n = 1, len(source)
        j = i
        while j < n and depth:
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
            j += 1
        opts = source[i : j - 1]
        nm = _GLS_NAME_RE.search(opts)
        if nm:
            glossary[key] = (nm.group(1) or nm.group(2) or "").strip()


_BOOK_CLASS_RE = re.compile(
    r"\\documentclass(?:\[[^\]]*\])?\{(book|report|memoir|scrbook|scrreprt)\}"
)
_DOCUMENT_CLASS_RE = re.compile(
    r"\\documentclass(?:\[[^\]]*\])?\{([^}]*)\}"
)


def _is_book_class(source: str) -> bool:
    """True for a book/report-style document (top sectioning level is \\chapter)."""
    return bool(_BOOK_CLASS_RE.search(source)) or bool(re.search(r"\\chapter\b", source))


def _document_class(source: str) -> str | None:
    """Return the declared class name, if one is present."""
    match = _DOCUMENT_CLASS_RE.search(source)
    return match.group(1).strip() if match else None


_CTEX_RE = re.compile(
    r"\\documentclass(?:\[[^\]]*\])?\{ctex(?:art|rep|book)\}"
    r"|\\usepackage(?:\[[^\]]*\])?\{ctex\}"
)


def _is_ctex_document(source: str) -> bool:
    """True when a ctex class or the ctex package controls paragraph conventions."""
    return bool(_CTEX_RE.search(source))


_DOCCLASS_OPTS_RE = re.compile(r"\\documentclass\s*\[([^\]]*)\]")
# \begin{multicols}{N} / \begin{multicols*}{N} (multicol package)
_MULTICOLS_RE = re.compile(r"\\begin\{multicols\*?\}\s*\{\s*(\d+)\s*\}")


def _detect_columns(source: str) -> int:
    """Body column count from the class options / `\\twocolumn` / `multicols`.

    ``\\documentclass[twocolumn]`` (or a later ``\\twocolumn``) -> 2; a
    ``multicols`` environment's ``{N}`` wins if larger. ``\\onecolumn`` after a
    ``\\twocolumn`` is not modelled -- the max column count seen is used.
    """
    cols = 1
    m = _DOCCLASS_OPTS_RE.search(source)
    if m and "twocolumn" in [o.strip() for o in m.group(1).split(",")]:
        cols = 2
    if re.search(r"\\twocolumn\b", source):
        cols = max(cols, 2)
    for mc in _MULTICOLS_RE.finditer(source):
        cols = max(cols, int(mc.group(1)))
    return cols


def parse_document(
    source: str,
    base_dir: str = ".",
    csl_path: str | None = None,
    plugins: PluginRefs | None = None,
) -> tuple[ir.Document, ConversionReport]:
    """Parse LaTeX ``source`` into an IR :class:`~tex2word.ir.Document`.

    ``csl_path`` is an optional ``.csl`` style; when set (and ``citeproc-py`` is
    installed) citations and the reference list are formatted by the real CSL
    engine instead of the built-in heuristic. ``plugins`` may contain Python
    module names, ``.py`` paths, or register callables.
    """
    report = ConversionReport()
    plugin_registry = load_plugins(plugins, base_dir=base_dir)
    directive_source = flatten_inputs(strip_comments(source), base_dir)
    processed = preprocess(source, base_dir)
    for transform in plugin_registry.source_preprocessors:
        processed = transform(processed, base_dir, report)
    expanded = replace_inline_tikz(expand_macros(processed, base_dir))
    body, preamble = _split_document(expanded)
    # \newtheorem declarations may live in a \usepackage'd local .sty (e.g. a
    # paper's MyPreamble.sty), which macro expansion harvests but doesn't inline;
    # scan those sources too so the theorem environments are recognised.
    theorem_src = expanded + "\n" + local_package_sources(directive_source, base_dir)
    custom_theorems, unnumbered_theorems, shared_counters = _collect_newtheorems(theorem_src)
    custom_floats = _collect_declared_floats(theorem_src)
    ctx = _build_context(
        tuple(custom_theorems),
        extra_macros=tuple(plugin_registry.macro_specs),
        extra_environments=(
            *tuple(plugin_registry.environment_specs),
            *(EnvironmentSpec(name, "[") for name in custom_floats),
        ),
    )
    walker = LatexWalker(body, latex_context=ctx, tolerant_parsing=True)
    nodes, _, _ = walker.get_latex_nodes()

    builder = _Builder(report, ctx)
    builder.theorem_envs.update(custom_theorems)
    builder.unnumbered_theorems = unnumbered_theorems
    builder.theorem_counters = _resolve_theorem_counters(custom_theorems, shared_counters)
    builder.box_envs = _collect_tcolorbox_envs(theorem_src)  # \newtcolorbox callouts
    builder.custom_floats.update(custom_floats)
    builder.book_mode = _is_book_class(expanded)
    builder.meta.document_class = _document_class(expanded)
    builder.meta.ctex = _is_ctex_document(expanded)
    _collect_color_defs(expanded, builder.colors)  # \definecolor/\colorlet (preamble + body)
    _collect_acronyms(expanded, builder.acronyms)   # \newacronym (preamble + body)
    _collect_glossary_entries(expanded, builder.glossary)  # \newglossaryentry terms
    _collect_bib_headings(preamble, builder, ctx)  # biblatex \defbibheading (preamble)
    # \texwordstyle{noindent}{name}: pre-scanned so \noindent paragraphs can adopt
    # the bound style while blocks are built (style_overrides are detected later).
    builder.noindent_style = _detect_noindent_style(directive_source)
    blocks = builder.blocks(nodes)
    doc = ir.Document(blocks=blocks, meta=builder.meta, book=builder.book_mode)
    doc.meta.custom_floats.update(custom_floats)

    _fill_meta_from_preamble(doc, preamble, ctx, report)
    _collect_bib_resources(preamble, builder)  # biblatex \addbibresource (preamble)
    _detect_bib_style(preamble, builder)        # biblatex/natbib author-year option
    if doc.meta.language is None:
        doc.meta.language = _detect_language(preamble)  # babel/polyglossia -> BCP-47
    _detect_fonts(doc, preamble)  # fontspec/xeCJK \setmainfont / \setCJK*font
    doc.meta.columns = _detect_columns(expanded)  # twocolumn / \twocolumn / multicols
    # Scan the source before macro expansion: a user's
    # \providecommand{\texwordstyle}[2]{} (added so pdflatex ignores it) would
    # otherwise expand the directive away before we see it. Strip comments first
    # so disabled tex2word directives do not still take effect.
    _detect_style_overrides(doc, directive_source)  # \texwordstyle{role}{Word style name}
    _detect_caption_overrides(doc, directive_source)  # \texwordcaption{key}{value}
    _detect_template(doc, directive_source)  # \texwordtemplate{path.docx}
    _resolve_bibliography(doc, builder, base_dir, report, csl_path)
    return doc, report


_BABEL_LANG = {
    "english": "en-US", "american": "en-US", "usenglish": "en-US", "usenglish ": "en-US",
    "british": "en-GB", "ukenglish": "en-GB", "australian": "en-AU", "canadian": "en-CA",
    "ngerman": "de-DE", "german": "de-DE", "austrian": "de-AT", "naustrian": "de-AT",
    "french": "fr-FR", "francais": "fr-FR", "canadien": "fr-CA",
    "spanish": "es-ES", "italian": "it-IT", "portuguese": "pt-PT", "portuges": "pt-PT",
    "brazil": "pt-BR", "brazilian": "pt-BR", "dutch": "nl-NL", "russian": "ru-RU",
    "polish": "pl-PL", "swedish": "sv-SE", "danish": "da-DK", "norsk": "nb-NO",
    "finnish": "fi-FI", "czech": "cs-CZ", "greek": "el-GR", "turkish": "tr-TR",
    "japanese": "ja-JP", "chinese": "zh-CN",
}


def _detect_fonts(doc: ir.Document, preamble: str) -> None:
    """Pick up XeLaTeX/fontspec + xeCJK font choices from the preamble.

    ``\\setmainfont{Times New Roman}`` -> the Latin (ascii/hAnsi) default;
    ``\\setCJKmainfont{SimSun}`` -> the East-Asian (eastAsia) default so Word
    renders CJK text in that font; ``\\setCJKsansfont`` -> headings; and
    ``\\setCJKmonofont`` -> code. The optional ``[options]`` form is tolerated.
    The name is stored verbatim (it must match a font installed on the machine
    that opens the .docx)."""

    def _font(cmd: str) -> str | None:
        m = re.search(r"\\" + cmd + r"\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", preamble)
        return m.group(1).strip() if m and m.group(1).strip() else None

    main = _font("setmainfont")
    cjk_main = _font("setCJKmainfont")
    cjk_sans = _font("setCJKsansfont")
    cjk_mono = _font("setCJKmonofont")
    if main:
        doc.meta.main_font = main
    if cjk_main:
        doc.meta.cjk_main_font = cjk_main
    if cjk_sans:
        doc.meta.cjk_sans_font = cjk_sans
    if cjk_mono:
        doc.meta.cjk_mono_font = cjk_mono


#: logical roles a \texwordstyle directive may bind to a Word style.
_STYLE_OVERRIDE_ROLES = {
    "appendix1", "appendix2", "appendix3", "appendix4", "part",
    "itemize", "enumerate", "listbullet", "listnumber",
    "figure",   # the paragraph that holds an inserted image
    "caption",  # default for every caption when no per-type role is set
    "figurecaption", "tablecaption", "subfigurecaption", "algorithmcaption",
    # generic paragraph styles: bind, else auto-discover by name, else built-in
    "title", "subtitle", "abstract", "sourcecode", "quote", "bibliography",
    "footnote",
    # unnumbered sectioning commands (\section*, \chapter*, ...): let them use
    # a template paragraph style instead of the built-in Heading 1-5 style.
    "heading1*", "heading2*", "heading3*", "heading4*", "heading5*",
    "chapter*", "section*", "subsection*", "subsubsection*", "paragraph*",
    "subparagraph*",
    "body",           # paragraph style for ordinary body-text (正文) paragraphs
    "noindent",       # explicit/automatic unindented body paragraphs
    "table",          # paragraph style for text inside table cells (default Normal)
    "threelinetable", # Word *table* style applied to a 三线表 (first cmd is \toprule)
}
_STYLE_OVERRIDE_RE = re.compile(
    r"\\texwordstyle\s*\{([^}]*)\}\s*\{([^}]*)\}"
)
#: \texwordcaption keys: per-kind label words, SEQ counter names + wording knobs
#: (see caption_config).
_CAPTION_OVERRIDE_KEYS = {
    "figurelabel", "tablelabel", "equationlabel", "algorithmlabel",
    "figureseq", "tableseq", "equationseq", "algorithmseq",
    "labelstyle", "identifierstyle",
    "figurelabelstyle", "tablelabelstyle", "algorithmlabelstyle",
    "figureidentifierstyle", "tableidentifierstyle",
    "algorithmidentifierstyle",
    "labelsep", "sectionsep", "delim", "eqopen", "eqclose",
}
_CAPTION_OVERRIDE_RE = re.compile(
    r"\\texwordcaption\s*\{([^}]*)\}\s*\{([^}]*)\}"
)
#: \texwordtemplate[mode]{path.docx}: in-source Word reference template path,
#: with optional comma/space-separated modes (``keep`` selects content-injection
#: mode; ``style-numbering`` trusts template paragraph styles for numbering).
_TEMPLATE_RE = re.compile(
    r"\\texwordtemplate\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}"
)
#: optional-argument keywords that select "keep the template's content" mode.
_TEMPLATE_KEEP_KEYWORDS = {"keep", "keepcontent", "content", "preserve"}
_TEMPLATE_STYLE_NUMBERING_KEYWORDS = {
    "stylenumbering", "style-numbering", "style_numbering",
    "stylesnumbering", "styles-numbering", "style", "styles",
}


def _detect_style_overrides(doc: ir.Document, source: str) -> None:
    """Pick up ``\\texwordstyle{role}{Word style name}`` bindings from the source.

    Binds a logical role to a paragraph style *name* in the ``--reference-doc``
    template: ``appendix1``..``appendix4`` (appendix heading levels) and ``part``
    drive the heading style + its linked numbering; ``figure`` styles the image
    line; ``caption`` is the default caption style, overridable per type by
    ``figurecaption`` / ``tablecaption`` / ``subfigurecaption`` /
    ``algorithmcaption``; the generic paragraph roles (``title``, ``subtitle``,
    ``abstract``, ``sourcecode``, ``quote``, ``bibliography``, ``footnote``) restyle
    those paragraphs. Starred heading roles (``section*`` / ``subsection*`` /
    ``chapter*`` or the level-based ``heading1*``..``heading5*``) restyle only
    unnumbered sectioning commands, leaving numbered headings on the built-in
    navigation styles. ``body`` sets the paragraph style of ordinary body-text (正文)
    paragraphs (default ``Normal``), so 正文 can be an indented ``normal-indent``-style
    rather than plain ``Normal``. ``noindent`` styles explicit ``\\noindent``
    paragraphs and, with a reference document and a standard non-ctex class, the
    opening paragraph and first paragraph after a heading. ``table`` sets the
    paragraph style of the text inside every
    table cell (default ``Normal``); ``threelinetable`` names a Word *table* style
    applied to tables whose first command is ``\\toprule`` (booktabs 三线表), so the
    template's three-line border format takes effect. The pipeline resolves each name
    to the template's styleId and
    applies it; an unbound generic role is auto-discovered by name in the template.
    Unknown roles are ignored.
    """
    for m in _STYLE_OVERRIDE_RE.finditer(source):
        role = m.group(1).strip().lower()
        name = m.group(2).strip()
        if role in _STYLE_OVERRIDE_ROLES and name:
            doc.meta.style_overrides[role] = name


def _detect_noindent_style(source: str) -> str | None:
    """The Word style ``\\noindent`` adopts, from ``\\texwordstyle{noindent}{name}``.

    The global counterpart of the per-paragraph ``\\texwordparstyle{name}``: every
    paragraph introduced by ``\\noindent`` takes the named reference-doc style. The
    last binding wins; with no binding ``\\noindent`` is dropped as usual. The name
    is resolved to a styleId at write time (an unknown name warns and falls back).
    """
    name: str | None = None
    for m in _STYLE_OVERRIDE_RE.finditer(source):
        if m.group(1).strip().lower() == "noindent" and m.group(2).strip():
            name = m.group(2).strip()
    return name


def _detect_caption_overrides(doc: ir.Document, source: str) -> None:
    """Pick up ``\\texwordcaption{key}{value}`` caption/cross-ref wording overrides.

    Keys: ``figurelabel``/``tablelabel``/``equationlabel``/``algorithmlabel`` set
    the displayed label word; ``labelstyle``/``identifierstyle`` and their
    per-kind forms (e.g. ``figurelabelstyle``) name a Word character style for
    the displayed caption identifier; ``labelsep`` the gap between label and number;
    ``sectionsep`` the chapter/number separator (e.g. ``-`` for "1-1");
    ``delim`` the text before the caption; ``eqopen``/``eqclose`` the equation
    parentheses. The value is kept verbatim (spaces are significant), so e.g.
    ``\\texwordcaption{delim}{ - }`` keeps the surrounding spaces. Unknown keys
    are ignored.
    """
    custom_keys = _custom_caption_override_keys(doc.meta.custom_floats)
    for m in _CAPTION_OVERRIDE_RE.finditer(source):
        key = m.group(1).strip().lower()
        if key in _CAPTION_OVERRIDE_KEYS or key in custom_keys:
            doc.meta.caption_overrides[key] = m.group(2)


def _caption_key_prefix(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _custom_caption_override_keys(custom_floats: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    for env in custom_floats:
        prefix = _caption_key_prefix(env)
        if not prefix:
            continue
        keys.update({
            f"{prefix}label",
            f"{prefix}seq",
            f"{prefix}labelstyle",
            f"{prefix}identifierstyle",
        })
    return keys


def _detect_template(doc: ir.Document, source: str) -> None:
    """Pick up a ``\\texwordtemplate[mode]{path.docx}`` reference-template directive.

    Names the Word ``.docx`` whose styles, theme and page geometry the output
    adopts -- the in-source equivalent of ``--reference-doc``. A relative path is
    resolved against the ``.tex`` file's directory. The CLI ``--reference-doc``
    option takes priority when both are given. The last directive wins.

    The optional ``[mode]`` argument selects how the template is used: ``keep``
    (also ``preserve`` / ``content``) keeps the template's own content and
    splices the converted body at its ``tex2word_section`` bookmark; ``style-
    numbering`` copies the template numbering unchanged and lets paragraph
    styles (Heading/List Bullet/List Number/appendix/part) carry numbering.
    """
    for m in _TEMPLATE_RE.finditer(source):
        path = m.group(2).strip()
        if path:
            doc.meta.template_doc = path
            mode = (m.group(1) or "").strip().lower()
            modes = {t for t in re.split(r"[\s,;]+", mode) if t}
            doc.meta.template_keep_content = bool(modes & _TEMPLATE_KEEP_KEYWORDS)
            doc.meta.template_style_numbering = bool(
                modes & _TEMPLATE_STYLE_NUMBERING_KEYWORDS
            )


def _detect_language(preamble: str) -> str | None:
    """The main document language as a BCP-47 code from babel/polyglossia, or None."""
    # polyglossia: \setmainlanguage{...} / \setdefaultlanguage{...}
    m = re.search(r"\\set(?:main|default)language\{([^}]*)\}", preamble)
    if m and m.group(1).strip().lower() in _BABEL_LANG:
        return _BABEL_LANG[m.group(1).strip().lower()]
    # babel: \usepackage[...,lang]{babel} (main=lang wins, else the last known one)
    m = re.search(r"\\usepackage\[([^\]]*)\]\{babel\}", preamble)
    if m:
        opts = [o.strip().lower() for o in m.group(1).split(",")]
        main = next((o.split("=", 1)[1] for o in opts if o.startswith("main=")), None)
        if main and main in _BABEL_LANG:
            return _BABEL_LANG[main]
        known = [o for o in opts if o in _BABEL_LANG]
        if known:
            return _BABEL_LANG[known[-1]]  # babel's main language is the last option
    # \documentclass[...,lang,...]{...}
    m = re.search(r"\\documentclass\[([^\]]*)\]", preamble)
    if m:
        known = [o.strip().lower() for o in m.group(1).split(",")
                 if o.strip().lower() in _BABEL_LANG]
        if known:
            return _BABEL_LANG[known[-1]]
    # ctex: \documentclass{ctexart|ctexrep|ctexbook} or \usepackage{ctex} sets up
    # Chinese typesetting (CJK fonts, captions) without a babel/polyglossia option,
    # so treat it as the zh-CN main language.
    if re.search(r"\\documentclass(?:\[[^\]]*\])?\{ctex(?:art|rep|book)\}", preamble) \
            or re.search(r"\\usepackage(?:\[[^\]]*\])?\{ctex\}", preamble):
        return "zh-CN"
    return None


def _detect_bib_style(preamble: str, builder: _Builder) -> None:
    """Pick up the citation style from the biblatex/natbib package options when no
    ``\\bibliographystyle`` set it (so author-year biblatex/natbib renders right)."""
    if builder.bibstyle_set:
        return
    _AY = ("authoryear", "apa", "chicago", "harvard", "mla", "author-year")
    m = re.search(r"\\usepackage\[([^\]]*)\]\{biblatex\}", preamble)
    if m:
        opt = re.search(r"\bstyle\s*=\s*([A-Za-z0-9\-]+)", m.group(1))
        if opt:
            s = opt.group(1).lower()
            if any(a in s for a in _AY):
                builder.bib_style = "author-year"
            elif "numeric" in s or "ieee" in s:
                builder.bib_style = "numeric"
    nb = re.search(r"\\usepackage\[([^\]]*)\]\{natbib\}", preamble)
    if nb:
        nopt = nb.group(1).lower()
        if re.search(r"\bnumbers\b", nopt):
            builder.bib_style = "numeric"
        elif re.search(r"\bauthoryear\b", nopt):
            builder.bib_style = "author-year"


def _collect_bib_resources(preamble: str, builder: _Builder) -> None:
    """Pick up biblatex ``\\addbibresource{file.bib}`` declared in the preamble."""
    for m in re.finditer(r"\\addbibresource(?:\[[^\]]*\])?\{([^}]*)\}", preamble):
        for name in m.group(1).split(","):
            if name.strip() and name.strip() not in builder.bib_files:
                builder.bib_files.append(name.strip())


def _collect_bib_headings(preamble: str, builder: _Builder, ctx) -> None:
    """Pick up biblatex ``\\defbibheading`` definitions declared in the preamble."""
    if r"\defbibheading" not in preamble:
        return
    try:
        nodes, _, _ = LatexWalker(
            preamble, latex_context=ctx, tolerant_parsing=True
        ).get_latex_nodes()
    except Exception:
        return
    for node in nodes:
        if isinstance(node, LatexMacroNode) and node.macroname == "defbibheading":
            builder._defbibheading(node)


def _fill_meta_from_preamble(
    doc: ir.Document, preamble: str, ctx, report: ConversionReport
) -> None:
    """Recover \\title/\\author/\\date/\\keywords defined in the preamble (best effort)."""
    if doc.meta.running_head is None:
        doc.meta.running_head = _running_head_from_preamble(preamble)
    for macro in ("title", "author", "date", "keywords", "institute", "affiliation"):
        if macro == "title" and doc.meta.title is not None:
            continue
        if macro == "author" and doc.meta.authors:
            continue
        if macro == "date" and doc.meta.date is not None:
            continue
        if macro == "keywords" and doc.meta.keywords is not None:
            continue
        if macro in ("institute", "affiliation") and doc.meta.affiliations:
            continue
        content = _braced_content(preamble, macro)
        if content is None:
            continue
        try:
            sub_nodes, _, _ = LatexWalker(
                content, latex_context=ctx, tolerant_parsing=True
            ).get_latex_nodes()
            if macro in ("author", "institute", "affiliation"):
                target = doc.meta.authors if macro == "author" else doc.meta.affiliations
                for seg in _split_on_and(sub_nodes):
                    inl = _Builder(report).inlines(seg)
                    if inl:
                        target.append(inl)
                continue
            inlines = _Builder(report).inlines(sub_nodes)
        except Exception:
            inlines = [ir.Text(content)]
        if macro == "title":
            doc.meta.title = inlines
        elif macro == "date":
            doc.meta.date = inlines
        elif macro == "keywords":
            doc.meta.keywords = inlines


def _resolve_bibliography(
    doc: ir.Document, builder: _Builder, base_dir: str, report: ConversionReport,
    csl_path: str | None = None,
) -> None:
    from ..bib.bibtex import parse_bibtex
    from ..bib.render import resolve_citations

    style = builder.bib_style
    nocite = builder.nocite_keys

    # A .bbl (BibTeX's formatted .bst output) is authoritative -- prefer it over
    # the heuristic .bib->CSL rendering when present.
    bbl_items = _load_bbl(base_dir) if builder.bib_files else {}
    if bbl_items:
        from ..bib.bbl import bbl_style

        bbl_items.update(builder.thebib_items)
        resolve_citations(doc, bbl_items, bbl_style(bbl_items), report,
                          csl_path=csl_path, nocite_keys=nocite)
        report.info("\\bibliography", "used the .bbl (formatted .bst output)")
        return

    items: dict[str, ir.CSLItem] = {}
    for name in builder.bib_files:
        candidates = [name, name + ".bib"] if not name.endswith(".bib") else [name]
        for cand in candidates:
            path = os.path.join(base_dir, cand)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    items.update(parse_bibtex(fh.read()))
                break
        else:
            report.warn("\\bibliography", f"bibliography file not found: {name}")
    items.update(builder.thebib_items)

    if items or builder.bib_files or builder.thebib_items or nocite:
        resolve_citations(doc, items, style, report,
                          csl_path=csl_path, nocite_keys=nocite)


def _load_bbl(base_dir: str) -> dict[str, ir.CSLItem]:
    """Find and parse a single ``.bbl`` in ``base_dir`` (if exactly one)."""
    import glob

    from ..bib.bbl import parse_bbl

    bbls = glob.glob(os.path.join(base_dir, "*.bbl"))
    if len(bbls) != 1:
        return {}
    try:
        with open(bbls[0], encoding="utf-8") as fh:
            return parse_bbl(fh.read())
    except OSError:
        return {}
