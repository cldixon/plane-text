import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from plane_text import faa_sdrs

# Two real 2025 reports: an aileron write-up and a structural repair.
RAW = (Path(__file__).parent / "fixtures" / "sdr-2025.csv").read_bytes()


def test_snake():
    assert faa_sdrs.snake("OperatorControlNumber") == "operator_control_number"
    assert faa_sdrs.snake("JASCCode") == "jasc_code"
    assert faa_sdrs.snake("RegistryNNumber") == "registry_n_number"
    assert faa_sdrs.snake("ButtlineTo") == "butt_line_to"
    assert faa_sdrs.snake("ButtLineFrom") == "butt_line_from"


def test_parse_year():
    frame = faa_sdrs.parse_year(RAW, 2025)
    assert frame.height == 2
    assert frame.columns[0] == "operator_control_number"
    assert frame.columns[-1] == "file_year"
    assert "discrepancy" in frame.columns

    first = frame.row(0, named=True)
    assert first["operator_control_number"] == "SCNA20250102001"
    assert first["difficulty_date"] == date(2025, 1, 1)
    assert first["submission_date"] == datetime(2025, 1, 2, 14, 45, 26, 140000, tzinfo=UTC)
    assert first["jasc_code"] == "2710"
    assert first["part_name"] == "AILERON"
    assert first["fuselage_station_from"] is None  # empty strings become null
    assert first["discrepancy"].startswith("ON APPROACH WITH FLAPS 30")
    assert first["file_year"] == 2025

    second = frame.row(1, named=True)
    assert second["fuselage_station_from"] == "977.00"


def test_combine_rejects_a_changed_layout():
    frame = faa_sdrs.parse_year(RAW, 2025)
    assert faa_sdrs.combine([frame, frame]).height == 4
    with pytest.raises(ValueError):
        faa_sdrs.combine([frame, frame.drop("discrepancy")])


def test_fetch_year_reuses_cache(tmp_path: Path):
    class NoHTTP:
        def get(self, url: str):
            raise AssertionError("a cached year must not be downloaded")

    (tmp_path / "SDR-2025.csv.gz").write_bytes(gzip.compress(RAW))
    assert faa_sdrs.fetch_year(NoHTTP(), 2025, tmp_path, refresh=False) == RAW  # ty: ignore[invalid-argument-type]
