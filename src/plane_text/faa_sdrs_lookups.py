"""Lookup tables for the coded columns in the FAA SDR dataset.

Reads the FAA's SDRS Look-Up Tables (a zip of Excel sheets, the drop-down
lists the reporting system uses) and the 2008 JASC code table PDF, and writes
one small parquet per code set. The SDR table itself is left as published;
joining codes to labels is a later step.

    uv run python -m plane_text.faa_sdrs_lookups            # write parquet
    uv run python -m plane_text.faa_sdrs_lookups --upload   # ...and upload to R2
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl
from pypdf import PdfReader

from plane_text import storage

DATASET = "faa-sdrs-lookups"
LOOKUP_ZIP = "https://sdrs.faa.gov/Documents/SDRS%20Look-Up%20Tables.zip"
JASC_PDF = "https://sdrs.faa.gov/documents/JASC_Code.pdf"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

JASC_TITLE = re.compile(r"(\d{4}) ([A-Z0-9][A-Z0-9 ,/&()'.\-]*)")
JASC_CHAPTER = re.compile(r"(\d{2})\s{2,}([A-Z][A-Z ,/&()'.\-]*)")
SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Sheet:
    """One sheet in the zip: its file-name suffix, output table, and column names."""

    suffix: str
    table: str
    columns: tuple[str, ...]


SHEETS = (
    Sheet("HowDiscovered", "how_discovered", ("code", "description")),
    Sheet("StageofOperation", "stage_of_operation", ("code", "description")),
    Sheet("NatureofCondition", "nature_of_condition", ("code", "description")),
    Sheet("PrecautionCode", "precautionary_procedure", ("code", "description")),
    Sheet("SubmitterType", "submitter_type", ("code", "description")),
    Sheet("OperatorType", "sdr_type", ("code", "description")),
    Sheet("TimeSinceCode", "time_since", ("code", "description")),
    Sheet("PartLocation", "part_location_zone", ("code", "description")),
    Sheet("PartCondition", "part_condition", ("part_condition",)),
    Sheet("PartName", "part_name", ("part_name",)),
    Sheet("PartMake", "part_make", ("code", "name")),
    Sheet("AirCraft", "aircraft_make_model", ("make", "model")),
    Sheet("Engine", "engine_make_model", ("make", "model")),
    Sheet("Propeller", "propeller_make_model", ("make", "model")),
    Sheet("Component", "component_make_model", ("name", "model")),
)


def clean(column: str) -> pl.Expr:
    return pl.col(column).str.replace_all(r"\s+", " ").str.strip_chars().replace("", None)


# --- fetching ---------------------------------------------------------------


def fetch(http: httpx.Client, url: str, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    response = http.get(url)
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


# --- parsing ----------------------------------------------------------------


def find_sheet(archive: zipfile.ZipFile, suffix: str) -> zipfile.ZipInfo:
    """The zip names sheets `<date>_<Name>.xlsx`; the date changes between releases."""
    matches = [i for i in archive.infolist() if i.filename.endswith(f"_{suffix}.xlsx")]
    if len(matches) != 1:
        raise LookupError(f"expected one *_{suffix}.xlsx in the lookup zip, found {len(matches)}")
    return matches[0]


def read_sheet(archive: zipfile.ZipFile, sheet: Sheet) -> tuple[pl.DataFrame, str]:
    """A sheet as all-text columns, cleaned and deduplicated, with the sheet's date."""
    info = find_sheet(archive, sheet.suffix)
    frame = pl.read_excel(io.BytesIO(archive.read(info)), infer_schema_length=0)
    if frame.width != len(sheet.columns):
        raise ValueError(f"{info.filename}: expected {len(sheet.columns)} columns, got {frame.columns}")
    frame = frame.rename(dict(zip(frame.columns, sheet.columns, strict=True)))
    frame = frame.with_columns(clean(c) for c in sheet.columns)
    frame = frame.filter(pl.col(sheet.columns[0]).is_not_null()).unique(maintain_order=True)
    return frame, info.filename.split("_", 1)[0]


def parse_jasc_lines(lines: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Short JASC titles (`2710 AILERON CONTROL SYSTEM`) and chapter names (`27   FLIGHT CONTROLS`).

    The PDF repeats codes in its definitions section in title case; only the
    uppercase code table matches, and the first title seen for a code wins.
    """
    titles: dict[str, str] = {}
    chapters: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if m := JASC_TITLE.fullmatch(line):
            titles.setdefault(m.group(1), SPACES.sub(" ", m.group(2)).strip())
        elif m := JASC_CHAPTER.fullmatch(line):
            chapters.setdefault(m.group(1), SPACES.sub(" ", m.group(2)).strip())
    return titles, chapters


def parse_jasc_pdf(pdf: bytes) -> tuple[dict[str, str], dict[str, str]]:
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings are noise here
    pages = PdfReader(io.BytesIO(pdf)).pages
    return parse_jasc_lines([line for page in pages for line in page.extract_text().splitlines()])


def jasc_table(sheet: pl.DataFrame, titles: dict[str, str], chapters: dict[str, str]) -> pl.DataFrame:
    """Every JASC code in either source: chapter, short title (2008), description (2026)."""
    pdf = pl.DataFrame(
        {"code": list(titles), "title": list(titles.values())},
        schema={"code": pl.String, "title": pl.String},
    )
    chapter = pl.DataFrame(
        {"chapter": list(chapters), "chapter_title": list(chapters.values())},
        schema={"chapter": pl.String, "chapter_title": pl.String},
    )
    return (
        sheet.rename({"description": "reporting_instructions"})
        .join(pdf, on="code", how="full", coalesce=True)
        .with_columns(pl.col("code").str.slice(0, 2).alias("chapter"))
        .join(chapter, on="chapter", how="left")
        .select("code", "chapter", "chapter_title", "title", "reporting_instructions")
        .sort("code")
    )


# --- pipeline ---------------------------------------------------------------


def run(data_dir: Path, upload: bool) -> list[Path]:
    snapshot = datetime.now(UTC).date().isoformat()
    cache = data_dir / "raw" / DATASET / snapshot
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True) as http:
        archive = zipfile.ZipFile(io.BytesIO(fetch(http, LOOKUP_ZIP, cache / "lookup-tables.zip")))
        pdf = fetch(http, JASC_PDF, cache / "JASC_Code.pdf")

    tables: dict[str, tuple[pl.DataFrame, str, str]] = {}
    for sheet in SHEETS:
        frame, sheet_date = read_sheet(archive, sheet)
        tables[sheet.table] = (frame, LOOKUP_ZIP, sheet_date)
    jasc_sheet, sheet_date = read_sheet(archive, Sheet("JASC", "jasc", ("code", "description")))
    titles, chapters = parse_jasc_pdf(pdf)
    tables["jasc"] = (jasc_table(jasc_sheet, titles, chapters), f"{LOOKUP_ZIP} and {JASC_PDF}", sheet_date)

    out = data_dir / DATASET / f"v{snapshot}"
    paths = []
    retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
    for table, (frame, source_url, sheet_date) in tables.items():
        path = out / f"{table}.parquet"
        storage.write_parquet(
            frame,
            path,
            provenance={
                "dataset": DATASET,
                "table": table,
                "source": "FAA SDRS Look-Up Tables",
                "source_url": source_url,
                "source_date": sheet_date,
                "snapshot": snapshot,
                "retrieved_at": retrieved_at,
            },
        )
        print(f"{table}: {frame.height} rows")
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
