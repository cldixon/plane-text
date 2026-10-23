from pathlib import Path

import pytest

from plane_text import ecfr

FIXTURES = Path(__file__).parent / "fixtures"
DATE = "2026-10-21"


def parse(name: str, ref: ecfr.PartRef) -> list[ecfr.Section]:
    return ecfr.parse_part((FIXTURES / name).read_bytes(), ref, DATE)


def test_list_parts_carries_chapter_and_subchapter_and_skips_reserved():
    structure = {
        "type": "title",
        "identifier": "14",
        "children": [
            {
                "type": "chapter",
                "identifier": "I",
                "children": [
                    {
                        "type": "subchapter",
                        "identifier": "C",
                        "children": [
                            {"type": "part", "identifier": "21"},
                            {"type": "part", "identifier": "22", "reserved": True},
                        ],
                    }
                ],
            },
            {
                "type": "chapter",
                "identifier": "V",
                "children": [{"type": "part", "identifier": "1201"}],
            },
        ],
    }
    assert ecfr.list_parts(structure) == [
        ecfr.PartRef("21", "I", "C"),
        ecfr.PartRef("1201", "V", None),
    ]


def test_parse_part_sections():
    rows = parse("ecfr-part-39.xml", ecfr.PartRef("39", "I", "C"))

    assert [r.identifier for r in rows] == ["39.1", "39.3"]  # reserved 39.4 dropped
    first, second = rows
    assert first.heading == "Purpose of this regulation."
    assert first.part_heading == "PART 39 - AIRWORTHINESS DIRECTIVES"
    assert first.text.startswith("The regulations in this part provide a legal framework")
    assert "\n" not in first.text  # whitespace inside a paragraph is collapsed
    assert first.citation is None
    assert first.subpart is None

    assert second.text.split("\n\n") == [
        "FAA's airworthiness directives are legally enforceable rules that apply to the following products:",
        "(a) Aircraft,",
        "(b) Aircraft engines, and propellers.",
    ]
    assert second.citation == "[Doc. No. FAA-2000-8460, 67 FR 48003, July 22, 2002]"
    assert second.source_url == ecfr.part_url(DATE, "39")


def test_parse_part_subparts_subject_groups_and_appendices():
    rows = parse("ecfr-part-25.xml", ecfr.PartRef("25", "I", "C"))

    assert [(r.identifier, r.kind, r.subpart) for r in rows] == [
        ("25.1", "section", "A"),
        ("25.21", "section", "B"),
        ("Appendix A to Part 25", "appendix", None),
    ]
    assert rows[1].subpart_heading == "Subpart B - Flight"
    assert rows[2].heading == "Appendix A to Part 25"


def test_parse_part_finds_part_below_root():
    xml = (
        b'<DIV1 TYPE="TITLE"><DIV5 N="1" TYPE="PART"><HEAD>PART 1</HEAD>'
        b'<DIV8 N="1.1" TYPE="SECTION"><HEAD>\xc2\xa7 1.1 General definitions.</HEAD>'
        b"<P>Text.</P></DIV8></DIV5></DIV1>"
    )
    rows = ecfr.parse_part(xml, ecfr.PartRef("1", "I", "A"), DATE)
    assert [(r.identifier, r.heading) for r in rows] == [("1.1", "General definitions.")]


def test_parse_part_without_part_element():
    with pytest.raises(ValueError):
        ecfr.parse_part(b"<DIV1 TYPE='TITLE'/>", ecfr.PartRef("1", None, None), DATE)


def test_to_frame_matches_schema():
    rows = parse("ecfr-part-39.xml", ecfr.PartRef("39", "I", "C"))
    frame = ecfr.to_frame(rows)
    assert frame.schema == ecfr.SCHEMA
    assert frame.height == 2
