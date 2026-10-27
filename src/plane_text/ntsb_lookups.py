"""Lookup tables for the NTSB aviation database: column definitions and code meanings.

Both NTSB databases embed a data dictionary (`eADMSPUB_DataDictionary`), code
tables (`ct_iaids`, `ct_seqevt`) and a country table (`Country`).

- raw: those tables from `avall.mdb` and `Pre2008.mdb` as delivered, under
  `raw/ntsb/` (see `plane_text.ntsb_raw`).
- processed: built from raw. `columns`, `codes`, `code_sets` and
  `sequence_of_events_codes` from the current database (avall), and
  `countries` from both, because the two databases use different country
  codes. The processed narratives and findings take their labels from these.

    uv run python -m plane_text.ntsb_lookups                 # raw, then processed
    uv run python -m plane_text.ntsb_lookups --step processed
    uv run python -m plane_text.ntsb_lookups --upload        # ...and upload to R2

The raw step needs `mdb-export` from mdbtools.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from plane_text import ntsb_raw, storage

DATASET = "ntsb-lookups"
DICTIONARY = "eADMSPUB_DataDictionary"
RAW_TABLES = (DICTIONARY, "ct_iaids", "ct_seqevt", "Country")
CURRENT = "avall"  # the database the dictionary and code tables are read from

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


def countries_table(raw: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Each database's `Country` table (database -> raw frame): the two code the same country differently."""
    frames = [
        clean_all(frame)
        .select(pl.lit(database).alias("database"), pl.col("COUNTRY_CODE").alias("code"), "COUNTRY_NAME")
        .rename({"COUNTRY_NAME": "name"})
        for database, frame in raw.items()
    ]
    return pl.concat(frames).filter(pl.col("code").is_not_null()).unique(maintain_order=True)


# --- decoding, for the processed narratives and findings -------------------


@dataclass(frozen=True)
class Label:
    """A code column, the label column for it, and the data dictionary entries that decode it.

    `table` and `source` name the dictionary's table and column. With `match`
    `leading` or `trailing`, the label decodes that end of a composite code
    (as long as the dictionary's `code_part`s). The label is placed after
    `after`, or after `column` itself.
    """

    column: str
    label: str
    table: str
    source: str
    match: str = "exact"
    after: str | None = None


@dataclass(frozen=True)
class Lookups:
    """The processed lookup tables the labels come from."""

    codes: pl.DataFrame
    code_sets: pl.DataFrame
    countries: pl.DataFrame

    def labels(self, label: Label) -> dict[str, str]:
        """code (or code part) -> meaning for one dictionary column; the first wording listed wins.

        For exact codes, codes the dictionary leaves out are looked up in the
        column's named code set (`ct_iaids`), which is sometimes more complete.
        """
        entries = self.codes.filter(
            (pl.col("table") == label.table)
            & (pl.col("column") == label.source)
            & (pl.col("match") == label.match)
        )
        mapping = first_meanings(entries.select("code_part", "meaning"))
        if label.match == "exact":
            code_sets = entries["code_set"].drop_nulls()
            if len(code_sets):
                members = self.code_sets.filter(pl.col("code_set") == code_sets[0])
                mapping = first_meanings(members.select("code", "meaning")) | mapping
        return mapping

    def country_names(self, database: str) -> dict[str, str]:
        return first_meanings(self.countries.filter(pl.col("database") == database).select("code", "name"))


def first_meanings(frame: pl.DataFrame) -> dict[str, str]:
    """A two-column (code, meaning) frame as a dict, codes normalized, the first meaning per code kept."""
    code, meaning = frame.columns
    frame = frame.with_columns(normalize(code)).filter(
        pl.col(code).is_not_null() & pl.col(meaning).is_not_null()
    )
    return dict(frame.unique(code, keep="first", maintain_order=True).iter_rows())


def normalize(column: str) -> pl.Expr:
    """A code as trimmed uppercase text, null when blank."""
    return pl.col(column).str.strip_chars().str.to_uppercase().replace("", None)


def decode(expr: pl.Expr, mapping: dict[str, str]) -> pl.Expr:
    return expr.replace_strict(mapping, default=None, return_dtype=pl.String)


def part(label: Label, mapping: dict[str, str]) -> pl.Expr:
    """The part of `label.column` its dictionary entries describe: all of it, or one end."""
    if label.match == "exact":
        return pl.col(label.column)
    lengths = {len(code) for code in mapping}
    if len(lengths) != 1:
        raise ValueError(f"{label.table}.{label.source}: {label.match} code parts vary in length: {lengths}")
    n = lengths.pop()
    return (
        pl.col(label.column).str.slice(0, n)
        if label.match == "leading"
        else pl.col(label.column).str.slice(-n)
    )


def add_labels(frame: pl.DataFrame, labels: tuple[Label, ...], lookups: Lookups) -> pl.DataFrame:
    """Decode each label's column and place the label after its code."""
    decoded = []
    for label in labels:
        mapping = lookups.labels(label)
        decoded.append(decode(part(label, mapping), mapping).alias(label.label))
    after: dict[str, list[str]] = {}
    for label in labels:
        after.setdefault(label.after or label.column, []).append(label.label)
    return place_after(frame.with_columns(decoded), after)


def place_after(frame: pl.DataFrame, after: dict[str, list[str]]) -> pl.DataFrame:
    """Reorder so each list of columns follows its key column."""
    placed = {c for columns in after.values() for c in columns}
    order = [c for column in frame.columns if column not in placed for c in [column, *after.get(column, [])]]
    return frame.select(order)


def coverage(frame: pl.DataFrame, pairs: list[tuple[str, str]]) -> pl.DataFrame:
    """For each (code, label) column pair: rows with a code, and how many of those got a label."""
    rows = []
    for column, label in pairs:
        coded = frame.filter(pl.col(column).is_not_null())
        labeled = coded[label].is_not_null().sum()
        rows.append((column, label, coded.height, labeled, labeled / coded.height if coded.height else None))
    schema = {"code": pl.String, "label": pl.String, "coded": pl.Int64, "labeled": pl.Int64}
    return pl.DataFrame(rows, schema=schema | {"share": pl.Float64}, orient="row")


def coverage_json(frame: pl.DataFrame) -> str:
    """Coverage as JSON for parquet metadata: label -> [rows with a code, rows labeled]."""
    return json.dumps({row["label"]: [row["coded"], row["labeled"]] for row in frame.iter_rows(named=True)})


def print_coverage(frame: pl.DataFrame) -> None:
    with pl.Config(tbl_rows=-1, tbl_hide_dataframe_shape=True):
        print(frame)


def processed_dir(data_dir: Path, snapshot: str) -> Path:
    """The processed lookups for `snapshot`, or the latest version before it."""
    root = data_dir / storage.PROCESSED / DATASET
    versions = sorted(p for p in root.glob("v*") if p.name <= f"v{snapshot}")
    if not versions:
        raise FileNotFoundError(f"no processed lookups under {root}; run plane_text.ntsb_lookups first")
    return versions[-1]


def load(directory: Path) -> Lookups:
    return Lookups(
        *(pl.read_parquet(directory / f"{name}.parquet") for name in ("codes", "code_sets", "countries"))
    )


# --- pipeline ---------------------------------------------------------------


def build_processed(data_dir: Path, snapshot: str) -> list[Path]:
    raw = ntsb_raw.read_raw(data_dir, snapshot, RAW_TABLES)
    current = raw[CURRENT]

    def source(table: str) -> list[Path]:
        return [ntsb_raw.raw_path(data_dir, snapshot, CURRENT, table)]

    entries = dictionary(current[DICTIONARY])
    tables = {
        "columns": (columns_table(entries), source(DICTIONARY)),
        "codes": (codes_table(entries), source(DICTIONARY)),
        "code_sets": (code_sets_table(current["ct_iaids"]), source("ct_iaids")),
        "sequence_of_events_codes": (sequence_of_events_table(current["ct_seqevt"]), source("ct_seqevt")),
        "countries": (
            countries_table({database: frames["Country"] for database, frames in raw.items()}),
            ntsb_raw.sources(data_dir, snapshot, ("Country",)),
        ),
    }
    paths = []
    for name, (frame, sources) in tables.items():
        path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, name)
        storage.write_parquet(frame, path, ntsb_raw.processed_provenance(data_dir, sources, DATASET, name))
        print(f"processed {name}: {frame.height} rows")
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
