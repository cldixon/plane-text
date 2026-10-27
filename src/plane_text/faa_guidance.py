"""FAA guidance documents from DRS whose text lives in PDFs: SAIBs, advisory circulars and maintenance alerts.

One dataset per document type (`faa-saibs`, `faa-advisory-circulars`,
`faa-maintenance-alerts`), each with the same layout:

- raw `documents`: every DRS metadata record as read, one row each (duplicates
  kept), with the API's own field names and values as text, plus
  `page_offset`/`page_row` (as in faa_ads).
- raw `document-pages`: the text of each document's main PDF, one row per
  page: `guid`, `file_name`, `page` (1-based), `method` and `text`. `method`
  is `text_layer` (the PDF's embedded text), `ocr` (a scanned page read with
  Tesseract) or `ocr_pending` (a scanned page not yet OCR'd, holding what
  little the text layer had); a file that could not be read at all has one row
  with `method` `unreadable`, no page number and the error as its text. Text
  is as the extractor emits it (see pdf_text: runs of spaces and blank lines
  collapsed). The PDFs themselves are cached locally but not published.
- processed `documents`: built from the two raw tables. One row per guid:
  cleaned metadata, the page texts joined with blank lines, `pages`,
  `ocr_pages` and `ocr_pending_pages`.

    uv run python -m plane_text.faa_guidance SAIB AC                    # raw, then processed
    uv run python -m plane_text.faa_guidance SAIB AC --no-ocr           # no new OCR (cached OCR is used)
    uv run python -m plane_text.faa_guidance SAIB AC --step processed   # rebuild processed from raw
    uv run python -m plane_text.faa_guidance SAIB AC --upload           # ...and upload to R2

The raw step needs DRS_API_KEY (only to read what is not cached yet),
poppler-utils and, unless --no-ocr, tesseract-ocr; the processed step needs
none of them.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
from faa_drs import Document, DRSClient

from plane_text import drs, pdf_text, storage
from plane_text.faa_ads import clean_list, clean_text, raw_frame, records

DOCUMENTS = "documents"
PAGES = "document-pages"

TEXT_LAYER, OCR, OCR_PENDING, UNREADABLE = "text_layer", "ocr", "ocr_pending", "unreadable"
PAGES_SCHEMA = {
    "guid": pl.String,
    "file_name": pl.String,
    "page": pl.Int32,
    "method": pl.String,
    "text": pl.String,
}


@dataclass(frozen=True)
class DocType:
    code: str
    dataset: str
    name: str
    text: dict[str, str]  # column -> DRS field, single values
    lists: dict[str, str]  # column -> DRS field, list values
    dates: dict[str, str]  # column -> DRS field, YYYY-MM-DD values


DOCTYPES = {
    "SAIB": DocType(
        code="SAIB",
        dataset="faa-saibs",
        name="Special Airworthiness Information Bulletins",
        text={
            "document_number": "drs:documentNumber",
            "status": "drs:status",
            "title": "drs:title",
            "revision": "drs:revNum",
        },
        lists={
            "make": "drs:saibMake",
            "model": "drs:saibModel",
            "product_type": "drs:productType",
            "product_subtype": "drs:productSubType",
            "cfr_parts": "drs:partNumber",
            "cfr_sections": "drs:sectionNumber",
            "ad_references": "drs:ADReference",
        },
        dates={"issue_date": "drs:saibIssueDate"},
    ),
    "AC": DocType(
        code="AC",
        dataset="faa-advisory-circulars",
        name="Advisory Circulars",
        text={
            "document_number": "drs:documentNumber",
            "status": "drs:status",
            "title": "drs:title",
            "change_number": "drs:changeNumber",
            "change_description": "drs:changeDesc",
        },
        lists={
            "cancels": "drs:cancels",
            "cancelled_by": "drs:cancelledBy",
            "cfr_parts": "drs:partNumber",
            "cfr_sections": "drs:sectionNumber",
            "office": "drs:primaryRespOffice",
        },
        dates={"approval_date": "drs:approvalDate", "last_reviewed_date": "drs:lastReviewedDate"},
    ),
    # Monthly Aviation Maintenance Alerts (AC 43-16A); DRS holds the 2004-2012 issues.
    "ALERTS": DocType(
        code="ALERTS",
        dataset="faa-maintenance-alerts",
        name="Aviation Maintenance Alerts",
        text={
            "document_number": "drs:documentNumber",
            "status": "drs:status",
            "title": "drs:title",
            "year": "fsims:docLevel2",
        },
        lists={
            "cfr_parts": "drs:partNumber",
            "cfr_sections": "drs:sectionNumber",
            "specialties": "fsims:specialty",
            "office": "fsims:owner",
        },
        dates={"issue_date": "fsims:fileDate"},
    ),
}

TEXT_COLUMNS = {
    "text": pl.String,
    "pages": pl.Int32,
    "ocr_pages": pl.List(pl.Int32),
    "ocr_pending_pages": pl.List(pl.Int32),
    "file_name": pl.String,
    "drs_url": pl.String,
    "last_modified": pl.Datetime("us", "UTC"),
}


# --- raw --------------------------------------------------------------------


def page_rows(guid: str, file_name: str, extracted: pdf_text.Extracted) -> list[dict[str, Any]]:
    """One extraction result as raw page rows (see the module docstring)."""
    if extracted.error:
        error = {"page": None, "method": UNREADABLE, "text": extracted.error}
        return [{"guid": guid, "file_name": file_name} | error]
    ocr, pending = set(extracted.ocr_pages), set(extracted.ocr_pending_pages)
    return [
        {
            "guid": guid,
            "file_name": file_name,
            "page": number,
            "method": OCR if number in ocr else OCR_PENDING if number in pending else TEXT_LAYER,
            "text": text,
        }
        for number, text in enumerate(extracted.pages, 1)
    ]


def pdfs_by_guid(records: list[dict[str, Any]], cache: Path) -> dict[str, Path]:
    """Each document's cached main PDF, first record per guid."""
    pdfs: dict[str, Path] = {}
    for record in records:
        doc = Document.model_validate(record)
        path = drs.file_path(cache, doc)
        if doc.guid not in pdfs and path and path.exists() and path.suffix.lower() == ".pdf":
            pdfs[doc.guid] = path
    return pdfs


def pages_frame(pdfs: dict[str, Path], extracted: dict[Path, pdf_text.Extracted]) -> pl.DataFrame:
    rows = [row for guid, pdf in pdfs.items() for row in page_rows(guid, pdf.name, extracted[pdf])]
    return pl.DataFrame(rows, schema=PAGES_SCHEMA)


def build_raw(spec: DocType, data_dir: Path, snapshot: str, workers: int, ocr: bool) -> list[Path]:
    cache = drs.cache_dir(data_dir, spec.code, snapshot)
    with DRSClient() as client:
        pages = drs.fetch_pages(client, spec.code, cache)
        flat = [r for page in pages for r in page]
        downloaded, failed = drs.fetch_files(client, flat, cache)
    if downloaded or failed:
        print(f"{spec.code}: {downloaded} files downloaded, {failed} failed")

    provenance = {
        "dataset": spec.dataset,
        "layer": storage.RAW,
        "source": f"FAA Dynamic Regulatory System, document type {spec.code} ({spec.name})",
        "source_url": f"https://drs.faa.gov/api/drs/data-pull/{spec.code}",
        "snapshot": snapshot,
    }
    documents = raw_frame(pages)
    documents_path = storage.layer_path(data_dir, storage.RAW, spec.dataset, snapshot, DOCUMENTS)
    storage.write_parquet(
        documents,
        documents_path,
        provenance | {"table": DOCUMENTS, "retrieved_at": storage.modified_at(*drs.page_files(cache))},
    )
    print(f"{spec.code}: wrote {documents.height} rows to {documents_path}")

    pdfs = pdfs_by_guid(flat, cache)
    extracted = pdf_text.extract_all(sorted(pdfs.values()), workers, ocr)
    frame = pages_frame(pdfs, extracted)
    pages_path = storage.layer_path(data_dir, storage.RAW, spec.dataset, snapshot, PAGES)
    storage.write_parquet(
        frame,
        pages_path,
        provenance
        | {
            "table": PAGES,
            "source": provenance["source"] + ", text of each document's main PDF",
            "extraction": (
                "pdftotext -layout per page; pages with fewer than "
                f"{pdf_text.MIN_PAGE_CHARS} letters or digits read with Tesseract (eng, psm 1) "
                f"at {pdf_text.OCR_DPI} dpi, or left as ocr_pending when OCR is off"
            ),
            "files": str(len(pdfs)),
            "retrieved_at": storage.modified_at(*pdfs.values()),
            "extracted_at": storage.now(),
        },
    )
    print(f"{spec.code}: wrote {frame.height} page rows from {len(pdfs)} PDFs to {pages_path}")
    return [documents_path, pages_path]


# --- processed --------------------------------------------------------------


def unique_list(doc: Document, key: str) -> list[str] | None:
    """A list field with repeats removed, in order (DRS repeats CFR parts per section)."""
    values = clean_list(doc, key)
    return list(dict.fromkeys(values)) if values else None


def text_columns(pages: pl.DataFrame | None) -> dict[str, Any]:
    """`text`, `pages`, `ocr_pages`, `ocr_pending_pages` from a document's raw page rows.

    All null for a document without a PDF; an unreadable PDF has no text and 0 pages.
    """
    if pages is None:
        return {"text": None, "pages": None, "ocr_pages": None, "ocr_pending_pages": None}
    pages = pages.filter(pl.col("method") != UNREADABLE).sort("page")

    def numbers(method: str) -> list[int]:
        return pages.filter(pl.col("method") == method)["page"].to_list()

    return {
        "text": "\n\n".join(t for t in pages["text"] if t) or None,
        "pages": pages.height,
        "ocr_pages": numbers(OCR),
        "ocr_pending_pages": numbers(OCR_PENDING),
    }


def to_row(doc: Document, spec: DocType, pages: pl.DataFrame | None) -> dict[str, Any]:
    """One document's row; `pages` holds its raw page rows, or None if it has no PDF."""
    row: dict[str, Any] = {"guid": doc.guid}
    row |= {column: clean_text(doc.get(key)) for column, key in spec.text.items()}
    row |= {column: unique_list(doc, key) for column, key in spec.lists.items()}
    row |= {column: doc.get_date(key) for column, key in spec.dates.items()}
    row |= text_columns(pages)
    row |= {"file_name": doc.file_name, "drs_url": doc.url, "last_modified": doc.last_modified}
    return row


def schema(spec: DocType) -> dict[str, Any]:
    return {
        "guid": pl.String,
        **dict.fromkeys(spec.text, pl.String),
        **dict.fromkeys(spec.lists, pl.List(pl.String)),
        **dict.fromkeys(spec.dates, pl.Date),
        **TEXT_COLUMNS,
    }


def to_frame(records: list[dict[str, Any]], pages: pl.DataFrame, spec: DocType) -> pl.DataFrame:
    """One row per guid (the first record seen), with its PDF text from the raw page rows."""
    by_guid = {key[0]: group for key, group in pages.group_by("guid", maintain_order=True)}
    seen: set[str] = set()
    rows = []
    for record in records:
        doc = Document.model_validate(record)
        if doc.guid in seen:
            continue  # a read across a DRS update can return a document twice
        seen.add(doc.guid)
        rows.append(to_row(doc, spec, by_guid.get(doc.guid)))
    return pl.DataFrame(rows, schema=schema(spec))


def build_processed(spec: DocType, data_dir: Path, snapshot: str) -> Path:
    documents = storage.layer_path(data_dir, storage.RAW, spec.dataset, snapshot, DOCUMENTS)
    pages = storage.layer_path(data_dir, storage.RAW, spec.dataset, snapshot, PAGES)
    frame = to_frame(records(pl.read_parquet(documents)), pl.read_parquet(pages), spec)
    path = storage.layer_path(data_dir, storage.PROCESSED, spec.dataset, snapshot, DOCUMENTS)
    provenance = storage.derived_provenance(
        documents,
        data_dir,
        table=DOCUMENTS,
        built_from=", ".join(storage.object_key(p, data_dir) for p in (documents, pages)),
        text_extracted_at=storage.read_provenance(pages).get("extracted_at", ""),
    )
    storage.write_parquet(frame, path, provenance)
    print(f"{spec.code}: wrote {frame.height} rows to {path}")
    return path


# --- pipeline ---------------------------------------------------------------


def run(
    codes: list[str], data_dir: Path, snapshot: str, steps: list[str], upload: bool, workers: int, ocr: bool
) -> list[Path]:
    paths = []
    for code in codes:
        spec = DOCTYPES[code]
        if storage.RAW in steps:
            paths += build_raw(spec, data_dir, snapshot, workers, ocr)
        if storage.PROCESSED in steps:
            paths.append(build_processed(spec, data_dir, snapshot))
    if upload:
        for path in paths:
            print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codes", nargs="+", choices=sorted(DOCTYPES))
    parser.add_argument("--workers", type=int, default=4, help="PDFs read in parallel (raw step)")
    parser.add_argument(
        "--no-ocr", action="store_true", help="raw step: use cached OCR where present, text layer otherwise"
    )
    args = storage.parse_args(parser)
    run(args.codes, args.data_dir, args.snapshot, args.steps, args.upload, args.workers, ocr=not args.no_ocr)


if __name__ == "__main__":
    main()
