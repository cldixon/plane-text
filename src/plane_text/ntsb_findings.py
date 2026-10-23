"""NTSB investigation findings and event sequences, from the NTSB's downloadable databases.

The coded record of what happened and why, one row per coded item, keyed to
the narratives dataset by `ev_id` and `aircraft_key`. The NTSB changed coding
schemes around 2008, so each scheme gets its own tables:

- 2008 onward: `findings` (causes and factors, flagged when cited in the
  probable cause) and `events_sequence` (the sequence of occurrences, each a
  phase of flight plus an event, flagged when it defined the accident).
- 1982-2007: `occurrences` (occurrence and phase of flight) and
  `sequence_of_events` (coded subjects, modifiers and persons per occurrence).

Codes are kept as the NTSB writes them; meanings are in the ntsb-lookups
tables. Needs `mdb-export` from mdbtools.

    uv run python -m plane_text.ntsb_findings            # write parquet
    uv run python -m plane_text.ntsb_findings --upload   # ...and upload to R2
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import ntsb, storage

DATASET = "ntsb-findings"


@dataclass(frozen=True)
class Table:
    """A source table, the output table it becomes, and how its columns map."""

    source: str
    output: str
    columns: dict[str, str]
    key: tuple[str, ...]
    integers: tuple[str, ...] = ()


TABLES = (
    Table(
        "Findings",
        "findings",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "finding_no": "finding_no",
            "finding_code": "finding_code",
            "finding_description": "finding_description",
            "category_no": "category_no",
            "subcategory_no": "subcategory_no",
            "section_no": "section_no",
            "subsection_no": "subsection_no",
            "modifier_no": "modifier_no",
            "cm_inPc": "in_probable_cause",
            "Cause_Factor": "cause_factor",
        },
        key=("ev_id", "aircraft_key", "finding_no"),
        integers=("finding_no",),
    ),
    Table(
        "Events_Sequence",
        "events_sequence",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "Occurrence_No": "occurrence_no",
            "Occurrence_Code": "occurrence_code",
            "Occurrence_Description": "occurrence_description",
            "phase_no": "phase_no",
            "eventsoe_no": "event_no",
            "Defining_ev": "defining_event",
        },
        key=("ev_id", "aircraft_key", "occurrence_no"),
        integers=("occurrence_no",),
    ),
    Table(
        "Occurrences",
        "occurrences",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "Occurrence_No": "occurrence_no",
            "Occurrence_Code": "occurrence_code",
            "Phase_of_Flight": "phase_of_flight",
            "Altitude": "altitude",
        },
        key=("ev_id", "aircraft_key", "occurrence_no"),
        integers=("occurrence_no", "altitude"),
    ),
    Table(
        "seq_of_events",
        "sequence_of_events",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "Occurrence_No": "occurrence_no",
            "seq_event_no": "seq_event_no",
            "group_code": "group_code",
            "Subj_Code": "subject_code",
            "Cause_Factor": "cause_factor",
            "Modifier_Code": "modifier_code",
            "Person_Code": "person_code",
        },
        key=("ev_id", "aircraft_key", "occurrence_no", "seq_event_no", "group_code"),
        integers=("occurrence_no", "seq_event_no"),
    ),
)

# Flags the source stores as T/F or 1/0.
BOOLEANS = {"T": True, "F": False, "1": True, "0": False}


def build(raw: pl.DataFrame, table: Table, database: str) -> pl.DataFrame:
    """One source table as typed, renamed columns, with the database it came from."""
    # Columns added in later releases (cm_inPc, 2024) are missing from Pre2008.
    missing = [c for c in table.columns if c not in raw.columns]
    frame = (
        raw.with_columns(pl.lit(None, pl.String).alias(c) for c in missing)
        .select(list(table.columns))
        .rename(table.columns)
    )
    flags = [c for c in ("in_probable_cause", "defining_event") if c in frame.columns]
    codes = [c for c in frame.columns if c not in (*table.integers, *flags, "aircraft_key")]
    return frame.with_columns(
        pl.col("aircraft_key").cast(pl.Int16),
        *(pl.col(c).cast(pl.Int32) for c in table.integers),
        *(pl.col(c).replace_strict(BOOLEANS, default=None, return_dtype=pl.Boolean) for c in flags),
        *(pl.col(c).str.strip_chars().replace("", None) for c in codes),
        pl.lit(database).alias("source_database"),
    )


def combine(frames: list[pl.DataFrame], table: Table) -> pl.DataFrame:
    """Frames newest database first; a key in more than one database keeps the first."""
    return pl.concat(frames).unique(list(table.key), keep="first", maintain_order=True).sort(list(table.key))


def run(data_dir: Path, upload: bool) -> list[Path]:
    if shutil.which("mdb-export") is None:
        raise SystemExit("mdb-export not found: install mdbtools (apt-get install mdbtools)")
    snapshot = datetime.now(UTC).date().isoformat()
    sources = tuple(t.source for t in TABLES)
    built: dict[str, list[pl.DataFrame]] = {t.output: [] for t in TABLES}
    with httpx.Client(headers={"User-Agent": ntsb.USER_AGENT}, timeout=600, follow_redirects=True) as http:
        for name in ntsb.DATABASES:
            # Shares the narratives pipeline's cache.
            cache = data_dir / "raw" / ntsb.DATASET / (snapshot if name == "avall" else "static")
            raw = ntsb.read_database(ntsb.fetch(http, name, cache / f"{name}.zip"), name, sources)
            for table in TABLES:
                built[table.output].append(build(raw[table.source], table, name))

    out = data_dir / DATASET / f"v{snapshot}"
    retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
    paths = []
    for table in TABLES:
        frame = combine(built[table.output], table)
        path = out / f"{table.output}.parquet"
        storage.write_parquet(
            frame,
            path,
            provenance={
                "dataset": DATASET,
                "table": table.output,
                "source": f"NTSB avall.zip and Pre2008.zip, table {table.source}",
                "source_url": ntsb.PAGE,
                "snapshot": snapshot,
                "retrieved_at": retrieved_at,
            },
        )
        print(f"{table.output}: {frame.height} rows")
        paths.append(path)
        if upload:
            storage.upload(path, data_dir)
    if upload:
        print(f"uploaded {len(paths)} tables to r2://{storage.bucket()}/{DATASET}/v{snapshot}/")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--upload", action="store_true", help="upload the parquet files to R2")
    args = parser.parse_args()
    run(args.data_dir, args.upload)


if __name__ == "__main__":
    main()
