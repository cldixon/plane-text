import json
from pathlib import Path

import polars as pl
import pytest

from plane_text import faa_proposed_rules as fpr
from plane_text.faa_ads import raw_frame, records

# Real DRS records: an AD proposed rule with inline text, one whose body is a
# Federal Register link, and a general NPRM with list-valued sections.
RECORDS = json.loads((Path(__file__).parent / "fixtures" / "drs-documents.json").read_text())
NO_TEXTS = pl.DataFrame(schema=fpr.FR_SCHEMA)


@pytest.mark.parametrize(
    ("link", "number"),
    [
        ("https://federalregister.gov/a/2016-15927", "2016-15927"),
        ("https://www.federalregister.gov/d/2019-06275", "2019-06275"),
        ("https://www.federalregister.gov/documents/2006/07/07/E6-10596/enhanced", "E6-10596"),
        ("https://www.federalregister.gov/documents/2022/11/10/R1-2022-22698/ads", "R1-2022-22698"),
        ("https://www.gpo.gov/fdsys/pkg/FR-2012-01-05/pdf/2011-33717.pdf", "2011-33717"),
        (
            "https://federalregister.gov/a/2014-04568\nCorrection: https://federalregister.gov/a/C1-2014-04568",
            "2014-04568",
        ),
    ],
)
def test_federal_register_links(link, number):
    match = fpr.FR_LINK.search(link)
    assert match is not None
    assert match.group(1) == number


def test_body_text():
    assert fpr.body_text(["First paragraph.", None, "Second."]) == "First paragraph.\n\nSecond."
    assert fpr.body_text("https://federalregister.gov/a/2016-15927") is None
    assert fpr.body_text(None) is None
    assert fpr.body_text("Line one.\\nLine two.") == "Line one.\nLine two."


def test_one_line():
    rule = "-" * 71
    assert (
        fpr.one_line(f"Notice of proposed rulemaking (NPRM).\\n{rule}")
        == "Notice of proposed rulemaking (NPRM)."
    )
    assert (
        fpr.one_line(f"Supplemental notice of proposed rulemaking; reopening of\ncomment period.\n{rule}")
        == "Supplemental notice of proposed rulemaking; reopening of comment period."
    )
    assert fpr.one_line(None) is None


def test_raw_keeps_records_as_delivered():
    raw = raw_frame([RECORDS["ADNPRM"], RECORDS["ADNPRM"][:1]])
    assert raw.height == len(RECORDS["ADNPRM"]) + 1  # duplicates kept in raw
    assert raw.columns[:2] == ["doctype", "documentGuid"]
    assert raw.columns[-2:] == ["page_offset", "page_row"]
    linked = raw.filter(pl.col("documentGuid") == RECORDS["ADNPRM"][1]["guid"]).row(0, named=True)
    assert linked["drs:adnprmRegulatoryText"] == RECORDS["ADNPRM"][1]["metadata"]["drs:adnprmRegulatoryText"]
    nprm = raw_frame([RECORDS["NPRM"]])
    assert nprm.schema["drs:nprmSupplementaryInfo"] == pl.List(pl.String)


def test_processed_from_raw_matches_the_records():
    for code, spec in (("ADNPRM", fpr.AD_NPRM), ("NPRM", fpr.NPRM)):
        raw = raw_frame([RECORDS[code], RECORDS[code]])
        assert fpr.build(records(raw), spec).equals(fpr.build(RECORDS[code], spec))


def test_ad_proposed_rules():
    frame = fpr.to_frame(records(raw_frame([RECORDS["ADNPRM"]])), NO_TEXTS, fpr.AD_NPRM)
    inline, linked = frame.row(0, named=True), frame.row(1, named=True)
    assert inline["docket_number"] == "FAA-2005-22430"
    assert inline["regulatory_text"].startswith("The Proposed Amendment")
    assert inline["federal_register_number"] is None
    assert linked["regulatory_text"] is None
    assert linked["federal_register_number"] == "2016-15927"
    assert frame["federal_register_text"].to_list() == [None, None]


def test_nprm_list_sections_are_joined():
    row = fpr.build(RECORDS["NPRM"], fpr.NPRM).row(0, named=True)
    assert row["notice_number"] == "12-07"
    assert row["supplementary_info"].startswith('SUPPLEMENTARY INFORMATION: See the "Additional Information"')


def test_federal_register_text_joins_from_raw():
    texts = pl.DataFrame(
        [
            {
                "document_number": "2016-15927",
                "raw_text": "  Full   text of the proposed AD.  ",
                "retrieved_at": "x",
            }
        ],
        schema=fpr.FR_SCHEMA,
    )
    filled = fpr.to_frame(RECORDS["ADNPRM"], texts, fpr.AD_NPRM)
    assert filled.filter(pl.col("federal_register_number") == "2016-15927")[
        "federal_register_text"
    ].to_list() == ["Full text of the proposed AD."]
    assert filled.filter(pl.col("federal_register_number").is_null())["federal_register_text"].to_list() == [
        None
    ]


def test_federal_register_frame_reads_the_cache(tmp_path: Path):
    (tmp_path / "2016-15927.txt").write_text("  Full text, as fetched.  ")
    texts = fpr.federal_register_frame(["2016-15927", "2019-06275", "2016-15927"], tmp_path)
    assert texts["document_number"].to_list() == ["2016-15927"]  # only what is cached, once each
    assert texts["raw_text"].to_list() == ["  Full text, as fetched.  "]  # as fetched, not cleaned
    assert fpr.federal_register_frame([], tmp_path).schema == pl.Schema(fpr.FR_SCHEMA)


def test_fetch_stops_when_the_federal_register_is_unreachable(tmp_path: Path, monkeypatch):
    calls = []

    def unreachable(http, number, cache):
        calls.append(number)
        raise fpr.httpx.ConnectError("blocked")

    monkeypatch.setattr(fpr, "fetch_federal_register", unreachable)
    fpr.fetch_all_federal_register(["2016-15927", "2019-06275"], tmp_path)
    assert calls == ["2016-15927"]
