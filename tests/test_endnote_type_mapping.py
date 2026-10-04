"""Type-sensitive CSL JSON to EndNote XML mappings."""

from __future__ import annotations

import json

import pytest
from lxml import etree

from tex2word import ir
from tex2word.bib import endnote
from tex2word.bib.csl_json import parse_csl_json


def _record_xml(item_type: str, **fields: object) -> str:
    item = ir.CSLItem(id="example", type=item_type, csl_fields=fields)
    record = endnote._record("example", item)
    return etree.tostring(record, encoding="unicode")


@pytest.mark.parametrize(
    ("item_type", "endnote_name", "endnote_number"),
    [
        ("bill", "Bill", 4),
        ("broadcast", "Film or Broadcast", 21),
        ("dataset", "Dataset", 59),
        ("entry-dictionary", "Dictionary", 52),
        ("entry-encyclopedia", "Encyclopedia", 53),
        ("graphic", "Artwork", 2),
        ("hearing", "Hearing", 14),
        ("interview", "Personal Communication", 26),
        ("patent", "Patent", 25),
        ("post", "Web Page", 12),
        ("post-weblog", "Blog", 56),
        ("speech", "Conference Paper", 47),
        ("standard", "Standard", 58),
        ("classic", "Classical Work", 49),
        ("figure", "Figure", 37),
        ("musical_score", "Music", 61),
        ("periodical", "Serial", 57),
        ("regulation", "Legal Rule or Regulation", 50),
    ],
)
def test_extended_csl_types_use_endnote_reference_types(
    item_type: str, endnote_name: str, endnote_number: int
) -> None:
    xml = _record_xml(item_type, title="Example")
    assert f'<ref-type name="{endnote_name}">{endnote_number}</ref-type>' in xml


@pytest.mark.parametrize(
    "item_type",
    ["collection", "entry", "event", "performance", "review", "review-book", "treaty"],
)
def test_ambiguous_and_unknown_types_fall_back_to_generic(item_type: str) -> None:
    xml = _record_xml(item_type, title="Example")
    assert '<ref-type name="Generic">13</ref-type>' in xml


def test_periodical_titles_include_periodical_names() -> None:
    xml = _record_xml(
        "article-journal",
        title="Article",
        **{"container-title": "Journal", "container-title-short": "J."},
    )
    assert "<secondary-title>Journal</secondary-title>" in xml
    assert "<periodical><full-title>Journal</full-title><abbr-1>J.</abbr-1></periodical>" in xml


def test_chapter_titles_distinguish_book_and_series() -> None:
    xml = _record_xml(
        "chapter",
        title="Chapter",
        **{"container-title": "Book", "collection-title": "Series"},
    )
    assert "<secondary-title>Book</secondary-title>" in xml
    assert "<tertiary-title>Series</tertiary-title>" in xml
    assert "<periodical>" not in xml


def test_book_collection_is_a_secondary_title() -> None:
    xml = _record_xml("book", title="Book", **{"collection-title": "Series"})
    assert "<secondary-title>Series</secondary-title>" in xml
    assert "<tertiary-title>Series</tertiary-title>" not in xml


def test_conference_titles_separate_event_proceedings_and_series() -> None:
    xml = _record_xml(
        "paper-conference",
        title="Paper",
        **{
            "event-title": "Conference",
            "container-title": "Proceedings",
            "collection-title": "Series",
        },
    )
    assert "<secondary-title>Conference</secondary-title>" in xml
    assert "<tertiary-title>Series</tertiary-title>" in xml
    assert "<custom3>Proceedings</custom3>" in xml
    assert "<periodical>" not in xml


def test_legal_title_and_authority_fields_are_type_sensitive() -> None:
    case = _record_xml(
        "legal_case", title="Case", authority="Supreme Court", **{"container-title": "Reporter"}
    )
    assert "<secondary-title>Reporter</secondary-title>" in case
    assert "<publisher>Supreme Court</publisher>" in case

    bill = _record_xml(
        "bill", title="Bill", authority="Legislature", **{"container-title": "Code"}
    )
    assert "<secondary-title>Code</secondary-title>" in bill
    assert "<tertiary-title>Legislature</tertiary-title>" in bill


def test_article_issue_wins_over_generic_number() -> None:
    xml = _record_xml("article-journal", title="Article", issue="7", number="99")
    assert xml.count("<number>") == 1
    assert "<number>7</number>" in xml
    assert ">99<" not in xml


@pytest.mark.parametrize("item_type", ["patent", "report"])
def test_patent_and_report_numbers_use_isbn_field(item_type: str) -> None:
    xml = _record_xml(item_type, title="Example", number="R-42", ISBN="book-id")
    assert xml.count("<isbn>") == 1
    assert "<isbn>R-42</isbn>" in xml
    assert "book-id" not in xml


@pytest.mark.parametrize("item_type", ["bill", "legislation"])
def test_legislative_numbers_use_misc1(item_type: str) -> None:
    xml = _record_xml(item_type, title="Example", number="H.R. 42")
    assert "<misc1>H.R. 42</misc1>" in xml
    assert "<number>" not in xml


def test_additional_direct_fields_and_collision_priorities() -> None:
    xml = _record_xml(
        "software",
        title="Program",
        page="10-20",
        **{
            "number-of-pages": "500",
            "number-of-volumes": "3",
            "section": "API",
            "chapter-number": "2",
            "genre": "Scientific software",
            "event-place": "Taipei",
            "dimensions": "10 MB",
            "version": "2.0",
            "status": "Published",
            "original-publisher": "Original publisher",
            "call-number": "QA76",
            "source": "Library catalog",
        },
    )
    assert "<pages>10-20</pages>" in xml and "500" not in xml
    assert "<num-vols>3</num-vols>" in xml
    assert "<section>API</section>" in xml and ">2<" not in xml
    assert "<work-type>Scientific software</work-type>" in xml
    assert "<meeting-place>Taipei</meeting-place>" in xml
    assert "<size>10 MB</size>" in xml
    assert "<edition>2.0</edition>" in xml
    assert "<reprint-status>Published</reprint-status>" in xml
    assert "<orig-pub>Original publisher</orig-pub>" in xml
    assert "<call-num>QA76</call-num>" in xml
    assert "<remote-database-name>Library catalog</remote-database-name>" in xml


def test_book_series_number_and_chapter_volume_count_are_type_sensitive() -> None:
    book = _record_xml("book", title="Book", **{"collection-number": "S-12"})
    assert "<misc1>S-12</misc1>" in book

    chapter = _record_xml("chapter", title="Chapter", **{"number-of-volumes": "4"})
    assert "<issue>4</issue>" in chapter
    assert "<num-vols>" not in chapter


def test_book_and_chapter_editor_groups_are_type_sensitive() -> None:
    people = {
        "editor": [{"family": "Editor", "given": "Eve"}],
        "collection-editor": [{"family": "Series", "given": "Sam"}],
    }
    book = _record_xml("book", title="Book", **people)
    assert (
        "<secondary-authors><author>Series, Sam</author></secondary-authors>" in book
    )
    assert "<tertiary-authors><author>Editor, Eve</author></tertiary-authors>" in book

    chapter = _record_xml("chapter", title="Chapter", **people)
    assert "<secondary-authors><author>Editor, Eve</author></secondary-authors>" in chapter
    assert "<tertiary-authors><author>Series, Sam</author></tertiary-authors>" in chapter


def test_music_and_broadcast_roles_are_grouped_once_and_in_order() -> None:
    song = _record_xml(
        "song",
        title="Song",
        composer=[{"literal": "Composer"}],
        performer=[{"literal": "Performer"}],
    )
    assert "<authors><author>Composer</author></authors>" in song
    assert "<secondary-authors><author>Performer</author></secondary-authors>" in song

    broadcast = _record_xml(
        "broadcast",
        title="Broadcast",
        director=[{"literal": "Director"}],
        host=[{"literal": "Host"}],
        narrator=[{"literal": "Narrator"}],
        producer=[{"literal": "Producer"}],
        performer=[{"literal": "Performer"}],
    )
    assert broadcast.count("<authors>") == 1
    assert "<authors><author>Director</author></authors>" in broadcast
    assert broadcast.count("<secondary-authors>") == 1
    assert (
        "<secondary-authors><author>Host</author><author>Narrator</author>"
        "</secondary-authors>" in broadcast
    )
    assert "<tertiary-authors><author>Producer</author></tertiary-authors>" in broadcast
    assert "<subsidiary-authors><author>Performer</author></subsidiary-authors>" in broadcast


def test_interview_and_personal_communication_roles() -> None:
    interview = _record_xml(
        "interview",
        title="Interview",
        author=[{"literal": "Interviewee"}],
        interviewer=[{"literal": "Interviewer"}],
    )
    assert "<authors><author>Interviewee</author></authors>" in interview
    assert "<secondary-authors><author>Interviewer</author></secondary-authors>" in interview

    letter = _record_xml(
        "personal_communication",
        title="Letter",
        recipient=[{"literal": "Recipient"}],
    )
    assert "<secondary-authors><author>Recipient</author></secondary-authors>" in letter


def test_default_and_speech_contributor_roles_are_preserved() -> None:
    report = _record_xml(
        "report",
        title="Report",
        **{
            "container-author": [{"literal": "Institution"}],
            "collection-editor": [{"literal": "Series editor"}],
            "translator": [{"literal": "Translator"}],
            "contributor": [{"literal": "Contributor"}],
        },
    )
    assert (
        "<secondary-authors><author>Institution</author><author>Series editor</author>"
        "</secondary-authors>" in report
    )
    assert (
        "<subsidiary-authors><author>Translator</author><author>Contributor</author>"
        "</subsidiary-authors>" in report
    )

    speech = _record_xml(
        "speech",
        title="Speech",
        chair=[{"literal": "Chair"}],
        organizer=[{"literal": "Organizer"}],
    )
    assert (
        "<secondary-authors><author>Chair</author><author>Organizer</author>"
        "</secondary-authors>" in speech
    )


@pytest.mark.parametrize("language", ["en", "zh-CN", "zh-Hant-TW"])
def test_language_tags_are_exported_without_normalization(language: str) -> None:
    source = json.dumps(
        [{"id": "example", "type": "book", "title": "Example", "language": language}]
    )
    item = parse_csl_json(source)["example"]
    xml = etree.tostring(endnote._record("example", item), encoding="unicode")
    assert f"<language>{language}</language>" in xml
