"""NTSB investigation findings and event sequences, from the NTSB's downloadable databases.

The coded record of what happened and why, one row per coded item, keyed to
the narratives dataset by `ev_id` and `aircraft_key`. The NTSB changed coding
schemes around 2008, so each scheme gets its own tables:

- 2008 onward: `findings` (causes and factors, flagged when cited in the
  probable cause) and `events_sequence` (the sequence of occurrences, each a
  phase of flight plus an event, flagged when it defined the accident).
- 1982-2007: `occurrences` (occurrence and phase of flight) and
  `sequence_of_events` (coded subjects, modifiers and persons per occurrence).

Layers:

- raw: the `Findings`, `Events_Sequence`, `Occurrences` and `seq_of_events`
  tables of `avall.mdb` and `Pre2008.mdb` as delivered, under `raw/ntsb/`
  (see `plane_text.ntsb_raw`).
- processed: built from raw. One row per key (the newer database wins), numbers
  and flags typed, codes trimmed and uppercased, and each code (`<name>_code`)
  followed by its label (`<name>`) from the processed `ntsb-lookups` tables.
  The 2008-onward tables already carry the NTSB's own descriptions
  (`finding_description`, `occurrence_description`); the labels add their
  parts: the finding and its modifier, and the phase of flight and event, by
  decoding each end of the composite code. In the 1982-2007 sequence of
  events, a modifier or person code of `0` means none and becomes null.

    uv run python -m plane_text.ntsb_findings                   # raw, then processed
    uv run python -m plane_text.ntsb_findings --step processed  # rebuild processed from raw
    uv run python -m plane_text.ntsb_findings --upload          # ...and upload to R2

The raw step needs `mdb-export` from mdbtools. The processed step needs the
processed lookups (`plane_text.ntsb_lookups`) for the same snapshot, or the
latest one built before it.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from plane_text import ntsb_lookups, ntsb_raw, storage
from plane_text.ntsb_lookups import Label

DATASET = "ntsb-findings"

# The NTSB's cause/factor flag (C or F), described once in the dictionary, for seq_of_events.
CAUSE_FACTOR = ("cause_factor_code", "cause_factor", "seq_of_events", "Cause_Factor")


@dataclass(frozen=True)
class Table:
    """A source table, the output table it becomes, how its columns map, and how its codes decode."""

    source: str
    output: str
    columns: dict[str, str]
    key: tuple[str, ...]
    integers: tuple[str, ...] = ()
    labels: tuple[Label, ...] = ()
    zero_is_none: tuple[str, ...] = ()  # code columns where 0 means no code


TABLES = (
    Table(
        "Findings",
        "findings",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "finding_no": "finding_no",
            "finding_code": "finding_code",
            "modifier_no": "modifier_code",
            "finding_description": "finding_description",
            "category_no": "category_no",
            "subcategory_no": "subcategory_no",
            "section_no": "section_no",
            "subsection_no": "subsection_no",
            "cm_inPc": "in_probable_cause",
            "Cause_Factor": "cause_factor_code",
        },
        key=("ev_id", "aircraft_key", "finding_no"),
        integers=("finding_no",),
        labels=(
            Label("finding_code", "finding", "Findings", "findings_code", "leading"),
            Label("finding_code", "modifier", "Findings", "modifier_no", "trailing", after="modifier_code"),
            Label(*CAUSE_FACTOR),
        ),
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
            "phase_no": "phase_code",
            "eventsoe_no": "event_code",
            "Defining_ev": "defining_event",
        },
        key=("ev_id", "aircraft_key", "occurrence_no"),
        integers=("occurrence_no",),
        labels=(
            Label("occurrence_code", "phase", "Events_Sequence", "Occurrence_Code", "leading", "phase_code"),
            Label("occurrence_code", "event", "Events_Sequence", "Occurrence_Code", "trailing", "event_code"),
        ),
    ),
    Table(
        "Occurrences",
        "occurrences",
        {
            "ev_id": "ev_id",
            "Aircraft_Key": "aircraft_key",
            "Occurrence_No": "occurrence_no",
            "Occurrence_Code": "occurrence_code",
            "Phase_of_Flight": "phase_of_flight_code",
            "Altitude": "altitude",
        },
        key=("ev_id", "aircraft_key", "occurrence_no"),
        integers=("occurrence_no", "altitude"),
        labels=(
            Label("occurrence_code", "occurrence", "Occurrences", "Occurrence_Code"),
            Label("phase_of_flight_code", "phase_of_flight", "Occurrences", "Phase_of_Flight"),
        ),
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
            "Cause_Factor": "cause_factor_code",
            "Modifier_Code": "modifier_code",
            "Person_Code": "person_code",
        },
        key=("ev_id", "aircraft_key", "occurrence_no", "seq_event_no", "group_code"),
        integers=("occurrence_no", "seq_event_no"),
        labels=(
            Label("group_code", "group", "seq_of_events", "group_code"),
            Label("subject_code", "subject", "seq_of_events", "Subj_Code"),
            Label(*CAUSE_FACTOR),
            Label("modifier_code", "modifier", "seq_of_events", "Modifier_Code"),
            Label("person_code", "person", "seq_of_events", "Person_Code"),
        ),
        zero_is_none=("modifier_code", "person_code"),
    ),
)
RAW_TABLES = tuple(t.source for t in TABLES)

# Flags the source stores as T/F or 1/0.
BOOLEANS = {"T": True, "F": False, "1": True, "0": False}
FLAGS = ("in_probable_cause", "defining_event")


def build(raw: pl.DataFrame, table: Table, database: str) -> pl.DataFrame:
    """One source table renamed, values trimmed (blank -> null), codes uppercased, with its database."""
    # Columns added in later releases (cm_inPc, 2024) are missing from Pre2008.
    missing = [c for c in table.columns if c not in raw.columns]
    frame = (
        raw.with_columns(pl.lit(None, pl.String).alias(c) for c in missing)
        .select(list(table.columns))
        .rename(table.columns)
    )
    codes = [c for c in frame.columns if c.endswith("_code")]
    return frame.with_columns(pl.all().str.strip_chars().replace("", None)).with_columns(
        *(ntsb_lookups.normalize(c) for c in codes),
        pl.lit(database).alias("source_database"),
    )


def combine(frames: list[pl.DataFrame], table: Table) -> pl.DataFrame:
    """Frames newest database first; a key in more than one database keeps the first."""
    return pl.concat(frames).unique(list(table.key), keep="first", maintain_order=True)


def type_columns(frame: pl.DataFrame, table: Table) -> tuple[pl.DataFrame, dict[str, int]]:
    """Key, numbers and flags typed; returns how many non-null values in each did not parse."""
    flags = [c for c in FLAGS if c in frame.columns]
    columns = ["aircraft_key", *table.integers, *flags]
    typed = frame.with_columns(
        pl.col("aircraft_key").cast(pl.Int16, strict=False),
        *(pl.col(c).cast(pl.Int32, strict=False) for c in table.integers),
        *(pl.col(c).replace_strict(BOOLEANS, default=None, return_dtype=pl.Boolean) for c in flags),
    )
    before, after = (f.select(pl.col(columns).is_not_null().sum()).row(0) for f in (frame, typed))
    return typed, {c: b - a for c, b, a in zip(columns, before, after, strict=True)}


def process(
    frames: list[pl.DataFrame], table: Table, lookups: ntsb_lookups.Lookups
) -> tuple[pl.DataFrame, dict[str, int]]:
    """One processed table from its built frames (newest database first)."""
    frame, unparsed = type_columns(combine(frames, table), table)
    frame = frame.with_columns(pl.col(c).replace("0", None) for c in table.zero_is_none)
    frame = ntsb_lookups.add_labels(frame, table.labels, lookups)
    return frame.sort(list(table.key)), unparsed


def label_coverage(frame: pl.DataFrame, table: Table) -> pl.DataFrame:
    return ntsb_lookups.coverage(frame, [(label.column, label.label) for label in table.labels])


# --- pipeline ---------------------------------------------------------------


def build_processed(data_dir: Path, snapshot: str) -> list[Path]:
    lookups_dir = ntsb_lookups.processed_dir(data_dir, snapshot)
    lookups = ntsb_lookups.load(lookups_dir)
    raw = ntsb_raw.read_raw(data_dir, snapshot, RAW_TABLES)
    paths = []
    for table in TABLES:
        built = [build(tables[table.source], table, database) for database, tables in raw.items()]
        frame, unparsed = process(built, table, lookups)
        coverage = label_coverage(frame, table)
        path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, table.output)
        provenance = ntsb_raw.processed_provenance(
            data_dir,
            ntsb_raw.sources(data_dir, snapshot, (table.source,)),
            DATASET,
            table.output,
            lookups=storage.object_key(lookups_dir, data_dir),
            unparsed=json.dumps({column: n for column, n in unparsed.items() if n}),
            label_coverage=ntsb_lookups.coverage_json(coverage),
        )
        storage.write_parquet(frame, path, provenance)
        print(f"processed {table.output}: {frame.height} rows; did not parse: {provenance['unparsed']}")
        ntsb_lookups.print_coverage(coverage)
        paths.append(path)
    return paths


def run(data_dir: Path, snapshot: str, steps: list[str], upload: bool) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        paths += ntsb_raw.build_raw(data_dir, snapshot, RAW_TABLES)
    if storage.PROCESSED in steps:
        paths += build_processed(data_dir, snapshot)
    if upload:
        for path in paths:
            storage.upload(path, data_dir)
        print(f"uploaded {len(paths)} files to r2://{storage.bucket()}/")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = storage.parse_args(parser)
    run(args.data_dir, args.snapshot, args.steps, args.upload)


if __name__ == "__main__":
    main()
