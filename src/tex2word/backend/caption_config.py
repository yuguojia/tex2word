"""Localisable caption / cross-reference wording.

The SEQ counter *name* ("Figure"/"Table"/"Equation"/"Algorithm") is an English
field identifier that must stay stable and identical between a caption and its
``\\ref`` -- so it is never localised. What *is* localisable is the wording wrapped
around the live number: the displayed label word, the chapter/number separator
(only emitted with ``--number-by-section``), the delimiter before the caption
text, and the cleveref-style cross-reference prefixes.

A :class:`CaptionConfig` is built once per document from a locale preset
(English or Chinese), chosen by ``--caption-locale`` (``auto`` keys off the
document language or the presence of a CJK font), then overlaid with any
``\\texwordcaption{key}{value}`` directives from the source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: cleveref-style (abbrev, full) prefixes per target kind -- English default.
_EN_REF_NAMES: dict[str, tuple[str, str]] = {
    "figure": ("fig. ", "Figure "),
    "table": ("tab. ", "Table "),
    "section": ("sec. ", "Section "),
    "theorem": ("thm. ", "Theorem "),
    "algorithm": ("alg. ", "Algorithm "),
    "equation": ("eq. ", "Equation "),
}

#: Chinese equivalents. Section keeps no prefix (our REF model can't add the
#: trailing "节" suffix "第N节" needs), so a section ref renders as a bare number.
_ZH_REF_NAMES: dict[str, tuple[str, str]] = {
    "figure": ("图", "图"),
    "table": ("表", "表"),
    "section": ("", ""),
    "theorem": ("定理", "定理"),
    "algorithm": ("算法", "算法"),
    "equation": ("式", "式"),
}

#: \texwordcaption keys that set a per-kind label word.
_LABEL_KEYS = {
    "figurelabel": "Figure",
    "tablelabel": "Table",
    "equationlabel": "Equation",
    "algorithmlabel": "Algorithm",
}


@dataclass(frozen=True)
class CaptionConfig:
    """Wording around a caption/cross-reference number.

    ``labels`` maps a SEQ counter name to its displayed label word. The number
    is rendered as ``label + label_number_sep + N`` (or ``N{section_sep}M`` under
    ``--number-by-section``), then ``delim`` before the caption text. ``eq_wrap``
    are the parentheses around an equation number (in the body and in a ref).
    ``ref_names`` holds the cleveref (abbrev, full) prefixes per target kind.
    """

    labels: dict[str, str]
    ref_names: dict[str, tuple[str, str]]
    label_number_sep: str = " "
    section_sep: str = "."
    delim: str = ": "
    eq_wrap: tuple[str, str] = ("(", ")")

    @classmethod
    def english(cls) -> CaptionConfig:
        return cls(
            labels={"Figure": "Figure", "Table": "Table",
                    "Equation": "Equation", "Algorithm": "Algorithm"},
            ref_names=dict(_EN_REF_NAMES),
        )

    @classmethod
    def chinese(cls) -> CaptionConfig:
        return cls(
            labels={"Figure": "图", "Table": "表",
                    "Equation": "公式", "Algorithm": "算法"},
            ref_names=dict(_ZH_REF_NAMES),
            label_number_sep="",
            section_sep="-",
            delim="　",  # full-width space, e.g. 图1-1　说明文字
            eq_wrap=("(", ")"),  # equation numbers keep half-width parens
        )

    @staticmethod
    def is_chinese_locale(
        locale: str, language: str | None, *, has_cjk_font: bool
    ) -> bool:
        """Whether ``--caption-locale`` resolves to the Chinese preset.

        ``auto`` selects Chinese when the document language is ``zh-CN`` *or* a
        CJK font was set in the preamble (``\\setCJKmainfont`` etc.), since many
        Chinese sources use ctex/xeCJK without a babel ``chinese`` option. Shared
        with the quote proofing-language logic so both agree on "is Chinese".
        """
        loc = locale.lower()
        if loc in ("en", "en-us", "english"):
            return False
        if loc in ("zh", "zh-cn", "chinese"):
            return True
        return language == "zh-CN" or has_cjk_font  # auto

    @classmethod
    def from_locale(
        cls, locale: str, language: str | None, *, has_cjk_font: bool
    ) -> CaptionConfig:
        """Pick a preset from ``--caption-locale`` (``auto``/``en``/``zh-CN``)."""
        if cls.is_chinese_locale(locale, language, has_cjk_font=has_cjk_font):
            return cls.chinese()
        return cls.english()

    def with_overrides(self, overrides: dict[str, str]) -> CaptionConfig:
        """Overlay ``\\texwordcaption{key}{value}`` directives (see _LABEL_KEYS,
        ``labelsep``/``sectionsep``/``delim``/``eqopen``/``eqclose``).

        Override values are used verbatim (no stripping): leading/trailing spaces
        in a delimiter are significant.
        """
        if not overrides:
            return self
        labels = dict(self.labels)
        kw: dict[str, object] = {}
        eq_open, eq_close = self.eq_wrap
        for key, value in overrides.items():
            if key in _LABEL_KEYS:
                labels[_LABEL_KEYS[key]] = value
            elif key == "labelsep":
                kw["label_number_sep"] = value
            elif key == "sectionsep":
                kw["section_sep"] = value
            elif key == "delim":
                kw["delim"] = value
            elif key == "eqopen":
                eq_open = value
            elif key == "eqclose":
                eq_close = value
        return replace(self, labels=labels, eq_wrap=(eq_open, eq_close), **kw)

    def label(self, counter: str) -> str:
        """Displayed label word for a SEQ ``counter`` (falls back to the name)."""
        return self.labels.get(counter, counter)
