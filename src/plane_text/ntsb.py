"""NTSB aviation accident and incident narratives, from the NTSB's downloadable databases.

One row per aircraft per event: the factual narrative, the narrative summary
released with the final report, and the probable cause, with the event's
date, place and injuries and the aircraft's make, model and damage. Owner and
operator names and addresses are left out.

- raw: the `events`, `aircraft` and `narratives` tables of `avall.mdb` and
  `Pre2008.mdb` as delivered, under `raw/ntsb/` (see `plane_text.ntsb_raw`).
- processed: built from raw. Text cleaned, dates and counts typed, one row per
  aircraft per event (the newer database wins), and each coded column
  (`<name>_code`) followed by its label (`<name>`) from the processed
  `ntsb-lookups` tables.

    uv run python -m plane_text.ntsb                     # raw, then processed
    uv run python -m plane_text.ntsb --step processed    # rebuild processed from raw
    uv run python -m plane_text.ntsb --upload            # ...and upload to R2

The raw step needs `mdb-export` from mdbtools. The processed step needs the
processed lookups (`plane_text.ntsb_lookups`) for the same snapshot, or the
latest one built before it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from plane_text import ntsb_lookups, ntsb_raw, storage
from plane_text.ntsb_lookups import Label

DATASET = "ntsb-narratives"
NAME = "narratives"
RAW_TABLES = ("events", "aircraft", "narratives")

NARRATIVES = {
    "narr_accp": "narrative_factual",
    "narr_accf": "narrative_summary",
    "narr_cause": "probable_cause",
}
EVENT_COLUMNS = {
    "ntsb_no": "ntsb_no",
    "ev_type": "event_type_code",
    "ev_date": "event_date",
    "ev_city": "city",
    "ev_state": "state_code",
    "ev_country": "country_code",
    "ev_highest_injury": "highest_injury_code",
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
    "acft_category": "aircraft_category_code",
    "homebuilt": "homebuilt_code",
    "damage": "damage_code",
    "far_part": "far_part_code",
}
INTEGER_COLUMNS = ("fatal_injuries", "serious_injuries", "minor_injuries", "uninjured")
TEXT_COLUMNS = tuple(
    c
    for c in (*EVENT_COLUMNS.values(), *AIRCRAFT_COLUMNS.values())
    if c not in (*INTEGER_COLUMNS, "event_date")
)

# Code columns, their labels, and the data dictionary column whose codes decode them.
LABELS = (
    Label("event_type_code", "event_type", "events", "ev_type"),
    Label("highest_injury_code", "highest_injury", "events", "ev_highest_injury"),
    Label("aircraft_category_code", "aircraft_category", "aircraft", "acft_category"),
    Label("homebuilt_code", "homebuilt", "aircraft", "homebuilt"),
    Label("damage_code", "damage", "aircraft", "damage"),
    Label("far_part_code", "far_part", "aircraft", "far_part"),
)
# Decoded separately: a state only for US events (outside the US the column holds
# other things, `OF` for most), and a country by its own database's code table,
# because avall and Pre2008 use different country codes (`AU` is Australia in
# avall, Austria in Pre2008).
STATE = Label("state_code", "state", "events", "ev_state")
COUNTRY = ("country_code", "country")
CODE_COLUMNS = (*(label.column for label in LABELS), STATE.column, COUNTRY[0])


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
    """One database's narrative rows joined to their event and aircraft: trimmed text, codes uppercased."""
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
    trimmed = ["aircraft_key", "event_date", *INTEGER_COLUMNS, *TEXT_COLUMNS]
    return (
        narratives.join(events, on="ev_id", how="left")
        .join(aircraft, on=["ev_id", "aircraft_key"], how="left")
        .with_columns(pl.col(trimmed).str.strip_chars().replace("", None))
        .with_columns(
            *(ntsb_lookups.normalize(c) for c in CODE_COLUMNS), pl.lit(database).alias("source_database")
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
    return pl.concat(frames).unique(["ev_id", "aircraft_key"], keep="first", maintain_order=True)


def type_columns(frame: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    """Key, date and counts typed; returns how many non-null values in each did not parse."""
    columns = ["aircraft_key", "event_date", *INTEGER_COLUMNS]
    typed = frame.with_columns(
        pl.col("aircraft_key").cast(pl.Int16, strict=False),
        pl.col("event_date").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False),
        *(pl.col(c).cast(pl.Int32, strict=False) for c in INTEGER_COLUMNS),
    )
    before, after = (f.select(pl.col(columns).is_not_null().sum()).row(0) for f in (frame, typed))
    return typed, {c: b - a for c, b, a in zip(columns, before, after, strict=True)}


def add_labels(frame: pl.DataFrame, lookups: ntsb_lookups.Lookups) -> pl.DataFrame:
    """Each code's label right after it: dictionary codes, US states, and countries per database."""
    country = pl.lit(None, pl.String)
    for database in ntsb_raw.DATABASES:
        country = (
            pl.when(pl.col("source_database") == database)
            .then(ntsb_lookups.decode(pl.col(COUNTRY[0]), lookups.country_names(database)))
            .otherwise(country)
        )
    frame = ntsb_lookups.add_labels(frame, LABELS, lookups).with_columns(
        pl.when(pl.col(COUNTRY[0]) == "USA")
        .then(ntsb_lookups.decode(pl.col(STATE.column), lookups.labels(STATE)))
        .alias(STATE.label),
        country.alias(COUNTRY[1]),
    )
    return ntsb_lookups.place_after(frame, {STATE.column: [STATE.label], COUNTRY[0]: [COUNTRY[1]]})


def label_coverage(frame: pl.DataFrame) -> pl.DataFrame:
    """Coverage per decoded column; a state's only over US events, the only ones decoded."""
    pairs = [(label.column, label.label) for label in LABELS]
    return pl.concat(
        [
            ntsb_lookups.coverage(frame, pairs),
            ntsb_lookups.coverage(frame.filter(pl.col(COUNTRY[0]) == "USA"), [(STATE.column, STATE.label)]),
            ntsb_lookups.coverage(frame, [COUNTRY]),
        ]
    )


def process(
    raw: dict[str, dict[str, pl.DataFrame]], lookups: ntsb_lookups.Lookups
) -> tuple[pl.DataFrame, dict[str, int]]:
    """The processed table from the raw ones (database -> table -> frame, newest database first)."""
    frame = combine([build(tables, database) for database, tables in raw.items()])
    frame, unparsed = type_columns(frame)
    frame = add_labels(frame, lookups).sort("event_date", "ev_id", "aircraft_key")
    return frame, unparsed


# --- pipeline ---------------------------------------------------------------


def build_processed(data_dir: Path, snapshot: str) -> Path:
    lookups_dir = ntsb_lookups.processed_dir(data_dir, snapshot)
    raw = ntsb_raw.read_raw(data_dir, snapshot, RAW_TABLES)
    frame, unparsed = process(raw, ntsb_lookups.load(lookups_dir))
    coverage = label_coverage(frame)
    path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, NAME)
    provenance = ntsb_raw.processed_provenance(
        data_dir,
        ntsb_raw.sources(data_dir, snapshot, RAW_TABLES),
        DATASET,
        NAME,
        lookups=storage.object_key(lookups_dir, data_dir),
        unparsed=json.dumps({column: n for column, n in unparsed.items() if n}),
        label_coverage=ntsb_lookups.coverage_json(coverage),
    )
    storage.write_parquet(frame, path, provenance)
    print(f"processed {NAME}: {frame.height} rows")
    print(f"values that did not parse (still in raw): {provenance['unparsed']}")
    ntsb_lookups.print_coverage(coverage)
    return path


def run(data_dir: Path, snapshot: str, steps: list[str], upload: bool) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        paths += ntsb_raw.build_raw(data_dir, snapshot, RAW_TABLES)
    if storage.PROCESSED in steps:
        paths.append(build_processed(data_dir, snapshot))
    if upload:
        for path in paths:
            print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = storage.parse_args(parser)
    run(args.data_dir, args.snapshot, args.steps, args.upload)


if __name__ == "__main__":
    main()
