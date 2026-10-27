"""The raw layer shared by the NTSB pipelines: the NTSB's Access tables, exported as delivered.

The NTSB publishes its aviation database as Microsoft Access files:
`avall.zip` (2008 to present, refreshed monthly) and `Pre2008.zip`
(1982-2007, static). The narratives, lookups and findings pipelines all read
them, so they share one download cache and one raw dataset:

- cache: `cache/ntsb/<snapshot>/avall.zip` and `cache/ntsb/static/Pre2008.zip`.
- raw: `raw/ntsb/v<snapshot>/<database>-<table>.parquet`, one file per Access
  table per database (`avall-events`, `Pre2008-events`, ...), each pipeline
  writing the tables it reads. The NTSB's own table and column names, every
  value as text as `mdb-export` writes it (dates as ISO 8601), nothing
  cleaned, decoded or dropped, with one exception: the names and addresses of
  aircraft owners and operators in `aircraft` are withheld at raw, so they are
  never written out or published, and the file's `plane_text.withheld_columns`
  metadata lists them. The cached zip still holds them.

Reading the Access files needs `mdb-export` from mdbtools (`apt-get install mdbtools`).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import storage

DATASET = "ntsb"
DOWNLOAD = "https://data.ntsb.gov/avdata/FileDirectory/DownloadFile?fileID={name}"
PAGE = "https://data.ntsb.gov/avdata/"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"
# Dates and times as ISO 8601 rather than mdb-export's locale default.
EXPORT_OPTIONS = ("-T", "%Y-%m-%dT%H:%M:%S")

# Newest first: when a key appears in both, processed tables keep the newer database's row.
DATABASES = ("avall", "Pre2008")

# Personal data withheld from raw: the names and addresses of aircraft owners
# and operators (often private individuals). Flags such as
# `oper_individual_name` and operator certificate details stay.
WITHHELD = {
    "aircraft": (
        "owner_acft",
        "owner_street",
        "owner_city",
        "owner_state",
        "owner_country",
        "owner_zip",
        "oper_name",
        "oper_dba",
        "oper_street",
        "oper_city",
        "oper_state",
        "oper_country",
        "oper_zip",
    ),
}


# --- fetching ---------------------------------------------------------------


def cache_path(data_dir: Path, snapshot: str, database: str) -> Path:
    """avall changes monthly, so its cache is per snapshot; Pre2008 is static."""
    folder = snapshot if database == "avall" else "static"
    return storage.cache_dir(data_dir, DATASET) / folder / f"{database}.zip"


def fetch(http: httpx.Client, name: str, path: Path) -> Path:
    if not path.exists():
        response = http.get(DOWNLOAD.format(name=f"{name}.zip"))
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    return path


def require_mdbtools() -> None:
    if shutil.which("mdb-export") is None:
        raise SystemExit("mdb-export not found: install mdbtools (apt-get install mdbtools)")


# --- exporting --------------------------------------------------------------


def parse_export(csv: bytes) -> pl.DataFrame:
    """`mdb-export` output as delivered: the table's own column names, every value as text."""
    return pl.read_csv(io.BytesIO(csv), infer_schema=False)


def export_table(mdb: Path, table: str) -> pl.DataFrame:
    """One Access table as all-text columns, with ISO datetimes."""
    csv = subprocess.run(
        ["mdb-export", *EXPORT_OPTIONS, str(mdb), table], check=True, capture_output=True
    ).stdout
    return parse_export(csv)


def read_database(archive: Path, name: str, tables: tuple[str, ...]) -> tuple[dict[str, pl.DataFrame], str]:
    """The named tables of `<name>.mdb` in the zip, and the date the .mdb inside it was written."""
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(archive) as zf:
        info = zf.getinfo(f"{name}.mdb")
        zf.extract(info, tmp)
        mdb = Path(tmp) / info.filename
        frames = {table: export_table(mdb, table) for table in tables}
    return frames, datetime(*info.date_time).date().isoformat()


def withhold(frame: pl.DataFrame, table: str) -> tuple[pl.DataFrame, list[str]]:
    """`frame` without the personal-data columns listed for `table`, and which were dropped."""
    dropped = [c for c in WITHHELD.get(table, ()) if c in frame.columns]
    return frame.drop(dropped), dropped


# --- the raw layer ----------------------------------------------------------


def raw_path(data_dir: Path, snapshot: str, database: str, table: str) -> Path:
    return storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, f"{database}-{table}")


def build_raw(data_dir: Path, snapshot: str, tables: tuple[str, ...]) -> list[Path]:
    """Export `tables` from both databases (downloading them if not cached) to raw parquet."""
    require_mdbtools()
    paths = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=600, follow_redirects=True) as http:
        for database in DATABASES:
            archive = fetch(http, database, cache_path(data_dir, snapshot, database))
            frames, source_date = read_database(archive, database, tables)
            for table, frame in frames.items():
                frame, dropped = withhold(frame, table)
                provenance = {
                    "dataset": DATASET,
                    "layer": storage.RAW,
                    "database": database,
                    "table": table,
                    "source": f"NTSB downloadable aviation database {archive.name}, table {table}",
                    "source_url": PAGE,
                    "source_file": f"{archive.name}/{database}.mdb",
                    "source_date": source_date,
                    "export": f"mdb-export {' '.join(EXPORT_OPTIONS)}",
                    "snapshot": snapshot,
                    "retrieved_at": storage.modified_at(archive),
                }
                if dropped:
                    provenance["withheld_columns"] = json.dumps(dropped)
                path = raw_path(data_dir, snapshot, database, table)
                storage.write_parquet(frame, path, provenance)
                print(f"raw {database}-{table}: {frame.height} rows")
                paths.append(path)
    return paths


def read_raw(data_dir: Path, snapshot: str, tables: tuple[str, ...]) -> dict[str, dict[str, pl.DataFrame]]:
    """database -> table -> raw frame, newest database first."""
    return {
        database: {t: pl.read_parquet(raw_path(data_dir, snapshot, database, t)) for t in tables}
        for database in DATABASES
    }


def sources(data_dir: Path, snapshot: str, tables: tuple[str, ...]) -> list[Path]:
    """The raw files for `tables`, from every database."""
    return [raw_path(data_dir, snapshot, database, t) for database in DATABASES for t in tables]


def processed_provenance(
    data_dir: Path, raw: list[Path], dataset: str, table: str, **extra: str
) -> dict[str, str]:
    """Provenance for a processed table built from several raw NTSB files (`raw`)."""
    read = [storage.read_provenance(path) for path in raw]
    dates = {p["database"]: p["source_date"] for p in read}
    # `source` is also derived_provenance's first parameter, so it is set afterwards.
    source = f"NTSB downloadable aviation databases ({', '.join(f'{d}.zip' for d in dates)})"
    return storage.derived_provenance(
        raw[0],
        data_dir,
        dataset=dataset,
        table=table,
        database=", ".join(dates),
        source_file=", ".join(dict.fromkeys(p["source_file"] for p in read)),
        source_date=", ".join(f"{database}: {date}" for database, date in dates.items()),
        retrieved_at=max(p["retrieved_at"] for p in read),
        built_from=", ".join(storage.object_key(path, data_dir) for path in raw),
        **extra,
    ) | {"source": source}
