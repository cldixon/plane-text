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


def test_layer_path_and_cache_dir(tmp_path: Path):
    path = storage.layer_path(tmp_path, storage.RAW, "faa-sdrs", "2026-10-23", "service-difficulty-reports")
    assert storage.object_key(path, tmp_path) == "raw/faa-sdrs/v2026-10-23/service-difficulty-reports.parquet"
    assert storage.cache_dir(tmp_path, "faa-sdrs") == tmp_path / "cache" / "faa-sdrs"


def test_derived_provenance_carries_the_source_forward(tmp_path: Path):
    raw = storage.layer_path(tmp_path, storage.RAW, "faa-ads", "2026-10-23", "ad-final-rules")
    storage.write_parquet(
        pl.DataFrame({"a": [1]}), raw, {"dataset": "faa-ads", "layer": "raw", "snapshot": "x"}
    )
    provenance = storage.derived_provenance(raw, tmp_path, table="t")
    assert provenance["dataset"] == "faa-ads"
    assert provenance["snapshot"] == "x"
    assert provenance["layer"] == "processed"
    assert provenance["built_from"] == "raw/faa-ads/v2026-10-23/ad-final-rules.parquet"
    assert provenance["table"] == "t"
    assert "built_at" in provenance
