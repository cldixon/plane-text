"""FAA airworthiness directives (AD final rules) from the DRS API, via faa-drs.

One row per DRS document. Corrections are published as their own documents
and share the AD number of the AD they correct, so `ad_number` is not unique;
`guid` is. Pages are cached as gzipped JSON so a re-run only re-parses.

- raw: every record as read, one row each (duplicates kept), with the API's
  own field names (`documentGuid`, `drs:title`, ...) and values as text;
  list-valued DRS fields stay lists of text. Plus `page_offset`/`page_row`.
- processed: built from raw. One row per guid, text cleaned, dates typed.

    uv run python -m plane_text.faa_ads                       # raw, then processed
    uv run python -m plane_text.faa_ads --step processed      # rebuild processed from raw
    uv run python -m plane_text.faa_ads --upload              # ...and upload to R2

The raw step needs DRS_API_KEY in the environment; the processed step does not.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any

import polars as pl
from faa_drs import PAGE_SIZE, DocType, Document, DRSClient

from plane_text import storage

DATASET = "faa-ads"
NAME = "ad-final-rules"
DOCTYPE = DocType.ADFRAWD

# Values DRS uses in place of a missing one.
PLACEHOLDERS = {"", "Unknown", "This information is not available."}
SPACES = re.compile(r"[ \t\xa0]+")
BLANK_LINES = re.compile(r"\n{3,}")

SCHEMA = {
    "guid": pl.String,
    "ad_number": pl.String,
    "status": pl.String,
    "action": pl.String,
    "title": pl.String,
    "subject": pl.String,
    "docket_number": pl.String,
    "amendment": pl.String,
    "make": pl.List(pl.String),
    "model": pl.List(pl.String),
    "product_type": pl.List(pl.String),
    "product_subtype": pl.List(pl.String),
    "supersedes": pl.List(pl.String),
    "superseded_by": pl.List(pl.String),
    "affects": pl.List(pl.String),
    "affected_by": pl.List(pl.String),
    "citation": pl.String,
    "pages": pl.String,
    "publish_date": pl.Date,
    "issue_date": pl.Date,
    "effective_date": pl.Date,
    "summary": pl.String,
    "supplementary_info": pl.String,
    "regulatory_text": pl.String,
    "last_modified": pl.Datetime("us", "UTC"),
    "drs_url": pl.String,
    "file_name": pl.String,
}

TEXT_FIELDS = {
    "status": "drs:status",
    "action": "drs:adfrawdAction",
    "title": "drs:title",
    "subject": "drs:adfrawdSubject",
    "docket_number": "drs:adfrawdDocketNo",
    "amendment": "drs:adfrawdAmendment",
    "citation": "drs:adfrawdCitation",
    "pages": "drs:adfrawdPageNumber",
}
LIST_FIELDS = {
    "make": "drs:adfrawdMake",
    "model": "drs:adfrawdModel",
    "product_type": "drs:adfrawdProductType",
    "product_subtype": "drs:adfrawdProductSubType",
    "supersedes": "drs:adfrawdSupersededAD",
    "superseded_by": "drs:adfrawdSupersededBy",
    "affects": "drs:adfrawdAffectedAD",
    "affected_by": "drs:adfrawdAffectedBy",
}
DATE_FIELDS = {
    "publish_date": "drs:adfrawdCitationPublishDate",
    "issue_date": "drs:adfrawdIssueDate",
    "effective_date": "drs:effectiveDate",
}
BODY_FIELDS = {
    "summary": "drs:adfrawdSummary",
    "supplementary_info": "drs:adfrawdSupplementaryInfo",
    "regulatory_text": "drs:adfrawdRegulatoryText",
}


# --- fetching ---------------------------------------------------------------


def fetch_pages(drs: DRSClient, cache: Path) -> list[list[dict[str, Any]]]:
    """Read every page, newest change first, caching each page as it arrives.

    A cached page is reused, so an interrupted read resumes where it stopped.
    """
    pages: list[list[dict[str, Any]]] = []
    offset = 0
    while True:
        path = cache / f"page-{offset:06d}.json.gz"
        if path.exists():
            docs = json.loads(gzip.decompress(path.read_bytes()))
        else:
            page = drs.list_documents(DOCTYPE, offset=offset, sort="DESC")
            docs = [doc.model_dump(mode="json") for doc in page.documents]
            cache.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(json.dumps(docs).encode()))
        print(f"offset {offset}: {len(docs)} documents")
        pages.append(docs)
        if len(docs) < PAGE_SIZE:
            return pages
        offset += len(docs)


# --- raw --------------------------------------------------------------------

# The faa-drs SDK's names for a document's common fields, mapped to the API's.
API_NAMES = {name: field.alias or name for name, field in Document.model_fields.items() if name != "metadata"}
PAGE_COLUMNS = ["page_offset", "page_row"]


def as_text(value: Any) -> Any:
    """A JSON value as text: booleans as `true`/`false`, lists item by item, null kept."""
    if isinstance(value, list):
        return [as_text(item) for item in value]
    if isinstance(value, bool):
        return json.dumps(value)
    return None if value is None else str(value)


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    """A cached record as the API delivered it: common fields under their API names, then metadata."""
    common = {API_NAMES[k]: as_text(v) for k, v in record.items() if k != "metadata"}
    return common | {k: as_text(v) for k, v in record["metadata"].items()}


def raw_frame(pages: list[list[dict[str, Any]]]) -> pl.DataFrame:
    """Every record as read, one row each, with where it sits in the read (`page_offset`, `page_row`).

    A field is a list column if any record has a list there, text otherwise.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    for docs in pages:
        rows += [flatten(r) | {"page_offset": offset, "page_row": i} for i, r in enumerate(docs)]
        offset += len(docs)
    fields = list(dict.fromkeys(k for row in rows for k in row if k not in PAGE_COLUMNS))
    lists = {k for row in rows for k, v in row.items() if isinstance(v, list)}
    for row in rows:
        if mixed := [k for k in lists if row.get(k) is not None and not isinstance(row[k], list)]:
            raise ValueError(f"{row.get('documentGuid')}: text where other records have lists: {mixed}")
    schema = {k: pl.List(pl.String) if k in lists else pl.String for k in fields}
    return pl.DataFrame(rows, schema=schema | {"page_offset": pl.Int32, "page_row": pl.Int16})


def records(raw: pl.DataFrame) -> list[dict[str, Any]]:
    """Raw rows back into API-shaped records, for `Document.model_validate`."""
    flag = API_NAMES["has_attachments"]
    return [
        row | {flag: json.loads(row[flag]) if row[flag] else False}
        for row in raw.drop(PAGE_COLUMNS).iter_rows(named=True)
    ]


# --- processed --------------------------------------------------------------


def clean_text(value: Any) -> str | None:
    """Collapse runs of spaces, keep line breaks, and drop placeholders."""
    if not isinstance(value, str):
        return None
    lines = (SPACES.sub(" ", line).strip() for line in value.replace("\r\n", "\n").split("\n"))
    text = BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    return None if text in PLACEHOLDERS else text


def clean_list(doc: Document, key: str) -> list[str] | None:
    items = [item for item in (clean_text(v) for v in doc.get_list(key)) if item]
    return items or None


def to_row(doc: Document) -> dict[str, Any]:
    row: dict[str, Any] = {"guid": doc.guid, "ad_number": clean_text(doc.number)}
    row |= {column: clean_text(doc.get(key)) for column, key in TEXT_FIELDS.items()}
    row |= {column: clean_list(doc, key) for column, key in LIST_FIELDS.items()}
    row |= {column: doc.get_date(key) for column, key in DATE_FIELDS.items()}
    row |= {column: clean_text(doc.get(key)) for column, key in BODY_FIELDS.items()}
    row |= {"last_modified": doc.last_modified, "drs_url": doc.url, "file_name": doc.file_name}
    return row


def to_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    """Rows from raw document records, one per guid, keeping the first seen."""
    seen: set[str] = set()
    rows = []
    for record in records:
        doc = Document.model_validate(record)
        if doc.guid in seen:
            continue  # a read across a DRS update can return a document twice
        seen.add(doc.guid)
        rows.append(to_row(doc))
    return pl.DataFrame(rows, schema=SCHEMA)


# --- pipeline ---------------------------------------------------------------


def build_raw(data_dir: Path, snapshot: str) -> Path:
    cache = storage.cache_dir(data_dir, DATASET) / snapshot
    with DRSClient() as drs:
        pages = fetch_pages(drs, cache)

    frame = raw_frame(pages)
    path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, NAME)
    storage.write_parquet(
        frame,
        path,
        provenance={
            "dataset": DATASET,
            "layer": storage.RAW,
            "source": f"FAA Dynamic Regulatory System, document type {DOCTYPE}",
            "source_url": "https://drs.faa.gov/api/drs/data-pull/ADFRAWD",
            "snapshot": snapshot,
            "retrieved_at": storage.modified_at(*cache.glob("page-*.json.gz")),
        },
    )
    print(f"wrote {frame.height} rows to {path}")
    return path


def build_processed(data_dir: Path, snapshot: str) -> Path:
    source = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, NAME)
    frame = to_frame(records(pl.read_parquet(source)))
    path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, NAME)
    storage.write_parquet(frame, path, storage.derived_provenance(source, data_dir))
    print(f"wrote {frame.height} rows to {path}")
    return path


def run(data_dir: Path, snapshot: str, steps: list[str], upload: bool) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        paths.append(build_raw(data_dir, snapshot))
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
