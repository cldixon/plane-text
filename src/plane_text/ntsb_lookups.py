"""Lookup tables for the NTSB aviation database: column definitions and code meanings.

Both NTSB databases embed a data dictionary (`eADMSPUB_DataDictionary`) and
code tables (`ct_iaids`, `ct_seqevt`). This reads them from the current
`avall.zip` and writes four long tables, so codes in the narratives dataset
(and in tables not yet published, such as findings and occurrences) can be
joined to their meanings. Needs `mdb-export` from mdbtools.

    uv run python -m plane_text.ntsb_lookups            # write parquet
    uv run python -m plane_text.ntsb_lookups --upload   # ...and upload to R2
"""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import ntsb, storage

DATASET = "ntsb-lookups"
SOURCE_TABLES = ("eADMSPUB_DataDictionary", "ct_iaids", "ct_seqevt")

DICTIONARY_COLUMNS = {
    "Table": "table",
    "Column": "column",
    "Category of Data": "category",
    "Data Type eADMS": "data_type",
    "Length eADMS": "length",
    "short_desc": "short_description",
    "Question_Def": "definition",
    "code_iaids": "code",
    "meaning": "meaning",
    "Code meaning": "detail",
    "ct_name": "code_set",
    "typeofchange": "change_type",
    "Change_notes": "change_notes",
}


def clean_all(frame: pl.DataFrame) -> pl.DataFrame:
    """Collapse runs of whitespace, trim, and turn empty strings into nulls."""
    return frame.with_columns(
        pl.col(pl.String).str.replace_all(r"\s+", " ").str.strip_chars().replace("", None)
    )


def dictionary(raw: pl.DataFrame) -> pl.DataFrame:
    return clean_all(raw.rename(DICTIONARY_COLUMNS).select(DICTIONARY_COLUMNS.values())).filter(
        pl.col("table").is_not_null() & pl.col("column").is_not_null()
    )


def columns_table(entries: pl.DataFrame) -> pl.DataFrame:
    """One row per database column: what it holds and its type."""
    described = ["category", "data_type", "length", "short_description", "definition"]
    return (
        entries.group_by("table", "column", maintain_order=True)
        .agg(pl.col(c).drop_nulls().first() for c in described)
        .sort("table", "column")
    )


def codes_table(entries: pl.DataFrame) -> pl.DataFrame:
    """One row per code per column. A few columns list a code twice with different wording.

    Findings and event-sequence codes are composite, and the dictionary describes
    each part with placeholders: `01022214XX` is the finding (first eight
    digits), `xxxxxxxx01` the modifier (last two), `152xxx` the phase of flight
    and `xxx091` the event. `match` says which end of a full code `code_part`
    matches; elsewhere an X is a literal letter and `match` is `exact`.
    """
    composite = (
        (pl.col("table") == "Findings") & pl.col("column").is_in(["findings_code", "modifier_no"])
    ) | ((pl.col("table") == "Events_Sequence") & (pl.col("column") == "Occurrence_Code"))
    return (
        entries.filter(pl.col("code").is_not_null())
        .with_columns(
            pl.when(composite & pl.col("code").str.contains(r"^\d+[xX]+$"))
            .then(pl.lit("leading"))
            .when(composite & pl.col("code").str.contains(r"^[xX]+\d+$"))
            .then(pl.lit("trailing"))
            .otherwise(pl.lit("exact"))
            .alias("match")
        )
        .with_columns(
            pl.when(pl.col("match") == "exact")
            .then(pl.col("code"))
            .otherwise(pl.col("code").str.replace_all("[xX]", ""))
            .alias("code_part")
        )
        .select(
            "table",
            "column",
            "code",
            "match",
            "code_part",
            "meaning",
            "detail",
            "code_set",
            "category",
            "change_type",
            "change_notes",
        )
        .unique(maintain_order=True)
    )


def code_sets_table(raw: pl.DataFrame) -> pl.DataFrame:
    """The NTSB's named code sets (`ct_iaids`), as published."""
    return (
        clean_all(
            raw.rename({"ct_name": "code_set", "code_iaids": "code"}).select(
                "code_set", "code", "meaning", "seq", "notes", "not_for_ntsb_use", "eADMS_use"
            )
        )
        .with_columns(
            pl.col("seq").cast(pl.Int32, strict=False),
            pl.col("not_for_ntsb_use", "eADMS_use").cast(pl.Int8, strict=False).cast(pl.Boolean),
        )
        .rename({"eADMS_use": "eadms_use"})
    )


def sequence_of_events_table(raw: pl.DataFrame) -> pl.DataFrame:
    """Legacy sequence-of-events codes (`ct_seqevt`), used by pre-2008 coding."""
    return clean_all(raw).select("code", "meaning").sort(pl.col("code").cast(pl.Int32, strict=False))


def run(data_dir: Path, upload: bool) -> list[Path]:
    if shutil.which("mdb-export") is None:
        raise SystemExit("mdb-export not found: install mdbtools (apt-get install mdbtools)")
    snapshot = datetime.now(UTC).date().isoformat()
    # Shares the narratives pipeline's cache, so avall.zip downloads once per snapshot.
    cache = data_dir / "raw" / ntsb.DATASET / snapshot
    with httpx.Client(headers={"User-Agent": ntsb.USER_AGENT}, timeout=600, follow_redirects=True) as http:
        raw = ntsb.read_database(ntsb.fetch(http, "avall", cache / "avall.zip"), "avall", SOURCE_TABLES)

    entries = dictionary(raw["eADMSPUB_DataDictionary"])
    tables = {
        "columns": columns_table(entries),
        "codes": codes_table(entries),
        "code_sets": code_sets_table(raw["ct_iaids"]),
        "sequence_of_events_codes": sequence_of_events_table(raw["ct_seqevt"]),
    }

    out = data_dir / DATASET / f"v{snapshot}"
    retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
    paths = []
    for name, frame in tables.items():
        path = out / f"{name}.parquet"
        storage.write_parquet(
            frame,
            path,
            provenance={
                "dataset": DATASET,
                "table": name,
                "source": "NTSB avall.zip: eADMSPUB_DataDictionary, ct_iaids and ct_seqevt tables",
                "source_url": ntsb.PAGE,
                "snapshot": snapshot,
                "retrieved_at": retrieved_at,
            },
        )
        print(f"{name}: {frame.height} rows")
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
