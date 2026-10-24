import json
from pathlib import Path

import polars as pl
import pytest

from plane_text import faa_proposed_rules as fpr

# Real DRS records: an AD proposed rule with inline text, one whose body is a
# Federal Register link, and a general NPRM with list-valued sections.
RECORDS = json.loads((Path(__file__).parent / "fixtures" / "drs-documents.json").read_text())


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


def test_ad_proposed_rules():
    frame = fpr.build(RECORDS["ADNPRM"], fpr.AD_NPRM)
    inline, linked = frame.row(0, named=True), frame.row(1, named=True)
    assert inline["docket_number"] == "FAA-2005-22430"
    assert inline["regulatory_text"].startswith("The Proposed Amendment")
    assert inline["federal_register_number"] is None
    assert linked["regulatory_text"] is None
    assert linked["federal_register_number"] == "2016-15927"


def test_nprm_list_sections_are_joined():
    row = fpr.build(RECORDS["NPRM"], fpr.NPRM).row(0, named=True)
    assert row["notice_number"] == "12-07"
    assert row["supplementary_info"].startswith('SUPPLEMENTARY INFORMATION: See the "Additional Information"')


def test_add_federal_register_uses_cache(tmp_path):
    (tmp_path / "2016-15927.txt").write_text("  Full   text of the proposed AD.  ")
    frame = fpr.build(RECORDS["ADNPRM"], fpr.AD_NPRM)
    filled = fpr.add_federal_register(frame, tmp_path)
    assert filled.filter(pl.col("federal_register_number") == "2016-15927")[
        "federal_register_text"
    ].to_list() == ["Full text of the proposed AD."]
    assert filled.filter(pl.col("federal_register_number").is_null())["federal_register_text"].to_list() == [
        None
    ]
