import zipfile
from pathlib import Path

import polars as pl
import pytest

from plane_text import faa_sdrs_lookups as lookups

# Two real sheets from the FAA's SDRS Look-Up Tables zip (2026-07-09).
ARCHIVE = zipfile.ZipFile(Path(__file__).parent / "fixtures" / "sdrs-lookups.zip")


def sheet(suffix: str) -> lookups.Sheet:
    return next(s for s in lookups.SHEETS if s.suffix == suffix)


WORKBOOKS = lookups.read_workbooks(ARCHIVE)


def test_raw_workbooks_keep_their_own_headers_and_text():
    assert set(WORKBOOKS) == {"HowDiscovered", "PartMake"}
    frame, member = WORKBOOKS["HowDiscovered"]
    assert member == "2026-07-09_HowDiscovered.xlsx"
    assert frame.columns == ["HowDiscoveredCode", "Description"]
    assert frame.schema == {"HowDiscoveredCode": pl.String, "Description": pl.String}
    assert frame.height == 14


def test_sheet_name():
    assert lookups.sheet_name("2026-07-09_HowDiscovered.xlsx") == "HowDiscovered"
    assert lookups.sheet_name("2026-07-06_AirCraft.xlsx") == "AirCraft"


def test_tidy_code_sheet_keeps_codes_as_text():
    frame = lookups.tidy(WORKBOOKS["HowDiscovered"][0], sheet("HowDiscovered"))
    assert frame.columns == ["code", "description"]
    assert frame.schema == {"code": pl.String, "description": pl.String}
    codes = dict(frame.iter_rows())
    assert codes["V"] == "Visual"
    assert codes["9"] == "Unknown"  # a digit code stays a string
    assert codes["0"] == "Not Available"


def test_tidy_two_column_reference_sheet():
    frame = lookups.tidy(WORKBOOKS["PartMake"][0], sheet("PartMake"))
    assert frame.columns == ["code", "name"]
    assert frame.height > 1000
    assert frame["code"].null_count() == 0
    assert frame.height == frame.unique().height


def test_tidy_collapses_spaces_and_drops_empty_and_duplicate_rows():
    raw = pl.DataFrame({"Code": [" A ", None, "B", "B", ""], "Description": ["x  y", "z", "w", "w", "v"]})
    frame = lookups.tidy(raw, sheet("TimeSinceCode"))
    assert frame.rows() == [("A", "x y"), ("B", "w")]


def test_tidy_rejects_a_changed_layout():
    with pytest.raises(ValueError):
        lookups.tidy(WORKBOOKS["PartMake"][0], sheet("PartCondition"))


def test_missing_workbook_is_an_error():
    with pytest.raises(LookupError):
        lookups.require_workbooks(set(WORKBOOKS))
    lookups.require_workbooks({s.suffix for s in (*lookups.SHEETS, lookups.JASC_SHEET)})


def test_parse_jasc_lines():
    titles, chapters = lookups.parse_jasc_lines(
        [
            "27   FLIGHT CONTROLS  ",
            "2710 AILERON CONTROL SYSTEM ",
            "3397 LIGHT SYSTEM WIRING",
            "2710 Aileron Control System",  # definitions section, title case: ignored
            "27 - Flight Controls",
            "AILERON CONTROL SYSTEM 2710",
        ]
    )
    assert titles == {"2710": "AILERON CONTROL SYSTEM", "3397": "LIGHT SYSTEM WIRING"}
    assert chapters == {"27": "FLIGHT CONTROLS"}


def test_jasc_table_combines_both_sources():
    sheet_codes = pl.DataFrame({"code": ["2710", "4900"], "description": ["Aileron text.", "APU text."]})
    table = lookups.jasc_table(
        sheet_codes,
        titles={"2710": "AILERON CONTROL SYSTEM", "3397": "LIGHT SYSTEM WIRING"},
        chapters={"27": "FLIGHT CONTROLS", "33": "LIGHTS", "49": "AIRBORNE AUXILIARY POWER"},
    )
    assert table.rows() == [
        ("2710", "27", "FLIGHT CONTROLS", "AILERON CONTROL SYSTEM", "Aileron text."),
        ("3397", "33", "LIGHTS", "LIGHT SYSTEM WIRING", None),
        ("4900", "49", "AIRBORNE AUXILIARY POWER", None, "APU text."),
    ]
