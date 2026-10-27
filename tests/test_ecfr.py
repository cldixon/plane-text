import json
import shutil
from pathlib import Path

import httpx
import polars as pl
import pytest

from plane_text import ecfr, storage

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
        ("25.25", "section", "B"),
        ("Appendix A to Part 25", "appendix", None),
    ]
    assert rows[1].subpart_heading == "Subpart B - Flight"
    assert rows[3].heading == "Appendix A to Part 25"


def test_parse_part_renders_tables_row_by_row():
    rows = parse("ecfr-part-25.xml", ecfr.PartRef("25", "I", "C"))
    table = next(r for r in rows if r.identifier == "25.25")
    assert table.text == (
        "(a) Maximum weights are listed in table 1.\n\n"
        "Table 1 to § 25.25\n"
        "Condition | Maximum weight\n"
        "Takeoff | 100,000 lb\n"
        "Landing | 90,000 lb"
    )


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


# --- raw and processed layers -------------------------------------------------

# A trimmed title 14 structure in the API's shape: sizes are numbers,
# `reserved` a boolean and `volumes` a list.
STRUCTURE = {
    "identifier": "14",
    "label": "Title 14—Aeronautics and Space",
    "reserved": False,
    "type": "title",
    "size": 16966767,
    "children": [
        {
            "identifier": "I",
            "label": " Chapter I—Federal Aviation Administration, Department of Transportation",
            "reserved": False,
            "type": "chapter",
            "size": 12378811,
            "descendant_range": "1 \u2013 199",  # an en dash, as delivered
            "children": [
                {
                    "identifier": "C",
                    "reserved": False,
                    "type": "subchapter",
                    "children": [
                        {"identifier": "22", "reserved": True, "type": "part", "volumes": ["1"]},
                        {"identifier": "25", "reserved": False, "type": "part", "volumes": ["1"]},
                        {"identifier": "39", "reserved": False, "type": "part", "volumes": ["1", "2"]},
                    ],
                }
            ],
        }
    ],
}


def as_delivered_text(node: dict) -> dict:
    """`node` with the values raw keeps as text turned to text, `reserved` left boolean."""
    out = {k: v if k == "reserved" else ecfr.as_text(v) for k, v in node.items() if k != "children"}
    if node.get("children"):
        out["children"] = [as_delivered_text(child) for child in node["children"]]
    return out


def test_structure_frame_keeps_every_node_with_api_keys_as_text():
    raw = ecfr.structure_frame(STRUCTURE)
    assert raw.height == 6
    assert raw.columns[:3] == ["node", "parent", "depth"]
    assert raw["node"].to_list() == [0, 1, 2, 3, 4, 5]
    assert raw["parent"].to_list() == [None, 0, 1, 2, 2, 2]
    assert raw["depth"].to_list() == [0, 1, 2, 3, 3, 3]
    assert raw["type"].to_list() == ["title", "chapter", "subchapter", "part", "part", "part"]
    assert raw["reserved"].to_list() == ["false", "false", "false", "true", "false", "false"]
    assert raw["size"].to_list()[:2] == ["16966767", "12378811"]
    assert raw.schema["volumes"] == pl.List(pl.String)
    assert raw["volumes"].to_list()[-1] == ["1", "2"]
    assert raw["label"][1].startswith(" Chapter I")  # not trimmed


def test_structure_round_trips_through_raw():
    tree = ecfr.structure_tree(ecfr.structure_frame(STRUCTURE))
    assert tree == as_delivered_text(STRUCTURE)
    assert (
        ecfr.list_parts(tree)
        == ecfr.list_parts(STRUCTURE)
        == [
            ecfr.PartRef("25", "I", "C"),
            ecfr.PartRef("39", "I", "C"),
        ]
    )


def test_structure_frame_rejects_text_where_lists_are_expected():
    mixed = json.loads(json.dumps(STRUCTURE))
    mixed["children"][0]["volumes"] = "1"
    with pytest.raises(ValueError):
        ecfr.structure_frame(mixed)


def raw_parts() -> pl.DataFrame:
    rows = [
        ecfr.part_row(ref, DATE, (FIXTURES / f"ecfr-part-{ref.part}.xml").read_bytes())
        for ref in ecfr.list_parts(STRUCTURE)
    ]
    return ecfr.parts_frame(rows)


def test_parts_frame_keeps_the_xml_as_delivered():
    raw = raw_parts()
    assert raw.schema == ecfr.PARTS_SCHEMA
    assert raw.select("issue_date", "chapter", "subchapter", "part").rows() == [
        (DATE, "I", "C", "25"),
        (DATE, "I", "C", "39"),
    ]
    assert raw["source_url"].to_list() == [ecfr.part_url(DATE, "25"), ecfr.part_url(DATE, "39")]
    for row in raw.iter_rows(named=True):
        assert row["xml"].encode("utf-8") == (FIXTURES / f"ecfr-part-{row['part']}.xml").read_bytes()


def test_part_row_rejects_xml_that_is_not_utf8():
    with pytest.raises(UnicodeDecodeError):
        ecfr.part_row(ecfr.PartRef("1", "I", "A"), DATE, b"<P>\xa7 1.1</P>")


def test_processed_from_raw_matches_parsing_the_parts():
    expected = ecfr.to_frame(
        parse("ecfr-part-25.xml", ecfr.PartRef("25", "I", "C"))
        + parse("ecfr-part-39.xml", ecfr.PartRef("39", "I", "C"))
    )
    assert ecfr.sections_from_raw(raw_parts()).equals(expected)


def offline(monkeypatch, handler=None) -> list[str]:
    """Route eCFR requests to `handler`; with none, any request fails. Returns the URLs requested."""
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if handler is None:
            raise AssertionError(f"unexpected request: {request.url}")
        return handler(request)

    monkeypatch.setattr(ecfr, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    return requested


def cache_snapshot(data_dir: Path, snapshot: str) -> Path:
    cache = storage.cache_dir(data_dir, ecfr.DATASET) / snapshot
    cache.mkdir(parents=True)
    (cache / "structure.json").write_text(json.dumps(STRUCTURE))
    for part in ("25", "39"):
        shutil.copy(FIXTURES / f"ecfr-part-{part}.xml", cache / f"part-{part}.xml")
    return cache


def test_run_builds_raw_and_processed_from_the_cache_alone(tmp_path: Path, monkeypatch):
    requested = offline(monkeypatch)
    cache_snapshot(tmp_path, DATE)

    paths = ecfr.run(tmp_path, DATE, [storage.RAW, storage.PROCESSED], upload=False)

    assert requested == []
    assert [storage.object_key(p, tmp_path) for p in paths] == [
        f"raw/ecfr-title14/v{DATE}/structure.parquet",
        f"raw/ecfr-title14/v{DATE}/parts.parquet",
        f"processed/ecfr-title14/v{DATE}/sections.parquet",
    ]
    structure, parts, sections = paths
    assert ecfr.structure_tree(pl.read_parquet(structure)) == as_delivered_text(STRUCTURE)
    assert pl.read_parquet(parts).equals(raw_parts())
    assert pl.read_parquet(sections).equals(ecfr.sections_from_raw(raw_parts()))

    raw_provenance = storage.read_provenance(parts)
    assert raw_provenance["layer"] == "raw"
    assert raw_provenance["snapshot"] == raw_provenance["issue_date"] == DATE
    assert raw_provenance["source_url"] == f"{ecfr.API}/full/{DATE}/title-14.xml"
    assert storage.read_provenance(structure)["source_url"] == ecfr.structure_url(DATE)
    processed_provenance = storage.read_provenance(sections)
    assert processed_provenance["layer"] == "processed"
    assert processed_provenance["built_from"] == f"raw/ecfr-title14/v{DATE}/parts.parquet"
    assert processed_provenance["retrieved_at"] == raw_provenance["retrieved_at"]


def test_processed_step_needs_only_raw(tmp_path: Path, monkeypatch):
    offline(monkeypatch)
    cache = cache_snapshot(tmp_path, DATE)
    ecfr.run(tmp_path, DATE, [storage.RAW], upload=False)
    shutil.rmtree(cache)

    (path,) = ecfr.run(tmp_path, DATE, [storage.PROCESSED], upload=False)
    assert pl.read_parquet(path).height == 6


def test_raw_fetches_what_is_not_cached_and_a_later_snapshot_means_the_latest_issue(
    tmp_path: Path, monkeypatch
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/titles.json"):
            return httpx.Response(200, json={"titles": [{"number": 14, "latest_issue_date": DATE}]})
        if str(request.url) == ecfr.structure_url(DATE):
            return httpx.Response(200, json=STRUCTURE)
        part = request.url.params["part"]
        return httpx.Response(200, content=(FIXTURES / f"ecfr-part-{part}.xml").read_bytes())

    monkeypatch.setattr(ecfr.time, "sleep", lambda _: None)
    requested = offline(monkeypatch, handler)

    snapshot, paths = ecfr.build_raw(tmp_path, "2099-01-01")

    assert snapshot == DATE
    assert requested == [
        f"{ecfr.API}/titles.json",
        ecfr.structure_url(DATE),
        ecfr.part_url(DATE, "25"),
        ecfr.part_url(DATE, "39"),
    ]
    assert pl.read_parquet(paths[1]).equals(raw_parts())
    cache = storage.cache_dir(tmp_path, ecfr.DATASET) / DATE
    assert sorted(p.name for p in cache.iterdir()) == ["part-25.xml", "part-39.xml", "structure.json"]

    requested.clear()
    ecfr.build_raw(tmp_path, DATE)
    assert requested == []  # the second build is all cache
