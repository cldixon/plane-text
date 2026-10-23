"""FAA Service Difficulty Reports (SDRs), from the FAA's yearly CSV downloads.

One row per report: what failed, where on the aircraft, how it was found,
and a free-text discrepancy narrative. Each year's CSV is cached as gzip, so a
re-run only re-parses; the current year is always fetched again because it
grows as reports arrive.

    uv run python -m plane_text.faa_sdrs                # all years, write parquet
    uv run python -m plane_text.faa_sdrs --years 2024 2025
    uv run python -m plane_text.faa_sdrs --upload       # ...and upload to R2
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

from plane_text import storage

DATASET = "faa-sdrs"
FIRST_YEAR = 1995
URL = "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv"
PAGE = "https://www.faa.gov/av-info/download_SDR"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


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


# --- parsing ----------------------------------------------------------------


def parse_year(raw: bytes, year: int) -> pl.DataFrame:
    """One year's CSV as typed, snake_case columns, plus the file it came from."""
    frame = pl.read_csv(io.BytesIO(raw), infer_schema=False)
    frame = frame.rename({c: snake(c) for c in frame.columns})
    return frame.with_columns(pl.all().str.strip_chars().replace("", None)).with_columns(
        pl.col("difficulty_date").str.to_date("%m/%d/%Y", strict=False),
        pl.col("submission_date").str.to_datetime(time_zone="UTC", strict=False),
        pl.lit(year, pl.Int16).alias("file_year"),
    )


def combine(frames: list[pl.DataFrame]) -> pl.DataFrame:
    columns = frames[0].columns
    for frame in frames[1:]:
        if frame.columns != columns:
            raise ValueError(f"column layout changed in file year {frame['file_year'][0]}")
    return pl.concat(frames)


# --- pipeline ---------------------------------------------------------------


def run(years: list[int], data_dir: Path, upload: bool) -> Path:
    snapshot = datetime.now(UTC).date().isoformat()
    current = datetime.now(UTC).year
    cache = data_dir / "raw" / DATASET
    frames = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as http:
        for year in years:
            raw = fetch_year(http, year, cache, refresh=year == current)
            frames.append(parse_year(raw, year))
            print(f"{year}: {frames[-1].height} reports")

    frame = combine(frames)
    path = data_dir / DATASET / f"v{snapshot}" / "service-difficulty-reports.parquet"
    storage.write_parquet(
        frame,
        path,
        provenance={
            "dataset": DATASET,
            "source": "FAA Service Difficulty Reports, yearly CSV downloads",
            "source_url": PAGE,
            "years": f"{min(years)}-{max(years)}",
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
    parser.add_argument("--years", type=int, nargs="+", help="file years; default all")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--upload", action="store_true", help="upload the parquet to R2")
    args = parser.parse_args()
    years = args.years or list(range(FIRST_YEAR, datetime.now(UTC).year + 1))
    run(years, args.data_dir, args.upload)


if __name__ == "__main__":
    main()
