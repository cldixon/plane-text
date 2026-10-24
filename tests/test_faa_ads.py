import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from faa_drs import Document

from plane_text import faa_ads

# Three real ADFRAWD records, with long text trimmed and contact fields removed:
# a recent Federal Register AD, a 1950 AD, and a correction.
RECORDS = json.loads((Path(__file__).parent / "fixtures" / "drs-adfrawd.json").read_text())


def frame() -> pl.DataFrame:
    return faa_ads.to_frame(RECORDS)


def row(ad_number: str, action: str | None = None) -> dict:
    rows = frame().filter(pl.col("ad_number") == ad_number)
    if action:
        rows = rows.filter(pl.col("action") == action)
    return rows.row(0, named=True)


def test_frame_matches_schema():
    assert frame().schema == faa_ads.SCHEMA
    assert frame().height == 3


def test_federal_register_ad():
    ad = row("2026-21-05")
    assert ad["guid"] == "FR-ADFRAWD-2026-21664-0000000000"
    assert ad["status"] == "Current"
    assert ad["action"] == "Final rule."
    assert ad["make"] == ["MD Helicopters LLC"]
    assert ad["model"] == ["369D", "369E", "369F", "369FF", "369H", "500N"]
    assert ad["product_subtype"] == ["Rotorcraft"]
    assert ad["supersedes"] is None
    assert ad["publish_date"] == date(2026, 10, 23)
    assert ad["effective_date"] == date(2026, 11, 27)
    assert ad["summary"].startswith("The FAA is adopting a new airworthiness directive")
    assert ad["regulatory_text"]
    assert ad["last_modified"].year == 2026


def test_legacy_ad_text_keeps_paragraphs_and_drops_placeholders():
    ad = row("50-05-01")
    assert ad["summary"] is None
    assert ad["supplementary_info"] is None
    assert ad["docket_number"] is None  # "Unknown" in DRS
    assert ad["citation"] is None  # "This information is not available." in DRS
    assert ad["regulatory_text"].startswith(
        "50-05-01 PIPER: Applies to All J3C, PA-11, PA-12 Aircraft Manufactured Between "
        "November 1945 and November 1946 Inclusive.\n\nCompliance required at next periodic inspection"
    )


def test_correction_shares_ad_number_but_has_its_own_row():
    correction = row("2026-20-10")
    assert correction["action"] == "Final rule; correction."
    assert correction["guid"] == "FR-ADFRAWD-2026-21686-0000000000"


def test_duplicate_guids_are_dropped():
    assert faa_ads.to_frame(RECORDS + RECORDS[:1]).height == 3


def test_clean_text():
    assert faa_ads.clean_text("  a\t\tb \r\n\n\n\n c  ") == "a b\n\nc"
    assert faa_ads.clean_text("Unknown") is None
    assert faa_ads.clean_text(None) is None


def test_raw_frame_keeps_api_names_and_text():
    raw = faa_ads.raw_frame([RECORDS, RECORDS[:1]])
    assert raw.height == 4  # a record read twice stays twice in raw
    assert raw.columns[:3] == ["doctype", "documentGuid", "documentURL"]
    assert "drs:adfrawdRegulatoryText" in raw.columns
    assert raw.columns[-2:] == ["page_offset", "page_row"]
    assert raw["page_offset"].to_list() == [0, 0, 0, 3]
    assert raw["page_row"].to_list() == [0, 1, 2, 0]
    assert raw.schema["hasMoreAttachments"] == pl.String
    assert set(raw["hasMoreAttachments"]) <= {"true", "false"}
    assert raw.schema["drs:adfrawdModel"] == pl.List(pl.String)
    legacy = raw.filter(pl.col("drs:documentNumber") == "50-05-01").row(0, named=True)
    assert legacy["drs:adfrawdDocketNo"] == "Unknown"  # placeholders are cleaned in processed, not raw


def test_processed_from_raw_matches_the_records():
    raw = faa_ads.raw_frame([RECORDS, RECORDS[:1]])
    assert faa_ads.to_frame(faa_ads.records(raw)).equals(frame())


def test_raw_frame_rejects_text_where_lists_are_expected():
    mixed = json.loads(json.dumps(RECORDS))
    mixed[0]["metadata"]["drs:adfrawdModel"] = "369D"
    with pytest.raises(ValueError):
        faa_ads.raw_frame([mixed])


def test_fetch_pages_reuses_cache(tmp_path: Path):
    class FakeDRS:
        def __init__(self) -> None:
            self.calls = 0

        def list_documents(self, doctype, *, offset, sort):
            self.calls += 1
            return SimpleNamespace(documents=[Document.model_validate(r) for r in RECORDS])

    drs = FakeDRS()
    first = faa_ads.fetch_pages(drs, tmp_path)  # ty: ignore[invalid-argument-type]
    second = faa_ads.fetch_pages(drs, tmp_path)  # ty: ignore[invalid-argument-type]
    assert drs.calls == 1  # a short page ends the read; the second read is all cache
    assert first == second
    assert [r["guid"] for r in first[0]] == [r["guid"] for r in RECORDS]
