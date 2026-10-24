"""FAA guidance documents from DRS whose text lives in PDFs: SAIBs and advisory circulars.

One row per DRS document: its metadata, plus the text of its main PDF. The
text layer is used where a page has one and OCR where it does not (see
pdf_text), with the OCR'd page numbers recorded per row.

    uv run python -m plane_text.faa_guidance SAIB AC            # write parquet
    uv run python -m plane_text.faa_guidance SAIB AC --upload   # ...and upload to R2

Needs DRS_API_KEY, poppler-utils and tesseract-ocr.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from faa_drs import Document, DRSClient

from plane_text import drs, pdf_text, storage
from plane_text.faa_ads import clean_list, clean_text


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
}

TEXT_COLUMNS = {
    "text": pl.String,
    "pages": pl.Int32,
    "ocr_pages": pl.List(pl.Int32),
    "file_name": pl.String,
    "drs_url": pl.String,
    "last_modified": pl.Datetime("us", "UTC"),
}


def unique_list(doc: Document, key: str) -> list[str] | None:
    """A list field with repeats removed, in order (DRS repeats CFR parts per section)."""
    values = clean_list(doc, key)
    return list(dict.fromkeys(values)) if values else None


def to_row(doc: Document, spec: DocType, extracted: pdf_text.Extracted | None) -> dict[str, Any]:
    row: dict[str, Any] = {"guid": doc.guid}
    row |= {column: clean_text(doc.get(key)) for column, key in spec.text.items()}
    row |= {column: unique_list(doc, key) for column, key in spec.lists.items()}
    row |= {column: doc.get_date(key) for column, key in spec.dates.items()}
    row |= {
        "text": (extracted.text or None) if extracted else None,
        "pages": len(extracted.pages) if extracted else None,
        "ocr_pages": extracted.ocr_pages if extracted else None,
        "file_name": doc.file_name,
        "drs_url": doc.url,
        "last_modified": doc.last_modified,
    }
    return row


def schema(spec: DocType) -> dict[str, Any]:
    return {
        "guid": pl.String,
        **dict.fromkeys(spec.text, pl.String),
        **dict.fromkeys(spec.lists, pl.List(pl.String)),
        **dict.fromkeys(spec.dates, pl.Date),
        **TEXT_COLUMNS,
    }


def build(records: list[dict[str, Any]], spec: DocType, cache: Path, workers: int) -> pl.DataFrame:
    docs = [Document.model_validate(r) for r in records]
    docs = list({d.guid: d for d in docs}.values())  # a read across a DRS update can repeat one
    pdfs = {
        d.guid: p
        for d in docs
        if (p := drs.file_path(cache, d)) and p.exists() and p.suffix.lower() == ".pdf"
    }
    extracted = pdf_text.extract_all(sorted(pdfs.values()), workers)
    rows = [to_row(d, spec, extracted.get(pdfs[d.guid]) if d.guid in pdfs else None) for d in docs]
    return pl.DataFrame(rows, schema=schema(spec))


def run(codes: list[str], data_dir: Path, upload: bool, workers: int) -> list[Path]:
    snapshot = datetime.now(UTC).date().isoformat()
    paths = []
    with DRSClient() as client:
        for code in codes:
            spec = DOCTYPES[code]
            cache = drs.cache_dir(data_dir, code, snapshot)
            records = drs.fetch_pages(client, code, cache)
            drs.fetch_files(client, records, cache)
            frame = build(records, spec, cache, workers)
            path = data_dir / spec.dataset / f"v{snapshot}" / "documents.parquet"
            storage.write_parquet(
                frame,
                path,
                provenance={
                    "dataset": spec.dataset,
                    "source": f"FAA Dynamic Regulatory System, document type {code} ({spec.name})",
                    "source_url": f"https://drs.faa.gov/browse/{code}/doctypeDetails",
                    "snapshot": snapshot,
                    "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            print(f"{code}: wrote {frame.height} rows to {path}")
            paths.append(path)
            if upload:
                print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codes", nargs="+", choices=sorted(DOCTYPES))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--upload", action="store_true", help="upload the parquet to R2")
    args = parser.parse_args()
    run(args.codes, args.data_dir, args.upload, args.workers)


if __name__ == "__main__":
    main()
