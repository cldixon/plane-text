import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import polars as pl
import pytest

from plane_text import release, storage

REPOSITORY_PINS = Path(__file__).resolve().parents[1] / "releases"


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


def test_versions():
    assert release.parse_version("v1.0") == "1.0"
    assert release.parse_version("2.10") == "2.10"
    assert release.version_key("1.10") > release.version_key("1.9")
    for bad in ["1", "v1", "1.0.0", "latest"]:
        with pytest.raises(SystemExit):
            release.parse_version(bad)
    assert release.RELEASE_KEY.match("releases/plane-text-v1.0.zip")
    assert not release.RELEASE_KEY.match("releases/plane-text-v1.zip")


class FakeBucket:
    """R2 as a directory: list_objects, download and upload work on files under `root`."""

    def __init__(self, root: Path):
        self.root = root

    def list_objects(self, prefix: str) -> dict[str, storage.Stored]:
        found = {}
        for path in self.root.rglob("*"):
            key = path.relative_to(self.root).as_posix()
            if path.is_file() and key.startswith(prefix):
                found[key] = storage.Stored(path.stat().st_size, hashlib.md5(path.read_bytes()).hexdigest())
        return found

    def download(self, key: str, data_dir: Path) -> Path:
        path = data_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.root / key, path)
        return path

    def upload(self, path: Path, data_dir: Path, content_type: str | None = None) -> str:
        key = storage.object_key(path, data_dir)
        (self.root / key).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, self.root / key)
        return key


def put(root: Path, dataset: str, snapshot: str, table: str, frame: pl.DataFrame) -> None:
    path = storage.layer_path(root, storage.PROCESSED, dataset, snapshot, table)
    provenance = {"dataset": dataset, "source": f"{dataset} source", "snapshot": snapshot}
    storage.write_parquet(frame, path, provenance)


@pytest.fixture
def bucket(tmp_path: Path, monkeypatch) -> FakeBucket:
    root = tmp_path / "r2"
    put(root, "faa-ads", "2026-10-23", "ad-final-rules", pl.DataFrame({"ad": ["old"]}))
    put(root, "faa-ads", "2026-10-27", "ad-final-rules", pl.DataFrame({"ad": ["a", "b"], "x": [1, 2]}))
    put(root, "ntsb-findings", "2026-10-27", "findings", pl.DataFrame({"ev_id": ["e1"]}))
    put(root, "ntsb-findings", "2026-10-27", "occurrences", pl.DataFrame({"ev_id": ["e1", "e2", "e3"]}))
    fake = FakeBucket(root)
    monkeypatch.setattr(storage, "list_objects", fake.list_objects)
    monkeypatch.setattr(storage, "download", fake.download)
    monkeypatch.setattr(storage, "upload", fake.upload)
    return fake


def test_pin_records_the_newest_snapshots_with_size_and_etag(bucket: FakeBucket, tmp_path: Path):
    path = release.pin("1.0", pins=tmp_path / "pins")

    assert path.name == "v1.0.json"
    pinned = json.loads(path.read_text())
    assert pinned["release"] == "plane-text-v1.0"
    assert [f["key"] for f in pinned["files"]] == [
        "processed/faa-ads/v2026-10-27/ad-final-rules.parquet",
        "processed/ntsb-findings/v2026-10-27/findings.parquet",
        "processed/ntsb-findings/v2026-10-27/occurrences.parquet",
    ]
    stored = bucket.list_objects("processed/")["processed/faa-ads/v2026-10-27/ad-final-rules.parquet"]
    assert (pinned["files"][0]["bytes"], pinned["files"][0]["etag"]) == (stored.bytes, stored.etag)
    with pytest.raises(SystemExit, match="already exists"):
        release.pin("1.0", pins=tmp_path / "pins")


def test_build_and_publish_a_pinned_release(bucket: FakeBucket, tmp_path: Path):
    pin = release.pin("1.0", pins=tmp_path / "pins")
    # A newer snapshot after pinning is not picked up: the pin decides.
    put(bucket.root, "faa-ads", "2026-10-30", "ad-final-rules", pl.DataFrame({"ad": ["new"]}))
    data_dir = tmp_path / "data"

    out = release.build(pin, data_dir, date="2026-10-31", upload=True)

    assert out == data_dir / "releases" / "plane-text-v1.0.zip"
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == [
            "plane-text-v1.0/LICENSE",
            "plane-text-v1.0/README.md",
            "plane-text-v1.0/data/faa-ads/ad-final-rules.parquet",
            "plane-text-v1.0/data/ntsb-findings/findings.parquet",
            "plane-text-v1.0/data/ntsb-findings/occurrences.parquet",
            "plane-text-v1.0/manifest.json",
        ]
        info = archive.getinfo("plane-text-v1.0/data/faa-ads/ad-final-rules.parquet")
        assert info.compress_type == zipfile.ZIP_STORED
        ads = pl.read_parquet(archive.read("plane-text-v1.0/data/faa-ads/ad-final-rules.parquet"))
        manifest = json.loads(archive.read("plane-text-v1.0/manifest.json"))
        readme = archive.read("plane-text-v1.0/README.md").decode()
        license_text = archive.read("plane-text-v1.0/LICENSE").decode()

    assert ads["ad"].to_list() == ["a", "b"]
    assert manifest["release"] == "plane-text-v1.0"
    assert manifest["rows"] == 6
    ads_entry = manifest["files"][0]
    assert ads_entry["key"] == "processed/faa-ads/v2026-10-27/ad-final-rules.parquet"
    assert (ads_entry["snapshot"], ads_entry["rows"], ads_entry["columns"]) == ("2026-10-27", 2, 2)
    assert ads_entry["source"] == "faa-ads source"

    assert readme.startswith("---\npretty_name: Plane-text\nlicense: cc-by-4.0\n")
    assert "config_name: faa-ads\n" in readme  # one table: the dataset's name
    assert "config_name: ntsb-findings-occurrences\n" in readme  # several: dataset-table
    assert "@misc{plane_text_v1_0," in readme
    assert license_text.startswith("Creative Commons Attribution 4.0 International")

    releases = data_dir / "releases"
    notes = (releases / "plane-text-v1.0.notes.md").read_text()
    assert "https://plane-text.cldixon.dev/download/plane-text-v1.0.zip" in notes
    assert "| `ntsb-findings` | 2 | 4 | 2026-10-27 |" in notes
    assert json.loads((releases / "plane-text-v1.0.manifest.json").read_text()) == manifest
    assert (releases / "plane-text-v1.0.README.md").read_text() == readme

    # Published: the zip and the pointer to it.
    assert (bucket.root / "releases" / "plane-text-v1.0.zip").read_bytes() == out.read_bytes()
    latest = json.loads((bucket.root / "releases" / "latest.json").read_text())
    assert (latest["file"], latest["bytes"]) == ("plane-text-v1.0.zip", out.stat().st_size)

    # Versions only go up.
    with pytest.raises(SystemExit, match=r"v1\.0 is already published"):
        release.build(pin, data_dir, date="2026-10-31", upload=True)
    with pytest.raises(SystemExit, match="already published"):
        release.pin("0.9", pins=tmp_path / "pins")


def test_build_refuses_objects_changed_since_pinned(bucket: FakeBucket, tmp_path: Path):
    pin = release.pin("1.0", pins=tmp_path / "pins")
    # A pipeline re-run with the same snapshot date overwrites the key.
    put(bucket.root, "faa-ads", "2026-10-27", "ad-final-rules", pl.DataFrame({"ad": ["rewritten"]}))
    with pytest.raises(SystemExit, match="changed in R2 since they were pinned"):
        release.build(pin, tmp_path / "data", date="2026-10-31", upload=False)


def test_pin_files_in_the_repository_are_well_formed():
    for path in sorted(REPOSITORY_PINS.glob("*.json")):
        _, pinned = release.read_pin(path)
        assert all(f["bytes"] > 0 and f["etag"] for f in pinned), path


def test_size_category():
    assert release.size_category(999) == "n<1K"
    assert release.size_category(2_000_000) == "1M<n<10M"
