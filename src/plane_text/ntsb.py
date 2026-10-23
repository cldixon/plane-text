"""NTSB aviation accident and incident narratives, from the NTSB's downloadable databases.

One row per aircraft per event: the factual narrative, the narrative summary
released with the final report, and the probable cause, with the event's
date, place and injuries and the aircraft's make, model and damage. Owner and
operator names and addresses are left out.

The NTSB publishes Microsoft Access files: `Pre2008.zip` (1982-2007) and
`avall.zip` (2008 to present, refreshed monthly). Reading them needs the
`mdb-export` tool from mdbtools (`apt-get install mdbtools`).

    uv run python -m plane_text.ntsb            # write parquet
    uv run python -m plane_text.ntsb --upload   # ...and upload to R2
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import storage

DATASET = "ntsb-narratives"
DOWNLOAD = "https://data.ntsb.gov/avdata/FileDirectory/DownloadFile?fileID={name}"
PAGE = "https://data.ntsb.gov/avdata/"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

# Newest first: when an event appears in both, the newer database wins.
DATABASES = ("avall", "Pre2008")

NARRATIVES = {
    "narr_accp": "narrative_factual",
    "narr_accf": "narrative_summary",
    "narr_cause": "probable_cause",
}
EVENT_COLUMNS = {
    "ntsb_no": "ntsb_no",
    "ev_type": "event_type",
    "ev_date": "event_date",
    "ev_city": "city",
    "ev_state": "state",
    "ev_country": "country",
    "ev_highest_injury": "highest_injury",
    "inj_tot_f": "fatal_injuries",
    "inj_tot_s": "serious_injuries",
    "inj_tot_m": "minor_injuries",
    "inj_tot_n": "uninjured",
}
AIRCRAFT_COLUMNS = {
    "regis_no": "registration",
    "acft_make": "aircraft_make",
    "acft_model": "aircraft_model",
    "acft_series": "aircraft_series",
    "acft_category": "aircraft_category",
    "homebuilt": "homebuilt",
    "damage": "damage",
    "far_part": "far_part",
}
INTEGER_COLUMNS = ("fatal_injuries", "serious_injuries", "minor_injuries", "uninjured")
TEXT_COLUMNS = tuple(
    c
    for c in (*EVENT_COLUMNS.values(), *AIRCRAFT_COLUMNS.values())
    if c not in (*INTEGER_COLUMNS, "event_date")
)


# --- fetching ---------------------------------------------------------------


def fetch(http: httpx.Client, name: str, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    response = http.get(DOWNLOAD.format(name=f"{name}.zip"))
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


def export_table(mdb: Path, table: str) -> pl.DataFrame:
    """One Access table as all-text columns, with ISO datetimes."""
    csv = subprocess.run(
        ["mdb-export", "-T", "%Y-%m-%dT%H:%M:%S", str(mdb), table],
        check=True,
        capture_output=True,
    ).stdout
    return pl.read_csv(io.BytesIO(csv), infer_schema=False)


def read_database(archive: bytes, name: str) -> dict[str, pl.DataFrame]:
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(archive)).extract(f"{name}.mdb", tmp)
        mdb = Path(tmp) / f"{name}.mdb"
        return {table: export_table(mdb, table) for table in ("narratives", "events", "aircraft")}


# --- parsing ----------------------------------------------------------------


def clean_text(column: str) -> pl.Expr:
    """Unix line breaks, no trailing spaces on lines, at most one blank line, empty as null."""
    return (
        pl.col(column)
        .str.replace_all("\r\n?", "\n")
        .str.replace_all(r"[ \t]+\n", "\n")
        .str.replace_all(r"\n{3,}", "\n\n")
        .str.strip_chars()
        .replace("", None)
    )


def build(tables: dict[str, pl.DataFrame], database: str) -> pl.DataFrame:
    """Narrative rows for one database, joined to their event and aircraft."""
    narratives = (
        tables["narratives"]
        .rename({"Aircraft_Key": "aircraft_key", **NARRATIVES})
        .with_columns(clean_text(c) for c in NARRATIVES.values())
        .filter(pl.any_horizontal(pl.col(c).is_not_null() for c in NARRATIVES.values()))
        .select("ev_id", "aircraft_key", *NARRATIVES.values())
    )
    events = tables["events"].select("ev_id", *EVENT_COLUMNS).rename(EVENT_COLUMNS)
    aircraft = (
        tables["aircraft"]
        .select("ev_id", "Aircraft_Key", *AIRCRAFT_COLUMNS)
        .rename({"Aircraft_Key": "aircraft_key", **AIRCRAFT_COLUMNS})
    )
    return (
        narratives.join(events, on="ev_id", how="left")
        .join(aircraft, on=["ev_id", "aircraft_key"], how="left")
        .with_columns(
            pl.col("aircraft_key").cast(pl.Int16),
            pl.col("event_date").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False),
            *(pl.col(c).cast(pl.Int32, strict=False) for c in INTEGER_COLUMNS),
            *(pl.col(c).str.strip_chars().replace("", None) for c in TEXT_COLUMNS),
            pl.lit(database).alias("source_database"),
        )
        .select(
            "ev_id",
            "aircraft_key",
            *EVENT_COLUMNS.values(),
            *AIRCRAFT_COLUMNS.values(),
            *NARRATIVES.values(),
            "source_database",
        )
    )


def combine(frames: list[pl.DataFrame]) -> pl.DataFrame:
    """Frames newest database first; an aircraft in more than one keeps the first."""
    return (
        pl.concat(frames)
        .unique(["ev_id", "aircraft_key"], keep="first", maintain_order=True)
        .sort("event_date", "ev_id", "aircraft_key")
    )


# --- pipeline ---------------------------------------------------------------


def run(data_dir: Path, upload: bool) -> Path:
    if shutil.which("mdb-export") is None:
        raise SystemExit("mdb-export not found: install mdbtools (apt-get install mdbtools)")
    snapshot = datetime.now(UTC).date().isoformat()
    frames = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=600, follow_redirects=True) as http:
        for name in DATABASES:
            # avall changes monthly, so its cache is per snapshot; Pre2008 is static.
            cache = data_dir / "raw" / DATASET / (snapshot if name == "avall" else "static")
            tables = read_database(fetch(http, name, cache / f"{name}.zip"), name)
            frames.append(build(tables, name))
            print(f"{name}: {frames[-1].height} narrative rows")

    frame = combine(frames)
    path = data_dir / DATASET / f"v{snapshot}" / "narratives.parquet"
    storage.write_parquet(
        frame,
        path,
        provenance={
            "dataset": DATASET,
            "source": "NTSB downloadable aviation datasets (avall.zip, Pre2008.zip)",
            "source_url": PAGE,
            "snapshot": snapshot,
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    print(f"wrote {frame.height} rows to {path}")
    if upload:
        key = storage.upload(path, data_dir)
        print(f"uploaded to r2://{storage.bucket()}/{key}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--upload", action="store_true", help="upload the parquet to R2")
    args = parser.parse_args()
    run(args.data_dir, args.upload)


if __name__ == "__main__":
    main()
