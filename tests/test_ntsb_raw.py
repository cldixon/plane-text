import json
import zipfile
from pathlib import Path

import polars as pl
import pytest

from plane_text import ntsb_raw, storage

# The shape of `mdb-export -T %Y-%m-%dT%H:%M:%S` output: a quoted memo with
# Windows line breaks and padding, an empty field, and codes with leading zeros.
EXPORT = (
    b"ev_id,Aircraft_Key,narr_accp,far_part,lchg_date\n"
    b'"20080107X00026",1,"  On January 5, 2008...  \r\n\r\nThe pilot said.  ","091",2024-01-01T00:00:00\n'
    b'"20080107X00026",2,,"091F",\n'
)


def test_parse_export_keeps_the_table_as_delivered():
    frame = ntsb_raw.parse_export(EXPORT)
    assert frame.columns == ["ev_id", "Aircraft_Key", "narr_accp", "far_part", "lchg_date"]
    assert all(dtype == pl.String for dtype in frame.schema.values())
    first, second = frame.iter_rows(named=True)
    assert first["narr_accp"] == "  On January 5, 2008...  \r\n\r\nThe pilot said.  "  # not cleaned
    assert first["far_part"] == "091"  # not cast
    assert first["Aircraft_Key"] == "1"
    assert first["lchg_date"] == "2024-01-01T00:00:00"
    assert second["narr_accp"] is None and second["lchg_date"] is None


def test_withhold_drops_owner_and_operator_names_and_addresses():
    aircraft = pl.DataFrame(
        {
            "ev_id": ["x"],
            "regis_no": ["N1"],
            "owner_acft": ["A Person"],
            "owner_street": ["1 Main St"],
            "oper_name": ["A Person"],
            "oper_individual_name": ["Y"],
        }
    )
    frame, dropped = ntsb_raw.withhold(aircraft, "aircraft")
    assert dropped == ["owner_acft", "owner_street", "oper_name"]
    assert frame.columns == ["ev_id", "regis_no", "oper_individual_name"]
    events = pl.DataFrame({"ev_id": ["x"], "ev_city": ["Spotsylvania"]})
    kept, dropped = ntsb_raw.withhold(events, "events")
    assert kept.equals(events) and dropped == []


def test_cache_and_raw_paths(tmp_path: Path):
    assert (
        ntsb_raw.cache_path(tmp_path, "2026-10-23", "avall") == tmp_path / "cache/ntsb/2026-10-23/avall.zip"
    )
    assert (
        ntsb_raw.cache_path(tmp_path, "2026-10-23", "Pre2008") == tmp_path / "cache/ntsb/static/Pre2008.zip"
    )
    path = ntsb_raw.raw_path(tmp_path, "2026-10-23", "Pre2008", "seq_of_events")
    assert storage.object_key(path, tmp_path) == "raw/ntsb/v2026-10-23/Pre2008-seq_of_events.parquet"


def test_fetch_reuses_the_cache(tmp_path: Path):
    class NoHTTP:
        def get(self, url: str):
            raise AssertionError("a cached database must not be downloaded")

    path = tmp_path / "avall.zip"
    path.write_bytes(b"zip")
    assert ntsb_raw.fetch(NoHTTP(), "avall", path) == path  # ty: ignore[invalid-argument-type]


def test_build_raw_writes_each_table_per_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for database in ntsb_raw.DATABASES:
        path = ntsb_raw.cache_path(tmp_path, "2026-10-23", database)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(zipfile.ZipInfo(f"{database}.mdb", (2026, 10, 1, 7, 36, 0)), b"not really access")

    def export_table(mdb: Path, table: str) -> pl.DataFrame:
        assert mdb.name in ("avall.mdb", "Pre2008.mdb")
        return pl.DataFrame({"ev_id": ["x"], "owner_street": ["1 Main St"], "damage": ["SUBS"]})

    monkeypatch.setattr(ntsb_raw, "require_mdbtools", lambda: None)
    monkeypatch.setattr(ntsb_raw, "export_table", export_table)
    paths = ntsb_raw.build_raw(tmp_path, "2026-10-23", ("aircraft", "events"))
    keys = [storage.object_key(p, tmp_path) for p in paths]
    assert keys == [
        "raw/ntsb/v2026-10-23/avall-aircraft.parquet",
        "raw/ntsb/v2026-10-23/avall-events.parquet",
        "raw/ntsb/v2026-10-23/Pre2008-aircraft.parquet",
        "raw/ntsb/v2026-10-23/Pre2008-events.parquet",
    ]
    assert pl.read_parquet(paths[0]).columns == ["ev_id", "damage"]
    assert pl.read_parquet(paths[1]).columns == ["ev_id", "owner_street", "damage"]  # only aircraft withholds
    provenance = storage.read_provenance(paths[0])
    assert provenance["dataset"] == "ntsb"
    assert provenance["layer"] == "raw"
    assert (provenance["database"], provenance["table"]) == ("avall", "aircraft")
    assert provenance["source_file"] == "avall.zip/avall.mdb"
    assert provenance["source_date"] == "2026-10-01"
    assert provenance["export"] == "mdb-export -T %Y-%m-%dT%H:%M:%S"
    assert json.loads(provenance["withheld_columns"]) == ["owner_street"]
    assert "withheld_columns" not in storage.read_provenance(paths[1])

    processed = ntsb_raw.processed_provenance(
        tmp_path, ntsb_raw.sources(tmp_path, "2026-10-23", ("events",)), "ntsb-x", "t", extra="1"
    )
    assert processed["dataset"] == "ntsb-x"
    assert processed["layer"] == "processed"
    assert processed["database"] == "avall, Pre2008"
    assert processed["source"] == "NTSB downloadable aviation databases (avall.zip, Pre2008.zip)"
    assert processed["source_date"] == "avall: 2026-10-01, Pre2008: 2026-10-01"
    assert processed["built_from"] == (
        "raw/ntsb/v2026-10-23/avall-events.parquet, raw/ntsb/v2026-10-23/Pre2008-events.parquet"
    )
    assert processed["extra"] == "1"
