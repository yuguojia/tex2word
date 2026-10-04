from tex2word.bib.rich_text import RichTextSpan, parse_rich_text


def test_parse_csl_rich_text_combines_nested_faces():
    spans = parse_rich_text("A <strong><em><sup>B</sup></em></strong> C")

    assert spans == [
        RichTextSpan("A "),
        RichTextSpan("B", ("bold", "italic", "superscript")),
        RichTextSpan(" C"),
    ]


def test_parse_csl_rich_text_keeps_unknown_content_and_decodes_entities():
    spans = parse_rich_text(
        "A &amp; <unknown>B</unknown> "
        "<span style='text-decoration: underline'>C</span>"
    )

    assert spans == [
        RichTextSpan("A & B "),
        RichTextSpan("C", ("underline",)),
    ]
