"""Lookup tables for the coded columns in the FAA SDR dataset.

Reads the FAA's SDRS Look-Up Tables (a zip of Excel sheets, the drop-down
lists the reporting system uses) and the 2008 JASC code table PDF.

- raw: one parquet per workbook in the zip, named after it (`HowDiscovered`,
  `JASC`, ...), with the sheet's own headers and every cell as text, and the
  JASC PDF's extracted text lines (`JASC_Code`), one row per line.
- processed: one small, cleaned table per code set (`how_discovered`, `jasc`,
  ...), built from raw. The processed SDR table takes its labels from these.

    uv run python -m plane_text.faa_sdrs_lookups                 # raw, then processed
    uv run python -m plane_text.faa_sdrs_lookups --step raw      # only raw (or processed)
    uv run python -m plane_text.faa_sdrs_lookups --upload        # ...and upload to R2
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import polars as pl
from pypdf import PdfReader

from plane_text import storage

DATASET = "faa-sdrs-lookups"
LOOKUP_ZIP = "https://sdrs.faa.gov/Documents/SDRS%20Look-Up%20Tables.zip"
JASC_PDF = "https://sdrs.faa.gov/documents/JASC_Code.pdf"
JASC_LINES = "JASC_Code"  # raw table of the PDF's text lines
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

JASC_TITLE = re.compile(r"(\d{4}) ([A-Z0-9][A-Z0-9 ,/&()'.\-]*)")
JASC_CHAPTER = re.compile(r"(\d{2})\s{2,}([A-Z][A-Z ,/&()'.\-]*)")
SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Sheet:
    """One workbook in the zip: its name (the raw table), processed table, and column names."""

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
JASC_SHEET = Sheet("JASC", "jasc", ("code", "description"))


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


# --- raw --------------------------------------------------------------------


def sheet_name(filename: str) -> str:
    """The zip names workbooks `<date>_<Name>.xlsx`; the date changes between releases."""
    return filename.removesuffix(".xlsx").split("_", 1)[-1]


def read_workbooks(archive: zipfile.ZipFile) -> dict[str, tuple[pl.DataFrame, str]]:
    """Every workbook in the zip as delivered: its sheet's headers, all cells as text.

    Keyed by workbook name, with the zip member each came from.
    """
    workbooks = {}
    for info in archive.infolist():
        if not info.filename.endswith(".xlsx"):
            continue
        frame = pl.read_excel(io.BytesIO(archive.read(info)), infer_schema_length=0)
        workbooks[sheet_name(info.filename)] = (frame, info.filename)
    return workbooks


def require_workbooks(names: set[str]) -> None:
    """Fail early when a release drops or renames a workbook the processed tables need."""
    if missing := {sheet.suffix for sheet in (*SHEETS, JASC_SHEET)} - names:
        raise LookupError(f"the lookup zip has no workbook for {sorted(missing)}")


def pdf_lines(pdf: bytes) -> pl.DataFrame:
    """The PDF's extracted text, one row per line, numbered from 1 within each page."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings are noise here
    rows = [
        (page_number, line_number, line)
        for page_number, page in enumerate(PdfReader(io.BytesIO(pdf)).pages, start=1)
        for line_number, line in enumerate(page.extract_text().splitlines(), start=1)
    ]
    return pl.DataFrame(rows, schema={"page": pl.Int16, "line": pl.Int32, "text": pl.String}, orient="row")


# --- processed --------------------------------------------------------------


def tidy(frame: pl.DataFrame, sheet: Sheet) -> pl.DataFrame:
    """A raw sheet renamed to `sheet.columns`, whitespace collapsed, empty and duplicate rows dropped."""
    if frame.width != len(sheet.columns):
        raise ValueError(f"{sheet.suffix}: expected {len(sheet.columns)} columns, got {frame.columns}")
    frame = frame.rename(dict(zip(frame.columns, sheet.columns, strict=True)))
    frame = frame.with_columns(clean(c) for c in sheet.columns)
    return frame.filter(pl.col(sheet.columns[0]).is_not_null()).unique(maintain_order=True)


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


def build_raw(data_dir: Path, snapshot: str) -> list[Path]:
    """Download (or reuse the cached) zip and PDF and write each as delivered to raw."""
    cache = storage.cache_dir(data_dir, DATASET) / snapshot
    zip_path, pdf_path = cache / "lookup-tables.zip", cache / "JASC_Code.pdf"
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True) as http:
        archive = zipfile.ZipFile(io.BytesIO(fetch(http, LOOKUP_ZIP, zip_path)))
        pdf = fetch(http, JASC_PDF, pdf_path)

    workbooks = read_workbooks(archive)
    require_workbooks(set(workbooks))
    tables = {
        name: (frame, LOOKUP_ZIP, member, member.split("_", 1)[0], storage.modified_at(zip_path))
        for name, (frame, member) in workbooks.items()
    }
    tables[JASC_LINES] = (pdf_lines(pdf), JASC_PDF, pdf_path.name, "2008", storage.modified_at(pdf_path))

    paths = []
    for name, (frame, source_url, source_file, source_date, retrieved_at) in tables.items():
        path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, name)
        storage.write_parquet(
            frame,
            path,
            provenance={
                "dataset": DATASET,
                "layer": storage.RAW,
                "table": name,
                "source": "FAA SDRS Look-Up Tables",
                "source_url": source_url,
                "source_file": source_file,
                "source_date": source_date,
                "snapshot": snapshot,
                "retrieved_at": retrieved_at,
            },
        )
        print(f"raw {name}: {frame.height} rows")
        paths.append(path)
    return paths


def build_processed(data_dir: Path, snapshot: str) -> list[Path]:
    """Clean each raw sheet into its code table, and combine the JASC sheet and PDF into `jasc`."""

    def raw(name: str) -> Path:
        return storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, name)

    tables: dict[str, tuple[pl.DataFrame, Path, dict[str, str]]] = {}
    for sheet in SHEETS:
        tables[sheet.table] = (tidy(pl.read_parquet(raw(sheet.suffix)), sheet), raw(sheet.suffix), {})
    lines = pl.read_parquet(raw(JASC_LINES))["text"].to_list()
    titles, chapters = parse_jasc_lines(lines)
    jasc = jasc_table(tidy(pl.read_parquet(raw(JASC_SHEET.suffix)), JASC_SHEET), titles, chapters)
    pdf_key = storage.object_key(raw(JASC_LINES), data_dir)
    tables["jasc"] = (
        jasc,
        raw(JASC_SHEET.suffix),
        {"source_url": f"{LOOKUP_ZIP} and {JASC_PDF}", "also_built_from": pdf_key},
    )

    paths = []
    for table, (frame, source, extra) in tables.items():
        path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, table)
        storage.write_parquet(frame, path, storage.derived_provenance(source, data_dir, table=table, **extra))
        print(f"processed {table}: {frame.height} rows")
        paths.append(path)
    return paths


def run(data_dir: Path, snapshot: str, steps: list[str], upload: bool) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        paths += build_raw(data_dir, snapshot)
    if storage.PROCESSED in steps:
        paths += build_processed(data_dir, snapshot)
    if upload:
        for path in paths:
            storage.upload(path, data_dir)
        print(f"uploaded {len(paths)} tables to r2://{storage.bucket()}/")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = storage.parse_args(parser)
    run(args.data_dir, args.snapshot, args.steps, args.upload)


if __name__ == "__main__":
    main()
