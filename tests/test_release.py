import json
import zipfile
from pathlib import Path

import polars as pl
import pytest

from plane_text import release, storage


def test_newest_snapshots_takes_each_datasets_latest_in_release_order():
    keys = [
        "processed/ntsb-lookups/v2026-10-23/codes.parquet",
        "processed/faa-ads/v2026-10-23/ad-final-rules.parquet",
        "processed/faa-ads/v2026-10-27/ad-final-rules.parquet",
        "processed/ecfr-title14/v2026-10-15/sections.parquet",
        "processed/zz-new/v2026-10-01/t.parquet",
        "processed/faa-proposed-rules/v2026-10-27/proposed-rules.parquet",
        "processed/faa-proposed-rules/v2026-10-27/ad-proposed-rules.parquet",
        "processed/faa-proposed-rules/v2026-10-24/proposed-rules.parquet",
        "raw/faa-ads/v2026-10-27/ad-final-rules.parquet",
        "processed/faa-ads/v2026-10-27/notes.txt",
    ]
    assert release.newest_snapshots(keys) == [
        "processed/ecfr-title14/v2026-10-15/sections.parquet",
        "processed/faa-ads/v2026-10-27/ad-final-rules.parquet",
        "processed/faa-proposed-rules/v2026-10-27/ad-proposed-rules.parquet",
        "processed/faa-proposed-rules/v2026-10-27/proposed-rules.parquet",
        "processed/ntsb-lookups/v2026-10-23/codes.parquet",
        "processed/zz-new/v2026-10-01/t.parquet",
    ]


def write(data_dir: Path, dataset: str, snapshot: str, table: str, frame: pl.DataFrame) -> None:
    path = storage.layer_path(data_dir, storage.PROCESSED, dataset, snapshot, table)
    storage.write_parquet(
        frame, path, {"dataset": dataset, "source": f"{dataset} source", "snapshot": snapshot}
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write(tmp_path, "faa-ads", "2026-10-23", "ad-final-rules", pl.DataFrame({"ad": ["old"]}))
    write(tmp_path, "faa-ads", "2026-10-27", "ad-final-rules", pl.DataFrame({"ad": ["a", "b"], "x": [1, 2]}))
    write(tmp_path, "ntsb-findings", "2026-10-27", "findings", pl.DataFrame({"ev_id": ["e1"]}))
    write(tmp_path, "ntsb-findings", "2026-10-27", "occurrences", pl.DataFrame({"ev_id": ["e1", "e2", "e3"]}))
    return tmp_path


def test_local_release_layout_and_contents(data_dir: Path):
    out = release.run(data_dir, version=2, date="2026-10-31", source="local", upload=False)

    assert out == data_dir / "releases" / "plane-text-v2.zip"
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == [
            "plane-text-v2/LICENSE",
            "plane-text-v2/README.md",
            "plane-text-v2/data/faa-ads/ad-final-rules.parquet",
            "plane-text-v2/data/ntsb-findings/findings.parquet",
            "plane-text-v2/data/ntsb-findings/occurrences.parquet",
            "plane-text-v2/manifest.json",
        ]
        info = archive.getinfo("plane-text-v2/data/faa-ads/ad-final-rules.parquet")
        assert info.compress_type == zipfile.ZIP_STORED
        ads = pl.read_parquet(archive.read("plane-text-v2/data/faa-ads/ad-final-rules.parquet"))
        manifest = json.loads(archive.read("plane-text-v2/manifest.json"))
        readme = archive.read("plane-text-v2/README.md").decode()
        license_text = archive.read("plane-text-v2/LICENSE").decode()

    assert ads["ad"].to_list() == ["a", "b"]  # the newest snapshot only
    assert manifest["release"] == "plane-text-v2"
    assert manifest["rows"] == 6
    ads_entry = manifest["files"][0]
    assert ads_entry["key"] == "processed/faa-ads/v2026-10-27/ad-final-rules.parquet"
    assert (ads_entry["snapshot"], ads_entry["rows"], ads_entry["columns"]) == ("2026-10-27", 2, 2)
    assert ads_entry["source"] == "faa-ads source"
    assert len(ads_entry["sha256"]) == 64

    assert readme.startswith("---\npretty_name: Plane-text\nlicense: cc-by-4.0\n")
    # One table: the dataset's name; several: dataset-table.
    assert "config_name: faa-ads\n" in readme
    assert "config_name: ntsb-findings-occurrences\n" in readme
    assert "path: data/ntsb-findings/findings.parquet" in readme
    assert license_text.startswith("Creative Commons Attribution 4.0 International")

    latest = json.loads((data_dir / "releases" / "latest.json").read_text())
    assert latest["key"] == "releases/plane-text-v2.zip"
    assert latest["file"] == "plane-text-v2.zip"
    assert latest["bytes"] == out.stat().st_size


def test_publishing_refuses_a_release_number_already_used(data_dir: Path, monkeypatch):
    monkeypatch.setattr(release, "published_versions", lambda: [1, 2])
    with pytest.raises(SystemExit, match="v2 is already published"):
        release.run(data_dir, version=2, date="2026-10-31", source="local", upload=True)


def test_size_category():
    assert release.size_category(999) == "n<1K"
    assert release.size_category(2_000_000) == "1M<n<10M"
