from pathlib import Path

import polars as pl

from plane_text import storage


def test_write_parquet_round_trips_rows_and_provenance(tmp_path: Path):
    frame = pl.DataFrame({"identifier": ["39.1"], "text": ["Purpose."]})
    path = tmp_path / "ecfr-title14" / "v2026-10-21" / "sections.parquet"

    storage.write_parquet(frame, path, provenance={"dataset": "ecfr-title14", "issue_date": "2026-10-21"})

    assert pl.read_parquet(path).equals(frame)
    assert storage.read_provenance(path) == {"dataset": "ecfr-title14", "issue_date": "2026-10-21"}


def test_object_key_mirrors_data_dir(tmp_path: Path):
    path = tmp_path / "ecfr-title14" / "v2026-10-21" / "sections.parquet"
    assert storage.object_key(path, tmp_path) == "ecfr-title14/v2026-10-21/sections.parquet"


def test_bucket_override(monkeypatch):
    monkeypatch.delenv("PLANETEXT_BUCKET", raising=False)
    assert storage.bucket() == "plane-text"
    monkeypatch.setenv("PLANETEXT_BUCKET", "other")
    assert storage.bucket() == "other"
