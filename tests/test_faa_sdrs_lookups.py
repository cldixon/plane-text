import zipfile
from pathlib import Path

import polars as pl
import pytest

from plane_text import faa_sdrs_lookups as lookups

# Two real sheets from the FAA's SDRS Look-Up Tables zip (2026-07-09).
ARCHIVE = zipfile.ZipFile(Path(__file__).parent / "fixtures" / "sdrs-lookups.zip")


def sheet(suffix: str) -> lookups.Sheet:
    return next(s for s in lookups.SHEETS if s.suffix == suffix)


def test_read_code_sheet_keeps_codes_as_text():
    frame, sheet_date = lookups.read_sheet(ARCHIVE, sheet("HowDiscovered"))
    assert sheet_date == "2026-07-09"
    assert frame.columns == ["code", "description"]
    assert frame.schema == {"code": pl.String, "description": pl.String}
    codes = dict(frame.iter_rows())
    assert codes["V"] == "Visual"
    assert codes["9"] == "Unknown"  # a digit code stays a string
    assert codes["0"] == "Not Available"


def test_read_two_column_reference_sheet():
    frame, _ = lookups.read_sheet(ARCHIVE, sheet("PartMake"))
    assert frame.columns == ["code", "name"]
    assert frame.height > 1000
    assert frame["code"].null_count() == 0
    assert frame.height == frame.unique().height


def test_missing_sheet_is_an_error():
    with pytest.raises(LookupError):
        lookups.read_sheet(ARCHIVE, sheet("SubmitterType"))


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
