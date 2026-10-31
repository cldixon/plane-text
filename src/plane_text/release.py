"""Releases: the processed corpus, frozen as a numbered, downloadable zip.

The pipelines keep refreshing processed/ in R2 under dated snapshots. A release
freezes one snapshot of every processed dataset as `plane-text-v<major>.<minor>`,
in two steps, so that what goes into a release is reviewed and kept in git:

1. `pin`: writes `releases/v<version>.json` in the repository, the newest
   snapshot of every dataset in R2 with each object's size and ETag. It goes
   in through a pull request; the diff against the previous pin shows what
   changed.
2. `build`: run by the Release workflow when the tag `v<version>` is pushed.
   It downloads exactly the pinned objects (refusing any that changed since
   they were pinned), zips them, and with `--upload` publishes the zip to
   `releases/plane-text-v<version>.zip` in R2 and points `releases/latest.json`
   at it. Beside the zip it writes the GitHub release notes and copies of the
   manifest and README.

    uv run python -m plane_text.release pin 1.1
    uv run python -m plane_text.release build releases/v1.1.json            # local, no upload
    uv run python -m plane_text.release build releases/v1.1.json --upload   # what the workflow runs

The zip:

    plane-text-v<version>/
      README.md       dataset card: contents, layout, provenance, license, citation
                      (its YAML front matter declares each table as a Hugging Face config)
      LICENSE         CC BY 4.0
      manifest.json   every data file: dataset, table, snapshot, rows, bytes, sha256, R2 key
      data/<dataset>/<table>.parquet

Parquet is already zstd-compressed, so it is stored in the zip as is. The same
tree, unzipped, is what a Hugging Face dataset repo holds. Versions only go up:
pinning or publishing a version at or below a published one is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import polars as pl

from plane_text import storage

NAME = "plane-text"
RELEASES = "releases"
LATEST = f"{RELEASES}/latest.json"
LICENSE = "CC BY 4.0"
LICENSE_ID = "cc-by-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
REPOSITORY = "https://github.com/cldixon/plane-text"
SITE = "https://plane-text.cldixon.dev"

# Pin files live here in the repository: releases/v<version>.json.
PINS = Path(__file__).resolve().parents[2] / RELEASES

# The processed datasets, in the order the README lists them, with a line on each.
# A dataset found in processed/ but missing here is still released, listed last.
DATASETS: dict[str, str] = {
    "ecfr-title14": "14 CFR (Aeronautics and Space) from the eCFR: one row per section or appendix.",
    "faa-ads": "FAA airworthiness directives (final rules) from DRS: one row per DRS document.",
    "faa-proposed-rules": "FAA AD proposed rules and NPRMs from DRS, with Federal Register text where found.",
    "faa-saibs": "FAA Special Airworthiness Information Bulletins: one row per bulletin with its text.",
    "faa-advisory-circulars": "FAA advisory circulars: one row per circular with its text.",
    "faa-maintenance-alerts": "FAA Aviation Maintenance Alerts (AC 43-16A), 2004-2012: one row per issue.",
    "faa-sdrs": "FAA Service Difficulty Reports, 1995 onward: one row per report, codes decoded.",
    "faa-sdrs-lookups": "The FAA SDRS code tables and the JASC codes, one table per code set.",
    "ntsb-narratives": "NTSB accident and incident narratives, 1982 onward: one row per aircraft per event.",
    "ntsb-findings": "NTSB findings and event sequences (2008 on), occurrences and sequences (1982-2007).",
    "ntsb-lookups": "The NTSB data dictionary and code tables.",
}

PROCESSED_KEY = re.compile(
    r"^processed/(?P<dataset>[^/]+)/v(?P<snapshot>\d{4}-\d{2}-\d{2})/(?P<table>[^/]+)\.parquet$"
)
VERSION = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)$")
RELEASE_KEY = re.compile(rf"^{RELEASES}/{NAME}-v(?P<version>\d+\.\d+)\.zip$")


def parse_version(text: str) -> str:
    """`1.0`, from `1.0` or the tag `v1.0`."""
    match = VERSION.match(text.strip())
    if not match:
        raise SystemExit(f"not a release version: {text!r} (expected <major>.<minor>, e.g. 1.0)")
    return f"{int(match['major'])}.{int(match['minor'])}"


def version_key(version: str) -> tuple[int, int]:
    major, minor = version.split(".")
    return int(major), int(minor)


def release_name(version: str) -> str:
    return f"{NAME}-v{version}"


def pin_path(version: str, pins: Path = PINS) -> Path:
    return pins / f"v{version}.json"


@dataclass(frozen=True)
class Table:
    dataset: str
    table: str
    snapshot: str
    #: Where the file sits in the release, under its top folder.
    path: str
    #: The processed key it came from, in R2 and under the data directory.
    key: str
    rows: int
    columns: int
    bytes: int
    sha256: str
    source: str | None
    source_url: str | None
    retrieved_at: str | None


def newest_snapshots(keys: Iterable[str]) -> list[str]:
    """The processed keys of each dataset's newest snapshot, in release order."""
    by_dataset: dict[str, dict[str, list[str]]] = {}
    for key in keys:
        match = PROCESSED_KEY.match(key)
        if match:
            by_dataset.setdefault(match["dataset"], {}).setdefault(match["snapshot"], []).append(key)
    order = [*DATASETS, *sorted(set(by_dataset) - set(DATASETS))]
    newest: list[str] = []
    for dataset in order:
        if dataset in by_dataset:
            snapshots = by_dataset[dataset]
            newest.extend(sorted(snapshots[max(snapshots)]))
    return newest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def describe(key: str, data_dir: Path) -> Table:
    match = PROCESSED_KEY.match(key)
    assert match, key
    path = data_dir / key
    provenance = storage.read_provenance(path)
    schema = pl.read_parquet_schema(path)
    rows = pl.scan_parquet(path).select(pl.len()).collect().item()
    return Table(
        dataset=match["dataset"],
        table=match["table"],
        snapshot=match["snapshot"],
        path=f"data/{match['dataset']}/{match['table']}.parquet",
        key=key,
        rows=rows,
        columns=len(schema),
        bytes=path.stat().st_size,
        sha256=sha256(path),
        source=provenance.get("source"),
        source_url=provenance.get("source_url"),
        retrieved_at=provenance.get("retrieved_at"),
    )


def published_versions() -> list[str]:
    found = (m["version"] for k in storage.list_objects(f"{RELEASES}/") if (m := RELEASE_KEY.match(k)))
    return sorted(found, key=version_key)


def refuse_if_published(version: str) -> None:
    published = published_versions()
    if published and version_key(version) <= version_key(published[-1]):
        raise SystemExit(
            f"v{version} refused: v{published[-1]} is already published, and versions only go up"
        )


# --- pin ---------------------------------------------------------------------------------------------


def pin(version: str, pins: Path = PINS) -> Path:
    """Writes releases/v<version>.json: the newest snapshot of every processed dataset in R2."""
    path = pin_path(version, pins)
    if path.exists():
        raise SystemExit(f"{path} already exists")
    refuse_if_published(version)
    objects = storage.list_objects(f"{storage.PROCESSED}/")
    keys = newest_snapshots(objects)
    if not keys:
        raise SystemExit("nothing to pin: no processed datasets in R2")
    document = {
        "release": release_name(version),
        "version": version,
        "pinned_at": storage.now(),
        "files": [{"key": k, "bytes": objects[k].bytes, "etag": objects[k].etag} for k in keys],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")
    print(f"pinned {len(keys)} files to {path}")
    return path


def read_pin(path: Path) -> tuple[str, list[dict]]:
    """A pin file's version and files, checked against its name."""
    document = json.loads(path.read_text())
    version = parse_version(document["version"])
    if path.name != f"v{version}.json":
        raise SystemExit(f"{path} pins version {version}; it must be named v{version}.json")
    pinned = document["files"]
    bad = [f["key"] for f in pinned if not PROCESSED_KEY.match(f["key"])]
    if bad or not pinned:
        raise SystemExit(f"{path}: not processed parquet keys: {bad or 'no files'}")
    tables = [(m["dataset"], m["table"]) for f in pinned if (m := PROCESSED_KEY.match(f["key"]))]
    if len(set(tables)) != len(tables):
        raise SystemExit(f"{path}: a table is pinned twice")
    return version, pinned


# --- build -------------------------------------------------------------------------------------------


def fetch(pinned: list[dict], data_dir: Path) -> None:
    """Downloads every pinned object, after checking R2 still holds it as pinned."""
    objects = storage.list_objects(f"{storage.PROCESSED}/")
    changed = [
        f["key"]
        for f in pinned
        if f["key"] not in objects
        or (objects[f["key"]].bytes, objects[f["key"]].etag) != (f["bytes"], f["etag"])
    ]
    if changed:
        raise SystemExit(
            "pinned objects are missing or changed in R2 since they were pinned:\n  " + "\n  ".join(changed)
        )
    for f in pinned:
        path = data_dir / f["key"]
        if path.exists() and path.stat().st_size == f["bytes"]:
            continue
        storage.download(f["key"], data_dir)
        print(f"downloaded {f['key']} ({f['bytes'] / 1e6:.1f} MB)")


def config_name(table: Table, tables: list[Table]) -> str:
    """A table's Hugging Face config: the dataset's name, or `<dataset>-<table>` when it has several."""
    siblings = sum(t.dataset == table.dataset for t in tables)
    return table.dataset if siblings == 1 else f"{table.dataset}-{table.table}"


def size_category(rows: int) -> str:
    bounds = [(1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"), (1_000_000, "100K<n<1M")]
    bounds += [(10_000_000, "1M<n<10M"), (100_000_000, "10M<n<100M")]
    return next((label for bound, label in bounds if rows < bound), "n>100M")


def readme(tables: list[Table], version: str, date: str) -> str:
    name = release_name(version)
    rows = sum(t.rows for t in tables)
    size = sum(t.bytes for t in tables)
    datasets = list(dict.fromkeys(t.dataset for t in tables))

    configs = "\n".join(
        f"  - config_name: {config_name(t, tables)}\n"
        f"    data_files:\n      - split: train\n        path: {t.path}"
        for t in tables
    )
    front_matter = f"""---
pretty_name: Plane-text
license: {LICENSE_ID}
language:
  - en
tags:
  - aviation
  - maintenance
  - regulations
  - airworthiness
  - safety
size_categories:
  - {size_category(rows)}
configs:
{configs}
---"""

    contents = "\n".join(
        f"| `{t.dataset}` | `{t.table}` | {t.rows:,} | {t.columns} | {t.snapshot} | `{t.path}` |"
        for t in tables
    )
    about = "\n".join(f"- `{d}`" + (f": {DATASETS[d]}" if d in DATASETS else "") for d in datasets)
    sources = "\n".join(
        f"- `{d}`: {t.source}" + (f" ({t.source_url})" if t.source_url else "")
        for d in datasets
        if (t := next(t for t in tables if t.dataset == d)).source
    )
    tree = "\n".join(
        [f"{name}/", "  README.md", "  LICENSE", "  manifest.json", "  data/"]
        + [
            f"    {d}/" + "".join(f"\n      {t.table}.parquet" for t in tables if t.dataset == d)
            for d in datasets
        ]
    )
    first = tables[0]

    return f"""{front_matter}

# Plane-text

An open corpus of aviation maintenance text, built only from public sources: the regulations that
govern maintenance, the airworthiness directives that order inspections and repairs, the bulletins and
advisory circulars that explain them, and the reports mechanics and investigators write when something
goes wrong. Gathered from the FAA, the eCFR and the NTSB, cleaned, and published as parquet.

## Release

| | |
| --- | --- |
| Release | `{name}` |
| Date | {date} |
| Datasets | {len(datasets)} |
| Tables | {len(tables)} |
| Rows | {rows:,} |
| Size | {size / 1e6:,.0f} MB of parquet |
| License | [{LICENSE}]({LICENSE_URL}) |

A release freezes one snapshot of every dataset, pinned in
[`releases/v{version}.json`]({REPOSITORY}/blob/v{version}/releases/v{version}.json).
Each dataset keeps its own snapshot date (the day its pipeline ran, or for the eCFR the issue date),
listed below and in `manifest.json`.

## Datasets

{about}

| Dataset | Table | Rows | Columns | Snapshot | File |
| --- | --- | ---: | ---: | --- | --- |
{contents}

## Layout

```
{tree}
```

`manifest.json` lists every data file with its dataset, table, snapshot, row and column counts, size,
SHA-256 and the key it was built under in the plane-text bucket.

## Format

- One parquet file per table, zstd-compressed. One row per real-world entity: a CFR section, an AD, a report.
- Every code column `x_code` is followed by its label `x`. A code with no entry in the lookups keeps its
  code and gets a null label; the lookup tables themselves are included (`*-lookups`).
- Values that failed to parse when typed are null; their original text is kept in the raw layer, which is
  not part of the release (see the repository).
- Each file carries its provenance as `plane_text.*` key-value metadata: `source`, `source_url`, `snapshot`,
  `retrieved_at`, `built_from` and `built_at`, and where computed `unparsed` and `label_coverage`.
- Personal data: NTSB owner and operator names and street addresses are withheld.

## Loading

```python
import polars as pl

df = pl.read_parquet("{name}/{first.path}")
```

```sql
-- DuckDB
SELECT * FROM '{name}/{first.path}' LIMIT 10;
```

## Sources

{sources}

## License

The plane-text compilation (the selection, cleaning, structure and decoding of these datasets) is
released under [{LICENSE}]({LICENSE_URL}); see `LICENSE`. Attribute it as "Plane-text, {name}" with a
link to {SITE}.

The underlying texts are works of the US federal government (FAA, NTSB, eCFR), which are generally not
subject to copyright in the United States. Material by third parties quoted or referenced within them,
such as manufacturer service bulletins named in airworthiness directives, may not share that status.

## Citation

```bibtex
@misc{{plane_text_v{version.replace(".", "_")},
  title  = {{Plane-text: an open corpus of aviation maintenance text}},
  author = {{Dixon, C. L.}},
  year   = {{{date[:4]}}},
  note   = {{Release v{version}, {date}}},
  url    = {{{SITE}}}
}}
```

## Links

- Site: {SITE}
- Code and pipelines: {REPOSITORY}
"""


def manifest(tables: list[Table], version: str, date: str, built_at: str) -> dict:
    return {
        "name": NAME,
        "release": release_name(version),
        "version": version,
        "date": date,
        "built_at": built_at,
        "license": LICENSE_ID,
        "rows": sum(t.rows for t in tables),
        "bytes": sum(t.bytes for t in tables),
        "files": [asdict(t) for t in tables],
    }


def notes(tables: list[Table], version: str, date: str, archive: Path, digest: str) -> str:
    """The GitHub release notes: what is in the release, and where to get it."""
    name = release_name(version)
    url = f"{SITE}/download/{archive.name}"
    rows = sum(t.rows for t in tables)
    lines = [
        f"**[Download {archive.name}]({url})** ({archive.stat().st_size / 1e6:,.0f} MB)",
        "",
        f"`{name}`, {date}: {len({t.dataset for t in tables})} datasets, {len(tables)} tables, "
        f"{rows:,} rows, as parquet. [{LICENSE}]({LICENSE_URL}).",
        "",
        "| Dataset | Tables | Rows | Snapshot |",
        "| --- | ---: | ---: | --- |",
    ]
    for dataset in dict.fromkeys(t.dataset for t in tables):
        group = [t for t in tables if t.dataset == dataset]
        snapshots = ", ".join(sorted({t.snapshot for t in group}))
        lines.append(f"| `{dataset}` | {len(group)} | {sum(t.rows for t in group):,} | {snapshots} |")
    lines += [
        "",
        f"Pinned in [`releases/v{version}.json`]({REPOSITORY}/blob/v{version}/releases/v{version}.json). "
        "The attached manifest lists every file with its SHA-256; the README is the dataset card in the zip.",
        "",
        f"SHA-256 of the zip: `{digest}`",
        "",
    ]
    return "\n".join(lines)


def write_zip(tables: list[Table], data_dir: Path, version: str, date: str, out: Path) -> dict:
    """Writes the release zip to `out`; returns its manifest."""
    name = release_name(version)
    out.parent.mkdir(parents=True, exist_ok=True)
    license_text = files("plane_text").joinpath("licenses/CC-BY-4.0.txt").read_text(encoding="utf-8")
    contents = manifest(tables, version, date, storage.now())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}/README.md", readme(tables, version, date))
        archive.writestr(f"{name}/LICENSE", license_text)
        archive.writestr(f"{name}/manifest.json", json.dumps(contents, indent=2) + "\n")
        for table in tables:
            archive.write(data_dir / table.key, f"{name}/{table.path}", compress_type=zipfile.ZIP_STORED)
    return contents


def build(pin_file: Path, data_dir: Path, date: str, upload: bool) -> Path:
    """Builds the release a pin file names and, with `upload`, publishes it to R2.

    Beside the zip, under <data_dir>/releases/, it writes `<name>.notes.md` (the
    GitHub release notes), `<name>.manifest.json` and `<name>.README.md`.
    """
    version, pinned = read_pin(pin_file)
    name = release_name(version)
    if upload:
        refuse_if_published(version)
    fetch(pinned, data_dir)

    tables = [describe(f["key"], data_dir) for f in pinned]
    for t in tables:
        print(f"{t.path:<55} {t.rows:>10,} rows  v{t.snapshot}")
    out_dir = data_dir / RELEASES
    archive = out_dir / f"{name}.zip"
    contents = write_zip(tables, data_dir, version, date, archive)
    digest = sha256(archive)
    print(f"wrote {archive} ({archive.stat().st_size / 1e6:.1f} MB)")

    (out_dir / f"{name}.manifest.json").write_text(json.dumps(contents, indent=2) + "\n")
    (out_dir / f"{name}.README.md").write_text(readme(tables, version, date))
    (out_dir / f"{name}.notes.md").write_text(notes(tables, version, date, archive, digest))
    latest = data_dir / LATEST
    pointer = {
        "release": name,
        "version": version,
        "date": date,
        "key": storage.object_key(archive, data_dir),
        "file": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": digest,
    }
    latest.write_text(json.dumps(pointer, indent=2) + "\n")
    if upload:
        # The zip first: latest.json must never point at a release that is not there yet.
        print(f"uploaded to r2://{storage.bucket()}/{storage.upload(archive, data_dir, 'application/zip')}")
        print(f"uploaded to r2://{storage.bucket()}/{storage.upload(latest, data_dir, 'application/json')}")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pin_parser = commands.add_parser("pin", help="pin the newest snapshots in R2 as releases/v<version>.json")
    pin_parser.add_argument("version", help="<major>.<minor>, e.g. 1.1")

    build_parser = commands.add_parser("build", help="build (and with --upload, publish) a pinned release")
    build_parser.add_argument("pin", type=Path, help="the pin file, releases/v<version>.json")
    build_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    build_parser.add_argument(
        "--date",
        default=datetime.now(UTC).date().isoformat(),
        help="the release date (YYYY-MM-DD); default today",
    )
    build_parser.add_argument("--upload", action="store_true", help="publish the release to R2")

    args = parser.parse_args()
    if args.command == "pin":
        pin(parse_version(args.version))
    else:
        build(args.pin, args.data_dir, args.date, args.upload)


if __name__ == "__main__":
    main()
