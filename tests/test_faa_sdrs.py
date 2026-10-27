import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from plane_text import faa_sdrs

# Two real 2025 reports: an aileron write-up and a structural repair.
RAW = (Path(__file__).parent / "fixtures" / "sdr-2025.csv").read_bytes()

# A slice of the processed lookups, as `load_lookups` returns them.
LOOKUPS = {
    "submitter_type": {"A": "CARRIER PART 121", "B": "REPAIR STATION PART 145"},
    "sdr_type": {"A": "AIR CARRIER", "G": "GENERAL AVIATION"},
    "nature_of_condition": {"F": "FLT CONT AFFECTED", "O": "OTHER"},
    "precautionary_procedure": {"K": "NONE", "O": "OTHER"},
    "stage_of_operation": {"AP": "APPROACH", "IN": "INSP/MAINT"},
    "how_discovered": {"O": "Other", "V": "Visual", "9": "Unknown"},
    "time_since": {"O": "Overhaul", "R": "Repair", "I": "Inspection"},
    "part_location_zone": {"ZONE 100": "Fuselage Lower", "ZONE 200": "Fuselage Top"},
    "jasc_title": {"2710": "AILERON CONTROL SYSTEM", "5330": "FUSELAGE PLATING/SKIN"},
    "jasc_chapter_title": {"27": "FLIGHT CONTROLS", "53": "FUSELAGE"},
}


def raw() -> pl.DataFrame:
    return faa_sdrs.read_year(RAW, 2025)


def processed(frame: pl.DataFrame | None = None) -> pl.DataFrame:
    return faa_sdrs.process(raw() if frame is None else frame, LOOKUPS)[0]


def test_snake():
    assert faa_sdrs.snake("OperatorControlNumber") == "operator_control_number"
    assert faa_sdrs.snake("JASCCode") == "jasc_code"
    assert faa_sdrs.snake("RegistryNNumber") == "registry_n_number"
    assert faa_sdrs.snake("ButtlineTo") == "butt_line_to"
    assert faa_sdrs.snake("ButtLineFrom") == "butt_line_from"


def test_raw_keeps_the_source_as_delivered():
    frame = raw()
    assert frame.height == 2
    assert frame.columns[:3] == ["OperatorControlNumber", "DifficultyDate", "SubmissionDate"]
    assert frame.columns[-3:] == ["Discrepancy", "source_file", "source_row"]
    assert all(dtype == pl.String for c, dtype in frame.schema.items() if c != "source_row")

    first = frame.row(0, named=True)
    assert first["DifficultyDate"] == "01/01/2025"  # not parsed
    assert first["AircraftTotalTime"] == "53568"  # not cast
    assert first["FuselageStationFrom"] is None  # an empty field
    assert "BEOND 10-15 DEGREES OF BANK" in first["Discrepancy"]
    assert (first["source_file"], first["source_row"]) == ("SDR-2025.csv", 1)


def test_combine_rejects_a_changed_layout():
    frame = raw()
    assert faa_sdrs.combine([frame, frame]).height == 4
    with pytest.raises(ValueError):
        faa_sdrs.combine([frame, frame.drop("Discrepancy")])


def test_fetch_year_reuses_cache(tmp_path: Path):
    class NoHTTP:
        def get(self, url: str):
            raise AssertionError("a cached year must not be downloaded")

    (tmp_path / "SDR-2025.csv.gz").write_bytes(gzip.compress(RAW))
    assert faa_sdrs.fetch_year(NoHTTP(), 2025, tmp_path, refresh=False) == RAW  # ty: ignore[invalid-argument-type]


def test_processed_types_dates_and_measures():
    first, second = processed().iter_rows(named=True)
    assert first["operator_control_number"] == "SCNA20250102001"
    assert first["difficulty_date"] == date(2025, 1, 1)
    assert first["submission_date"] == datetime(2025, 1, 2, 14, 45, 26, 140000, tzinfo=UTC)
    assert first["aircraft_total_time"] == 53568
    assert first["aircraft_total_cycles"] == 29760
    assert first["file_year"] == 2025
    assert second["part_total_time"] == 30062
    assert second["corrosion_level"] == 2
    assert second["fuselage_station_from"] == "977.00"  # a location designator stays text
    assert first["discrepancy"].startswith("ON APPROACH WITH FLAPS 30")
    assert "source_file" not in processed().columns


def test_processed_labels_sit_next_to_their_codes():
    frame = processed()
    columns = frame.columns
    for code in faa_sdrs.CODES:
        assert columns.index(code.label) == columns.index(code.column) + 1
    jasc = columns.index("jasc_code")
    assert columns[jasc : jasc + 4] == ["jasc_code", "jasc_chapter", "jasc_chapter_title", "jasc_title"]
    location = columns.index("part_location")
    assert columns[location + 1 : location + 3] == ["part_location_zone_code", "part_location_zone"]

    first, second = frame.iter_rows(named=True)
    assert (first["submitter_type_code"], first["submitter_type"]) == ("A", "CARRIER PART 121")
    assert (first["sdr_type_code"], first["sdr_type"]) == ("A", "AIR CARRIER")
    assert (first["stage_of_operation_code"], first["stage_of_operation"]) == ("AP", "APPROACH")
    assert first["nature_of_condition_a"] == "FLT CONT AFFECTED"
    assert first["nature_of_condition_b_code"] is None and first["nature_of_condition_b"] is None
    assert (first["jasc_chapter"], first["jasc_chapter_title"]) == ("27", "FLIGHT CONTROLS")
    assert first["jasc_title"] == "AILERON CONTROL SYSTEM"
    assert second["how_discovered"] == "Visual"
    assert second["part_location"] == "ZONE 100"
    assert second["part_location_zone"] == "Fuselage Lower"


def test_codes_are_trimmed_and_uppercased_before_decoding():
    frame = raw().with_columns(
        pl.Series("HowDiscoveredCode", [" v ", "9"]),
        pl.Series("PartSinceCode", ["o", " "]),
        pl.Series("NatureOfConditionB", ["   ", "x"]),
        pl.Series("ReceivingRegionCode", ["sw", "GL"]),
    )
    first, second = processed(frame).iter_rows(named=True)
    assert (first["how_discovered_code"], first["how_discovered"]) == ("V", "Visual")
    assert (second["how_discovered_code"], second["how_discovered"]) == ("9", "Unknown")  # a digit code
    assert (first["part_since_code"], first["part_since"]) == ("O", "Overhaul")
    assert second["part_since_code"] is None  # blank -> null
    assert first["nature_of_condition_b_code"] is None
    assert (second["nature_of_condition_b_code"], second["nature_of_condition_b"]) == ("X", None)  # no label
    assert first["receiving_region_code"] == "SW"


def test_part_location_zones():
    frame = raw().with_columns(pl.Series("PartLocation", ["zn 200 fus top", "CABIN"]))
    first, second = processed(frame).iter_rows(named=True)
    assert first["part_location"] == "zn 200 fus top"  # the free text itself is kept
    assert (first["part_location_zone_code"], first["part_location_zone"]) == ("ZONE 200", "Fuselage Top")
    assert second["part_location_zone_code"] is None

    zones = pl.DataFrame({"p": ["ZONE 221", "ZONE 20", "ZONE", "ZONE 100 - FUSE", "OZONE 100"]})
    assert zones.select(faa_sdrs.zone_digit("p"))["p"].to_list() == ["2", None, None, "1", None]


def test_unparseable_values_become_null_and_are_counted():
    frame = raw().with_columns(
        pl.Series("AircraftTotalTime", ["12A", "100"]),
        pl.Series("DifficultyDate", ["13/45/2025", "01/02/2025"]),
    )
    typed, unparsed = faa_sdrs.type_columns(faa_sdrs.tidy(frame))
    assert typed["aircraft_total_time"].to_list() == [None, 100]
    assert typed["difficulty_date"].to_list() == [None, date(2025, 1, 2)]
    assert unparsed["aircraft_total_time"] == 1
    assert unparsed["difficulty_date"] == 1
    assert unparsed["engine_total_time"] == 0  # null to begin with is not a loss


def test_one_row_per_report_keeps_the_last_copy():
    frame = pl.concat([raw(), faa_sdrs.read_year(RAW, 2026)])
    out = processed(frame)
    assert out.height == 2
    assert out["file_year"].to_list() == [2026, 2026]


def test_label_coverage():
    frame = raw().with_columns(pl.Series("HowDiscoveredCode", ["V", "q"]))
    coverage = faa_sdrs.label_coverage(processed(frame))
    row = coverage.filter(pl.col("code") == "how_discovered_code").row(0, named=True)
    assert (row["coded"], row["labeled"], row["share"]) == (2, 1, 0.5)


def test_load_lookups(tmp_path: Path):
    for table in {code.table for code in (*faa_sdrs.CODES, faa_sdrs.ZONE_CODE)}:
        pl.DataFrame({"code": [" a "], "description": ["Label"]}).write_parquet(tmp_path / f"{table}.parquet")
    pl.DataFrame(
        {"code": ["2710", "2720"], "chapter": ["27", "27"], "chapter_title": ["FLIGHT CONTROLS"] * 2}
        | {"title": ["AILERON CONTROL SYSTEM", None], "reporting_instructions": [None, None]}
    ).write_parquet(tmp_path / "jasc.parquet")
    lookups = faa_sdrs.load_lookups(tmp_path)
    assert lookups["how_discovered"] == {"A": "Label"}  # codes normalized like the SDR codes
    assert lookups["jasc_title"] == {"2710": "AILERON CONTROL SYSTEM", "2720": None}
    assert lookups["jasc_chapter_title"] == {"27": "FLIGHT CONTROLS"}


def test_lookups_dir_picks_the_latest_version_not_after_the_snapshot(tmp_path: Path):
    root = tmp_path / "processed" / "faa-sdrs-lookups"
    for version in ("v2026-01-01", "v2026-10-23", "v2026-12-01"):
        (root / version).mkdir(parents=True)
    assert faa_sdrs.lookups_dir(tmp_path, "2026-10-24") == root / "v2026-10-23"
    with pytest.raises(FileNotFoundError):
        faa_sdrs.lookups_dir(tmp_path, "2025-01-01")
