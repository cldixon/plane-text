"""FAA Service Difficulty Reports (SDRs), from the FAA's yearly CSV downloads.

One row per report: what failed, where on the aircraft, how it was found,
and a free-text discrepancy narrative. Each year's CSV is cached as gzip, so a
re-run only re-parses; the current year is fetched again when building
today's snapshot, because it grows as reports arrive.

- raw: every year's rows as delivered, the FAA's own column names and every
  value as text (empty fields are null), plus `source_file` and `source_row`.
- processed: built from raw. Snake_case columns, values trimmed, dates and
  measures typed, codes uppercased and joined to their labels from the
  processed `faa-sdrs-lookups` tables, one row per report.

    uv run python -m plane_text.faa_sdrs                      # all years, raw then processed
    uv run python -m plane_text.faa_sdrs --years 2024 2025
    uv run python -m plane_text.faa_sdrs --step processed     # rebuild processed from raw
    uv run python -m plane_text.faa_sdrs --upload             # ...and upload to R2

The processed step needs the processed lookups (`plane_text.faa_sdrs_lookups`)
for the same snapshot, or the latest one built before it.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import faa_sdrs_lookups, storage

DATASET = "faa-sdrs"
NAME = "service-difficulty-reports"
FIRST_YEAR = 1995
URL = "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv"
PAGE = "https://www.faa.gov/av-info/download_SDR"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
SOURCE_FILE = re.compile(r"SDR-(\d{4})\.csv")
ZONE = re.compile(r"^(?:ZONE|ZN)\s*(\d)\d\d\b")

# Counts, hours and lengths. Each value either casts or becomes null, and the
# number that did not cast is recorded in the processed file's provenance;
# the original text is always in raw. Station, stringer, butt line and water
# line columns are location designators (`727B`, `RIB 10`, `Y1292`), not
# measures, so they stay text.
MEASURES = {
    "aircraft_total_time": pl.Int64,
    "aircraft_total_cycles": pl.Int64,
    "engine_total_time": pl.Int64,
    "engine_total_cycles": pl.Int64,
    "propeller_total_time": pl.Int64,
    "propeller_total_cycles": pl.Int64,
    "part_total_time": pl.Int64,
    "part_total_cycles": pl.Int64,
    "part_time_since": pl.Int64,
    "component_total_time": pl.Int64,
    "component_total_cycles": pl.Int64,
    "component_time_since": pl.Int64,
    "crack_length": pl.Float64,
    "number_of_cracks": pl.Int16,
    "corrosion_level": pl.Int8,
}

# Code columns whose source names lack the `_code` suffix the label pattern needs.
RENAMES = {
    "sdr_type": "sdr_type_code",
    **{f"nature_of_condition_{x}": f"nature_of_condition_{x}_code" for x in "abc"},
    **{f"precautionary_procedure_{x}": f"precautionary_procedure_{x}_code" for x in "abcd"},
}
# Free-text-ish codes with no lookup table: still trimmed and uppercased.
UPPERCASE = ("receiving_region_code",)


@dataclass(frozen=True)
class Code:
    """A code column, the label column placed after it, and the lookup table that decodes it."""

    column: str
    label: str
    table: str


NATURE, PRECAUTION = "nature_of_condition", "precautionary_procedure"
CODES = (
    Code("submitter_type_code", "submitter_type", "submitter_type"),
    Code("sdr_type_code", "sdr_type", "sdr_type"),
    *(Code(f"nature_of_condition_{x}_code", f"nature_of_condition_{x}", NATURE) for x in "abc"),
    *(Code(f"precautionary_procedure_{x}_code", f"precautionary_procedure_{x}", PRECAUTION) for x in "abcd"),
    Code("stage_of_operation_code", "stage_of_operation", "stage_of_operation"),
    Code("how_discovered_code", "how_discovered", "how_discovered"),
    Code("part_since_code", "part_since", "time_since"),
    Code("component_since_code", "component_since", "time_since"),
)
# Decoded from part_location where it names a zone; the JASC code gets three columns.
ZONE_CODE = Code("part_location_zone_code", "part_location_zone", "part_location_zone")
JASC_LABELS = ("jasc_chapter", "jasc_chapter_title", "jasc_title")


def snake(name: str) -> str:
    """`OperatorControlNumber` -> `operator_control_number`, `JASCCode` -> `jasc_code`."""
    return CAMEL.sub("_", name).lower().replace("buttline", "butt_line")


# --- fetching ---------------------------------------------------------------


def fetch_year(http: httpx.Client, year: int, cache: Path, refresh: bool) -> bytes:
    path = cache / f"SDR-{year}.csv.gz"
    if path.exists() and not refresh:
        return gzip.decompress(path.read_bytes())
    for attempt in range(4):
        try:
            response = http.get(URL.format(year=year))
            response.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(response.content))
    return response.content


# --- raw --------------------------------------------------------------------


def read_year(raw: bytes, year: int) -> pl.DataFrame:
    """One year's CSV as delivered: the FAA's column names, all text, plus where each row came from."""
    frame = pl.read_csv(io.BytesIO(raw), infer_schema=False)
    return frame.with_columns(
        pl.lit(f"SDR-{year}.csv").alias("source_file"),
        pl.int_range(1, pl.len() + 1, dtype=pl.Int32).alias("source_row"),
    )


def combine(frames: list[pl.DataFrame]) -> pl.DataFrame:
    columns = frames[0].columns
    for frame in frames[1:]:
        if frame.columns != columns:
            raise ValueError(f"column layout changed in {frame['source_file'][0]}")
    return pl.concat(frames)


# --- processed --------------------------------------------------------------


def load_lookups(directory: Path) -> dict[str, dict[str, str]]:
    """code -> description for each code table, codes normalized as the SDR codes are."""
    tables = {code.table for code in (*CODES, ZONE_CODE)}
    lookups = {}
    for table in tables:
        frame = pl.read_parquet(directory / f"{table}.parquet")
        frame = frame.with_columns(normalize("code"))
        lookups[table] = dict(zip(frame["code"], frame["description"], strict=True))
    jasc = pl.read_parquet(directory / "jasc.parquet")
    lookups["jasc_title"] = dict(zip(jasc["code"], jasc["title"], strict=True))
    lookups["jasc_chapter_title"] = dict(jasc.select("chapter", "chapter_title").unique().iter_rows())
    return lookups


def normalize(column: str) -> pl.Expr:
    """A code as trimmed uppercase text, null when blank."""
    return pl.col(column).str.strip_chars().str.to_uppercase().replace("", None)


def zone_digit(column: str) -> pl.Expr:
    """The major zone digit of `ZONE 200`, `ZN 100 FUS LOWER` or `ZONE 221` (ATA zoning: 221 is in 200)."""
    return pl.col(column).str.to_uppercase().str.extract(ZONE.pattern)


def decode(column: str, mapping: dict[str, str]) -> pl.Expr:
    return pl.col(column).replace_strict(mapping, default=None, return_dtype=pl.String)


def tidy(raw: pl.DataFrame) -> pl.DataFrame:
    """Snake_case names, values trimmed (blank -> null), and `file_year` in place of the row provenance."""
    frame = raw.drop("source_row").rename(lambda c: RENAMES.get(snake(c), snake(c)))
    texts = [c for c, dtype in frame.schema.items() if dtype == pl.String and c != "source_file"]
    return frame.with_columns(pl.col(texts).str.strip_chars().replace("", None)).with_columns(
        pl.col("source_file").str.extract(SOURCE_FILE.pattern).cast(pl.Int16).alias("file_year")
    )


def type_columns(frame: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    """Dates and measures typed; returns how many non-null values in each did not parse."""
    typed = frame.with_columns(
        pl.col("difficulty_date").str.to_date("%m/%d/%Y", strict=False),
        pl.col("submission_date").str.to_datetime(time_zone="UTC", strict=False),
        *(pl.col(column).cast(dtype, strict=False) for column, dtype in MEASURES.items()),
    )
    columns = ["difficulty_date", "submission_date", *MEASURES]
    before, after = (f.select(pl.col(columns).is_not_null().sum()).row(0) for f in (frame, typed))
    return typed, {c: b - a for c, b, a in zip(columns, before, after, strict=True)}


def add_labels(frame: pl.DataFrame, lookups: dict[str, dict[str, str]]) -> pl.DataFrame:
    """Normalize the code columns and put each code's label (or labels) right after it."""
    frame = frame.with_columns(normalize(c) for c in (*(code.column for code in CODES), *UPPERCASE))
    frame = frame.with_columns(
        *(decode(code.column, lookups[code.table]).alias(code.label) for code in CODES),
        pl.concat_str(pl.lit("ZONE "), zone_digit("part_location"), pl.lit("00")).alias(ZONE_CODE.column),
        pl.when(pl.col("jasc_code").str.contains(r"^\d{4}$"))
        .then(pl.col("jasc_code").str.slice(0, 2))
        .alias("jasc_chapter"),
    ).with_columns(
        decode(ZONE_CODE.column, lookups[ZONE_CODE.table]).alias(ZONE_CODE.label),
        decode("jasc_chapter", lookups["jasc_chapter_title"]).alias("jasc_chapter_title"),
        decode("jasc_code", lookups["jasc_title"]).alias("jasc_title"),
    )
    after = {code.column: [code.label] for code in CODES}
    after["part_location"] = [ZONE_CODE.column, ZONE_CODE.label]
    after["jasc_code"] = list(JASC_LABELS)
    placed = {c for labels in after.values() for c in labels}
    order = [c for column in frame.columns if column not in placed for c in [column, *after.get(column, [])]]
    return frame.select(order)


def label_coverage(frame: pl.DataFrame) -> pl.DataFrame:
    """For each decoded column: how many rows have a code, and how many of those got a label."""
    pairs = [(code.column, code.label) for code in (*CODES, ZONE_CODE)]
    pairs += [("jasc_code", label) for label in JASC_LABELS[1:]]
    rows = []
    for column, label in pairs:
        coded = frame.filter(pl.col(column).is_not_null())
        labeled = coded[label].is_not_null().sum()
        rows.append((column, label, coded.height, labeled, labeled / coded.height if coded.height else None))
    schema = {"code": pl.String, "label": pl.String, "coded": pl.Int64, "labeled": pl.Int64}
    return pl.DataFrame(rows, schema=schema | {"share": pl.Float64}, orient="row")


def process(raw: pl.DataFrame, lookups: dict[str, dict[str, str]]) -> tuple[pl.DataFrame, dict[str, int]]:
    """The processed table from the raw one: one row per report (the last filed copy wins)."""
    frame, unparsed = type_columns(tidy(raw))
    frame = add_labels(frame, lookups).drop("source_file")
    frame = frame.unique("operator_control_number", keep="last", maintain_order=True)
    return frame, unparsed


# --- pipeline ---------------------------------------------------------------


def lookups_dir(data_dir: Path, snapshot: str) -> Path:
    """The processed lookups for `snapshot`, or the latest version before it."""
    root = data_dir / storage.PROCESSED / faa_sdrs_lookups.DATASET
    versions = sorted(p for p in root.glob("v*") if p.name <= f"v{snapshot}")
    if not versions:
        raise FileNotFoundError(f"no processed lookups under {root}; run plane_text.faa_sdrs_lookups first")
    return versions[-1]


def build_raw(data_dir: Path, snapshot: str, years: list[int]) -> Path:
    today = datetime.now(UTC).date()
    cache = storage.cache_dir(data_dir, DATASET)
    frames = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as http:
        for year in years:
            refresh = year == today.year and snapshot == today.isoformat()
            frames.append(read_year(fetch_year(http, year, cache, refresh), year))
            print(f"{year}: {frames[-1].height} reports")

    frame = combine(frames)
    path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, NAME)
    storage.write_parquet(
        frame,
        path,
        provenance={
            "dataset": DATASET,
            "layer": storage.RAW,
            "source": "FAA Service Difficulty Reports, yearly CSV downloads",
            "source_url": PAGE,
            "years": f"{min(years)}-{max(years)}",
            "snapshot": snapshot,
            "retrieved_at": storage.modified_at(*(cache / f"SDR-{year}.csv.gz" for year in years)),
        },
    )
    print(f"wrote {frame.height} rows to {path}")
    return path


def build_processed(data_dir: Path, snapshot: str) -> Path:
    source = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, NAME)
    lookups = lookups_dir(data_dir, snapshot)
    frame, unparsed = process(pl.read_parquet(source), load_lookups(lookups))
    path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, NAME)
    provenance = storage.derived_provenance(
        source,
        data_dir,
        lookups=storage.object_key(lookups, data_dir),
        unparsed=json.dumps({column: n for column, n in unparsed.items() if n}),
    )
    storage.write_parquet(frame, path, provenance)
    print(f"wrote {frame.height} rows to {path}")
    print(f"values that did not parse (still in raw): {provenance['unparsed']}")
    with pl.Config(tbl_rows=-1, tbl_hide_dataframe_shape=True):
        print(label_coverage(frame))
    return path


def run(years: list[int], data_dir: Path, snapshot: str, steps: list[str], upload: bool) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        paths.append(build_raw(data_dir, snapshot, years))
    if storage.PROCESSED in steps:
        paths.append(build_processed(data_dir, snapshot))
    if upload:
        for path in paths:
            print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--years", type=int, nargs="+", help="file years for raw; default all")
    args = storage.parse_args(parser)
    years = args.years or list(range(FIRST_YEAR, datetime.now(UTC).year + 1))
    run(years, args.data_dir, args.snapshot, args.steps, args.upload)


if __name__ == "__main__":
    main()
