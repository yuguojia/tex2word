"""LaTeX math -> math AST.

A small recursive-descent parser over the (macro-expanded) math token stream.
It models the common + AMS math core. Anything it can't model raises
:class:`MathUnsupported`, which the OMML converter catches to drive the
fallback cascade (and the coverage report), mirroring how ``texmath`` renders
unconvertible math as raw TeX.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import symbols as S


class MathUnsupported(Exception):
    """Raised when a construct cannot be represented in the math AST."""

    def __init__(self, construct: str, detail: str = "") -> None:
        super().__init__(f"unsupported math construct: {construct} {detail}".strip())
        self.construct = construct


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


@dataclass
class Lit:
    """A leaf run of math text with styling hints."""

    text: str
    upright: bool = False
    bold: bool = False
    script: str | None = None  # OMML m:scr: script/double-struck/fraktur/...
    #: True for genuine text mode (\text/\textrm/...) -> OMML m:nor (normal,
    #: non-math text). Upright *math* (\mathrm/\symup/\uppi/functions) instead
    #: uses the math "plain" style (m:sty="p"), keeping math spacing.
    text_mode: bool = False


@dataclass
class Row:
    items: list[MNode] = field(default_factory=list)


@dataclass
class Group:
    row: Row


@dataclass
class Frac:
    num: Row
    den: Row


@dataclass
class Sup:
    base: MNode
    sup: MNode


@dataclass
class Sub:
    base: MNode
    sub: MNode


@dataclass
class SubSup:
    base: MNode
    sub: MNode
    sup: MNode


@dataclass
class Sqrt:
    radicand: Row


@dataclass
class Root:
    index: Row
    radicand: Row


@dataclass
class Nary:
    op: str
    body: MNode | None = None
    sub: MNode | None = None
    sup: MNode | None = None
    limloc: str = "undOvr"  # or "subSup"


@dataclass
class Accent:
    base: Row
    char: str


@dataclass
class Fenced:
    open: str
    close: str
    body: Row


@dataclass
class LimLow:
    """Function/operator with a lower limit, e.g. lim_{x\\to0}."""

    base: MNode
    lim: MNode


@dataclass
class GroupChr:
    """An over/under brace-or-bracket group (\\overbrace, \\underbrace)."""

    body: Row
    char: str
    pos: str  # "top" or "bot"


@dataclass
class Matrix:
    rows: list[list[Row]]
    #: alignment env (align/aligned/cases/…): render column-justified (right|left|…)
    #: so relation signs line up at the ``&``, like the classic ``array{rl}`` look.
    aligned: bool = False


MNode = (
    Lit | Row | Group | Frac | Sup | Sub | SubSup | Sqrt | Root | Nary | Accent
    | Fenced | LimLow | Matrix | GroupChr
)

# Functions whose subscript is rendered as an underscript limit in display.
LIMIT_FUNCS = frozenset({"lim", "limsup", "liminf", "max", "min", "sup", "inf", "gcd", "det"})

# An n-ary operator (\int, \sum, ...) binds the operand that follows it as its
# body (OMML m:e), up to the next relation or +/- sign -- matching how
# ``\int_\gamma f = \sum_k a_k`` reads (integrand ``f`` only) or how a trailing
# ``\sum_k n(k) R(k)`` swallows the whole product. These tables list the chars /
# commands that close the operand so the n-ary stops binding there.
_NARY_BODY_STOP_CHARS = frozenset("=<>+-")
_NARY_BODY_STOP_CMDS = frozenset({
    # relations
    "leq", "le", "geq", "ge", "neq", "ne", "equiv", "approx", "sim", "simeq",
    "cong", "propto", "ll", "gg", "subset", "supset", "subseteq", "supseteq",
    "in", "ni", "notin", "mid", "parallel", "perp", "prec", "succ", "models",
    "preceq", "succeq", "gtrsim", "lesssim", "gtrless", "lessgtr", "asymp",
    "doteq", "triangleq", "coloneqq", "eqqcolon", "approxeq", "sqsubseteq",
    "sqsupseteq", "vdash", "dashv", "lhd", "rhd", "unlhd", "unrhd",
    # +/- like signs acting as binary operators at the operand level
    "pm", "mp",
})

# \big \Big \bigg \Bigg (+ l/r/m variants): manual delimiter sizing -- the size
# is purely visual, so we drop it and let the following delimiter render itself.
_BIG_DELIMS = frozenset(
    f"{size}{suffix}"
    for size in ("big", "Big", "bigg", "Bigg")
    for suffix in ("", "l", "r", "m")
)

# Escaped literals inside math: \{ \} \% \# \& \$ \_ and \| (norm bar).
_CMD_LITERAL = {
    "{": "{", "}": "}", "|": "‖",
    "%": "%", "#": "#", "&": "&", "$": "$", "_": "_",
}

# Old plain-TeX/LaTeX2.09 math font *declarations*: ``{\rm Lik}`` behaves like
# ``\mathrm{Lik}`` -- it styles the rest of the current group. Without these the
# converter raised MathUnsupported on ``\rm`` and dumped the whole block as raw.
# Value is (upright, bold, script).
_MATH_STYLE_DECL: dict[str, tuple[bool, bool, str | None]] = {
    "rm": (True, False, None),
    "bf": (False, True, None),
    "sf": (True, False, None),
    "tt": (True, False, None),
    "sc": (True, False, None),
    "it": (False, False, None),
    "mit": (False, False, None),
    "cal": (False, False, "script"),
}

# mathtools/physics paired-delimiter macros: \abs{x} -> |x| etc. (the starred
# auto-size form is accepted and the star ignored). Only used when the user
# hasn't \newcommand-ed them (the macro expander runs first if they have).
_PAIRED_DELIM = {
    "abs": ("|", "|"), "norm": ("‖", "‖"), "ceil": ("⌈", "⌉"),
    "floor": ("⌊", "⌋"), "round": ("⌊", "⌉"), "set": ("{", "}"),
    "Set": ("{", "}"), "ket": ("|", "⟩"), "bra": ("⟨", "|"),
    "braket": ("⟨", "⟩"), "ip": ("⟨", "⟩"), "inner": ("⟨", "⟩"),
}  # the \abs*/\norm* auto-size form is handled by the star-peek, not extra keys

# Extensible (stretchy) arrows: \xrightarrow[under]{over} etc.
_XARROWS = {
    "xrightarrow": "→", "xleftarrow": "←",
    "xRightarrow": "⇒", "xLeftarrow": "⇐",
    "xleftrightarrow": "↔", "xLeftrightarrow": "⇔",
    "xhookrightarrow": "↪", "xhookleftarrow": "↩",
    "xmapsto": "↦", "xrightharpoonup": "⇀", "xrightleftharpoons": "⇌",
}

_MATRIX_DELIMS = {
    "matrix": ("", ""),
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "Bmatrix": ("{", "}"),
    "vmatrix": ("|", "|"),
    "Vmatrix": ("‖", "‖"),
    "cases": ("{", ""),
    "aligned": ("", ""),
    "array": ("", ""),
    "smallmatrix": ("", ""),
    # amsmath alignment/gather environments: when they appear *wrapped* (inside
    # $$/\[ or \left\{…\right.) the parser meets them here; render as a
    # column-aligned matrix with no surrounding delimiters.
    "align": ("", ""),
    "alignat": ("", ""),
    "flalign": ("", ""),
    "gather": ("", ""),
    "gathered": ("", ""),
    "split": ("", ""),
    "multlined": ("", ""),
    "eqnarray": ("", ""),
}

# Environments whose ``&`` is an *alignment* marker (line up relation signs),
# as opposed to plain matrices where ``&`` separates equal-status columns. These
# render with alternating right/left column justification.
_ALIGNED_ENVS = frozenset(
    {"aligned", "align", "alignat", "flalign", "split", "eqnarray"}
)


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

Token = tuple[str, str | None]


def tokenize(s: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            j = i + 1
            if j < n and not s[j].isalpha():
                # control symbol: \{ \} \, \\ \; \! \| etc.
                if s[j] == "\\":
                    tokens.append(("newline", None))
                    i = j + 1
                else:
                    tokens.append(("cmd", s[j]))
                    i = j + 1
            else:
                k = j
                while k < n and s[k].isalpha():
                    k += 1
                tokens.append(("cmd", s[j:k]))
                i = k
                while i < n and s[i] == " ":
                    i += 1
        elif c in "{}^_&":
            tokens.append((c, None))
            i += 1
        elif c in " \n\t":
            i += 1
        elif c.isdigit():
            k = i
            while k < n and (s[k].isdigit() or s[k] == "."):
                k += 1
            tokens.append(("num", s[i:k]))
            i = k
        else:
            tokens.append(("char", c))
            i += 1
    return tokens


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.i = 0

    def _peek(self) -> Token:
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", None)

    def _next(self) -> Token:
        if self.i >= len(self.toks):
            return ("eof", None)
        t = self.toks[self.i]
        self.i += 1
        return t

    def _expect(self, ttype: str) -> None:
        if self._peek()[0] != ttype:
            raise MathUnsupported("syntax", f"expected {ttype}")
        self._next()

    # -- rows ------------------------------------------------------------- #

    def parse_row(self, stop: frozenset[str] = frozenset()) -> Row:
        items: list[MNode] = []
        while True:
            ttype, _ = self._peek()
            if ttype in ("}", "eof") or ttype in stop:
                break
            item = self._parse_item(stop)
            if item is not None:
                items.append(item)
        return Row(items)

    def _parse_item(self, stop: frozenset[str]) -> MNode | None:
        """Parse one atom with its scripts; an n-ary then binds its operand."""
        atom = self.parse_atom()
        if atom is None:
            return None
        atom = self._maybe_scripts(atom)
        if isinstance(atom, Nary) and atom.body is None:
            atom.body = self._nary_body(stop)
        return atom

    def _nary_body(self, stop: frozenset[str]) -> MNode | None:
        """Collect the operand that an n-ary operator binds (its OMML m:e).

        Consumes following atoms until a relation / +/- sign or a structural
        boundary (group/environment/delimiter close), so ``\\int_a^b f = g``
        keeps only ``f`` as the integrand while ``\\sum_k a_k b_k`` takes the
        whole product."""
        items: list[MNode] = []
        while True:
            ttype, tval = self._peek()
            if ttype in ("}", "eof", "newline", "&") or ttype in stop:
                break
            if ttype == "char" and tval in _NARY_BODY_STOP_CHARS:
                break
            if ttype == "cmd" and (tval in _NARY_BODY_STOP_CMDS or tval in ("right", "end")):
                break
            item = self._parse_item(stop)
            if item is not None:
                items.append(item)
        return _flatten(Row(items)) if items else None

    def parse_group(self) -> Row:
        """Parse a braced group or a single atom (used for command arguments)."""
        ttype, _ = self._peek()
        if ttype == "{":
            self._next()
            row = self.parse_row()
            self._expect("}")
            return row
        atom = self.parse_atom()
        return Row([atom] if atom is not None else [])

    def _maybe_scripts(self, base: MNode) -> MNode:
        sub: MNode | None = None
        sup: MNode | None = None
        while True:
            ttype, _ = self._peek()
            if ttype == "^":
                self._next()
                sup = _flatten(self.parse_group())
            elif ttype == "_":
                self._next()
                sub = _flatten(self.parse_group())
            else:
                break

        if isinstance(base, Nary):
            base.sub, base.sup = sub, sup
            return base
        if isinstance(base, Lit) and base.upright and base.text in LIMIT_FUNCS:
            if sub is not None and sup is None:
                return LimLow(base, sub)
        if sub is not None and sup is not None:
            return SubSup(base, sub, sup)
        if sub is not None:
            return Sub(base, sub)
        if sup is not None:
            return Sup(base, sup)
        return base

    # -- atoms ------------------------------------------------------------ #

    def parse_atom(self) -> MNode | None:
        ttype, tval = self._next()
        if ttype == "{":
            row = self.parse_row()
            self._expect("}")
            return Group(row)
        if ttype == "num":
            # digits are upright by default in Word's math zone; tagging them
            # m:nor strips that math spacing/typesetting, so leave them unmarked.
            return Lit(tval or "")
        if ttype == "char":
            return self._char_atom(tval or "")
        if ttype == "cmd":
            return self.parse_command(tval or "")
        if ttype in ("^", "_"):
            # script with no preceding base
            self.i -= 1
            return Lit("")
        if ttype == "newline" or ttype == "&":
            raise MathUnsupported("alignment", "stray & or \\\\")
        return None

    def _char_atom(self, c: str) -> MNode:
        # Both letters (italic) and operators/digits/punctuation (upright) are
        # typeset correctly by Word's math engine on their own. Forcing m:nor on
        # the non-letters turns them into normal text and drops the relational/
        # binary-operator spacing (so ``a=b+c`` rendered cramped), so leave the
        # styling to the math zone -- only explicit \mathrm/\text mark upright.
        return Lit(c)

    def parse_command(self, name: str) -> MNode:  # noqa: C901 - dispatch table
        if name in S.ACCENTS:
            return Accent(self.parse_group(), S.ACCENTS[name])
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            return Frac(self.parse_group(), self.parse_group())
        if name in ("overbrace", "overbracket"):
            return GroupChr(self.parse_group(), "⏞", "top")
        if name in ("underbrace", "underbracket"):
            return GroupChr(self.parse_group(), "⏟", "bot")
        if name == "binom":
            inner = Matrix([[self.parse_group()], [self.parse_group()]])
            return Fenced("(", ")", Row([inner]))
        if name == "sqrt":
            if self._peek()[0] == "char" and self._peek()[1] == "[":
                index = self._parse_optional()
                return Root(index, self.parse_group())
            return Sqrt(self.parse_group())
        if name in S.NARY:
            limloc = "subSup" if name in ("int", "iint", "iiint", "oint") else "undOvr"
            return Nary(S.NARY[name], limloc=limloc)
        if name in _CMD_LITERAL:
            return Lit(_CMD_LITERAL[name], upright=True)
        if name in _BIG_DELIMS:
            # delimiter sizing is visual-only; the next delimiter renders itself
            return Lit("")
        if name in _PAIRED_DELIM:
            if self._peek() == ("char", "*"):  # \abs*{x} auto-size form
                self._next()
            open_d, close_d = _PAIRED_DELIM[name]
            return Fenced(open_d, close_d, self.parse_group())
        if name in ("dv", "odv", "pdv", "dd"):
            return self._parse_derivative(name)
        if name in _XARROWS:
            return self._parse_xarrow(_XARROWS[name])
        if name in ("overset", "stackrel"):
            top = self.parse_group()
            base = self.parse_group()
            return Matrix([[top], [base]])
        if name == "underset":
            bot = self.parse_group()
            base = self.parse_group()
            return Matrix([[base], [bot]])
        if name in ("text", "mbox", "textrm", "textnormal", "textsf", "texttt", "textsc"):
            # genuine text mode -> normal (non-math) upright text, OMML m:nor
            return _styled(self.parse_group(), upright=True, text_mode=True)
        if name in (
            "mathrm", "operatorname", "operatorname*", "mathsf", "mathtt",
            # unicode-math upright alphabets: \symup \symrm \symsf \symtt
            "symup", "symrm", "symsf", "symtt",
        ):
            # upright *math* -> the math "plain" style (m:sty="p"), not m:nor
            return _styled(self.parse_group(), upright=True)
        if name == "textbf":
            return _styled(self.parse_group(), upright=True, bold=True, text_mode=True)
        if name in ("textit", "textsl", "emph"):
            return self.parse_group_as_group()
        if name in ("mathbf", "boldsymbol", "bm", "symbf"):
            return _styled(self.parse_group(), bold=True)
        if name == "symbfup":  # unicode-math: bold upright
            return _styled(self.parse_group(), bold=True, upright=True)
        if name in ("mathbb", "symbb"):
            return _styled(self.parse_group(), script="double-struck", upright=True)
        if name in ("mathcal", "mathscr", "symcal", "symscr"):
            return _styled(self.parse_group(), script="script")
        if name in ("mathfrak", "symfrak"):
            return _styled(self.parse_group(), script="fraktur")
        if name in ("mathit", "symit", "symsfit"):
            return self.parse_group_as_group()
        if name in (
            "mathbin", "mathrel", "mathop", "mathord",
            "mathopen", "mathclose", "mathpunct", "mathinner",
        ):
            # math-class wrappers only affect spacing -> render their content
            return self.parse_group_as_group()
        if name in ("symbfit", "symbfsf"):  # unicode-math: bold italic
            return _styled(self.parse_group(), bold=True)
        if name == "left":
            return self._parse_fenced()
        if name == "right":
            raise MathUnsupported("delimiter", "stray \\right")
        if name == "substack":  # \substack{a \\ b \\ c} -> a single-column stack
            return self._parse_substack()
        if name == "bmod":  # infix modulo: a \bmod n -> "a mod n"
            return Lit(" mod ", upright=True)
        if name == "pmod":  # \pmod{n} -> " (mod n)"
            mod_row = Row([Lit("mod ", upright=True), self.parse_group()])
            return Row([Lit(" ", upright=True), Fenced("(", ")", mod_row)])
        if name in S.FUNCTIONS:
            return Lit(name, upright=True)
        if name in S.MATHBB:  # bare \N style not expected, but safe
            return Lit(S.MATHBB[name], upright=True)
        if name in S.SPACING:
            return Lit(S.SPACING[name], upright=True)
        if name in S.SYMBOLS:
            # Greek letters, relations, binary operators and arrows all render
            # with the correct shape (lowercase Greek italic, the rest upright)
            # and the correct math spacing on their own. m:nor would force them
            # to normal text and lose that spacing, so emit a plain math run.
            return Lit(S.SYMBOLS[name])
        if name in S.UPGREEK:  # upgreek package: \uppi \Upomega ... -> upright
            return Lit(S.UPGREEK[name], upright=True)
        if name == "begin":
            return self._parse_environment()
        if name == "end":
            raise MathUnsupported("environment", "stray \\end")
        if name in ("displaystyle", "textstyle", "scriptstyle", "limits", "nolimits"):
            return Lit("")  # styling hints we currently ignore
        if name in ("nonumber", "notag", "qedhere"):
            return Lit("")  # numbering/QED-placement hints: drop in the body
        if name in ("hline", "toprule", "midrule", "bottomrule", "hdashline"):
            return Lit("")  # array/table rules: no OMML equivalent, drop them
        if name in ("cline", "cmidrule", "noalign"):
            # \cmidrule(lr){2-3}: consume an optional (l/r/lr) trim modifier first.
            if self._peek() == ("char", "("):
                while self._peek()[0] != "eof" and self._next() != ("char", ")"):
                    pass
            if self._peek()[0] == "{":
                self.parse_group()  # consume the {a-b} / {…} argument
            return Lit("")
        if name in _MATH_STYLE_DECL:
            # declaration form {\rm ...}: style the rest of the current group.
            upright, bold, script = _MATH_STYLE_DECL[name]
            return _styled(self.parse_row(), upright=upright, bold=bold, script=script)
        raise MathUnsupported(f"\\{name}")

    def parse_group_as_group(self) -> Group:
        return Group(self.parse_group())

    def _parse_derivative(self, name: str) -> MNode:
        """physics ``\\dv``/``\\pdv``/``\\odv``/``\\dd`` derivatives."""
        d = "∂" if name == "pdv" else "d"
        if name == "dd":  # differential: \dd{x} -> "d x", \dd -> "d"
            lead: list[MNode] = [Lit(d, upright=True)]
            if self._peek()[0] == "{":
                lead.extend(self.parse_group().items)
            return Group(Row(lead))
        first = self.parse_group()
        second = self.parse_group() if self._peek()[0] == "{" else None
        if second is not None:  # \dv{f}{x} -> d f / d x
            num = Row([Lit(d, upright=True), *first.items])
            den = Row([Lit(d, upright=True), *second.items])
        else:  # \dv{x} -> d / d x
            num = Row([Lit(d, upright=True)])
            den = Row([Lit(d, upright=True), *first.items])
        return Frac(num, den)

    def _parse_xarrow(self, arrow: str) -> MNode:
        """Parse ``\\xrightarrow[under]{over}`` into a labelled arrow."""
        under: Row | None = None
        if self._peek() == ("char", "["):
            under = self._parse_optional()
        over = self.parse_group()
        base: MNode = Lit(arrow, upright=True)
        if under is not None and under.items:
            return SubSup(base, _flatten(under), _flatten(over))
        return Sup(base, _flatten(over))

    def _parse_optional(self) -> Row:
        """Parse a ``[...]`` optional argument (used by \\sqrt)."""
        assert self._next() == ("char", "[")
        items: list[MNode] = []
        while True:
            ttype, tval = self._peek()
            if ttype == "char" and tval == "]":
                self._next()
                break
            if ttype == "eof":
                break
            atom = self.parse_atom()
            if atom is not None:
                items.append(self._maybe_scripts(atom))
        return Row(items)

    def _read_delim(self) -> str:
        ttype, tval = self._next()
        if ttype == "char":
            return S.DELIMITERS.get(tval or "", tval or "")
        if ttype == "cmd":
            return S.DELIMITERS.get(tval or "", S.SYMBOLS.get(tval or "", tval or ""))
        raise MathUnsupported("delimiter")

    def _parse_fenced(self) -> Fenced:
        open_delim = self._read_delim()
        items: list[MNode] = []
        while True:
            ttype, tval = self._peek()
            if ttype == "eof":
                raise MathUnsupported("delimiter", "unclosed \\left")
            if ttype == "cmd" and tval == "right":
                self._next()
                close_delim = self._read_delim()
                return Fenced(open_delim, close_delim, Row(items))
            item = self._parse_item(frozenset())
            if item is not None:
                items.append(item)

    def _parse_substack(self) -> MNode:
        """\\substack{a \\\\ b \\\\ c} -> a single-column matrix (stacked limits)."""
        if self._peek()[0] == "{":
            self._next()
        rows: list[list[Row]] = [[]]
        current: list[MNode] = []
        while True:
            ttype, _ = self._peek()
            if ttype in ("}", "eof"):
                break
            if ttype == "newline":
                self._next()
                rows[-1].append(Row(current.copy()))
                current.clear()
                rows.append([])
                continue
            atom = self.parse_atom()
            if atom is not None:
                current.append(self._maybe_scripts(atom))
        rows[-1].append(Row(current.copy()))
        if self._peek()[0] == "}":
            self._next()
        if rows and all(len(c.items) == 0 for c in rows[-1]):
            rows.pop()
        return Matrix(rows)

    def _parse_environment(self) -> MNode:
        name_row = self.parse_group()
        env = _row_text(name_row)
        env = env.rstrip("*")
        if env not in _MATRIX_DELIMS:
            raise MathUnsupported(f"environment {env}")
        if env in ("array", "alignat"):
            # consume the column spec / column-count argument and ignore it
            if self._peek()[0] == "{":
                self.parse_group()
        rows: list[list[Row]] = [[]]
        current: list[MNode] = []

        def flush_cell() -> None:
            rows[-1].append(Row(current.copy()))
            current.clear()

        while True:
            ttype, tval = self._peek()
            if ttype == "eof":
                raise MathUnsupported("environment", f"unclosed {env}")
            if ttype == "cmd" and tval == "end":
                self._next()
                self.parse_group()  # consume {env}
                flush_cell()
                break
            if ttype == "&":
                self._next()
                flush_cell()
                continue
            if ttype == "newline" or (ttype == "cmd" and tval == "cr"):
                # both LaTeX `\\` and plain-TeX `\cr` end a matrix row
                self._next()
                flush_cell()
                rows.append([])
                continue
            atom = self.parse_atom()
            if atom is not None:
                current.append(self._maybe_scripts(atom))

        # drop a trailing empty row (from a final \\)
        if rows and all(len(c.items) == 0 for c in rows[-1]):
            rows.pop()
        matrix = Matrix(rows, aligned=env in _ALIGNED_ENVS)
        open_d, close_d = _MATRIX_DELIMS[env]
        if open_d or close_d:
            return Fenced(open_d, close_d, Row([matrix]))
        return matrix


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _styled(
    row: Row, *, upright: bool = False, bold: bool = False,
    script: str | None = None, text_mode: bool = False,
) -> MNode:
    """Apply styling to every Lit leaf in a parsed group."""
    for item in row.items:
        _apply_style(item, upright=upright, bold=bold, script=script, text_mode=text_mode)
    return Group(row)


def _apply_style(
    node: MNode, *, upright: bool, bold: bool, script: str | None, text_mode: bool
) -> None:
    if isinstance(node, Lit):
        if upright:
            node.upright = True
        if bold:
            node.bold = True
        if script:
            node.script = script
        if text_mode:
            node.text_mode = True
    elif isinstance(node, Group):
        for item in node.row.items:
            _apply_style(item, upright=upright, bold=bold, script=script, text_mode=text_mode)
    elif isinstance(node, Row):
        for item in node.items:
            _apply_style(item, upright=upright, bold=bold, script=script, text_mode=text_mode)


def _flatten(row: Row) -> MNode:
    """A single-item row collapses to its item; otherwise stays a Group."""
    if len(row.items) == 1:
        return row.items[0]
    return Group(row)


def _row_text(row: Row) -> str:
    out: list[str] = []
    for item in row.items:
        if isinstance(item, Lit):
            out.append(item.text)
        elif isinstance(item, Group):
            out.append(_row_text(item.row))
    return "".join(out)


def parse(latex: str) -> Row:
    """Parse a LaTeX math string into a math AST :class:`Row`."""
    parser = _Parser(tokenize(latex))
    row = parser.parse_row()
    if parser._peek()[0] != "eof":
        raise MathUnsupported("syntax", "trailing tokens")
    return row
