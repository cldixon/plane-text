import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
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
