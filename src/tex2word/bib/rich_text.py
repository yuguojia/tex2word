"""Parse the small HTML-like rich-text vocabulary used by CSL JSON.

CSL processors and reference managers commonly store emphasis and vertical
alignment in string variables with tags such as ``<i>`` and ``<sub>``.  Keep
the parser independent of any output backend so the same spans can drive both
EndNote XML ``<style>`` nodes and Word runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_TAG_FACES: dict[str, tuple[str, ...]] = {
    "i": ("italic",),
    "em": ("italic",),
    "b": ("bold",),
    "strong": ("bold",),
    "sup": ("superscript",),
    "sub": ("subscript",),
    "u": ("underline",),
    # Zotero accepts <sc> but EndNote XML has no matching face.  Its text is
    # retained while the unsupported presentation is deliberately dropped.
    "sc": (),
}


@dataclass(frozen=True)
class RichTextSpan:
    """A text fragment and the EndNote-compatible faces active on it."""

    text: str
    faces: tuple[str, ...] = ()


class _RichTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.spans: list[RichTextSpan] = []
        self._tags: list[tuple[str, tuple[str, ...]]] = []
        self._faces: list[str] = []

    def _emit(self, text: str) -> None:
        if not text:
            return
        faces = tuple(self._faces)
        if self.spans and self.spans[-1].faces == faces:
            previous = self.spans[-1]
            self.spans[-1] = RichTextSpan(previous.text + text, faces)
        else:
            self.spans.append(RichTextSpan(text, faces))

    @staticmethod
    def _format_for(tag: str, attrs: list[tuple[str, str | None]]) -> tuple[str, ...]:
        if tag != "span":
            return _TAG_FACES.get(tag, ())
        style = next((value or "" for name, value in attrs if name.lower() == "style"), "")
        if re.search(r"text-decoration\s*:[^;]*\bunderline\b", style, re.IGNORECASE):
            return ("underline",)
        return ()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self._emit("\n")
            return
        if tag == "p":
            self._paragraph_break()
        requested = self._format_for(tag, attrs)
        added = tuple(face for face in requested if face not in self._faces)
        self._tags.append((tag, added))
        self._faces.extend(added)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self._emit("\n")
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match = next(
            (index for index in range(len(self._tags) - 1, -1, -1)
             if self._tags[index][0] == tag),
            None,
        )
        if match is None:
            return
        for _, added in reversed(self._tags[match:]):
            for face in reversed(added):
                if face in self._faces:
                    self._faces.remove(face)
        del self._tags[match:]
        if tag == "p":
            self._paragraph_break()

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def _paragraph_break(self) -> None:
        if not self.spans:
            return
        current = self.spans[-1].text
        if not current.endswith("\n\n"):
            self._emit("\n" if current.endswith("\n") else "\n\n")


def parse_rich_text(value: object) -> list[RichTextSpan]:
    """Return plain/formatted spans from a CSL rich-text string.

    Unknown tags are discarded while their text is retained.  This mirrors
    Zotero's EndNote XML exporter and prevents literal markup leaking into the
    Word document when a CSL producer uses an unsupported tag.
    """

    parser = _RichTextParser()
    parser.feed(str(value).strip())
    parser.close()

    spans: list[RichTextSpan] = []
    for span in parser.spans:
        text = re.sub(r"\n{3,}", "\n\n", span.text)
        if text:
            spans.append(RichTextSpan(text, span.faces))
    return spans
