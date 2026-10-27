"""Shared fetching for FAA Dynamic Regulatory System (DRS) document types.

Caches every metadata page of a document type as gzipped JSON and, for types
whose text lives in files, each document's main file, so pipelines re-parse
without re-downloading. The cache sits at
`<data_dir>/cache/drs/<doctype>/<snapshot>/{pages,files}/`. Needs DRS_API_KEY.

    uv run python -m plane_text.drs SAIB AC --snapshot 2026-10-24   # cache metadata and files
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import time
from pathlib import Path
from typing import Any

from faa_drs import PAGE_SIZE, Document, DRSClient

from plane_text import storage

SAFE = re.compile(r"[^A-Za-z0-9._-]+")
SOURCE = "drs"


def cache_dir(data_dir: Path, doctype: str, snapshot: str) -> Path:
    """`<data_dir>/cache/drs/<doctype>/<snapshot>`."""
    return storage.cache_dir(data_dir, SOURCE) / doctype / snapshot


def page_files(cache: Path) -> list[Path]:
    return sorted((cache / "pages").glob("page-*.json.gz"))


def fetch_pages(drs: DRSClient, doctype: str, cache: Path) -> list[list[dict[str, Any]]]:
    """Every document's metadata, newest change first, one cached file per page.

    A cached page is reused, so an interrupted read resumes where it stopped.
    """
    pages: list[list[dict[str, Any]]] = []
    offset = 0
    while True:
        path = cache / "pages" / f"page-{offset:06d}.json.gz"
        if path.exists():
            docs = json.loads(gzip.decompress(path.read_bytes()))
        else:
            page = drs.list_documents(doctype, offset=offset, sort="DESC")
            docs = [doc.model_dump(mode="json") for doc in page.documents]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(json.dumps(docs).encode()))
        pages.append(docs)
        if len(docs) < PAGE_SIZE:
            print(f"{doctype}: {offset + len(docs)} documents in {len(pages)} pages")
            return pages
        offset += len(docs)


def file_path(cache: Path, doc: Document) -> Path | None:
    """Where a document's main file is cached: one directory per guid."""
    if not doc.download_url:
        return None
    return cache / "files" / SAFE.sub("_", doc.guid) / SAFE.sub("_", doc.file_name or "file")


def fetch_files(drs: DRSClient, records: list[dict[str, Any]], cache: Path) -> tuple[int, int]:
    """Download each document's main file once. Returns (downloaded, failed)."""
    downloaded = failed = 0
    for i, record in enumerate(records, 1):
        doc = Document.model_validate(record)
        path = file_path(cache, doc)
        if path is None or path.exists():
            continue
        try:
            data = drs.download(doc)
        except Exception as error:  # one bad file must not stop the run
            failed += 1
            print(f"  failed {doc.guid}: {error}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        downloaded += 1
        if downloaded % 100 == 0:
            print(f"  {i}/{len(records)} files")
        time.sleep(0.1)
    return downloaded, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("doctypes", nargs="+")
    parser.add_argument("--snapshot", required=True, help="cache snapshot date (YYYY-MM-DD)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--no-files", action="store_true", help="metadata only")
    args = parser.parse_args()
    with DRSClient() as drs:
        for doctype in args.doctypes:
            cache = cache_dir(args.data_dir, doctype, args.snapshot)
            records = [r for page in fetch_pages(drs, doctype, cache) for r in page]
            if not args.no_files:
                downloaded, failed = fetch_files(drs, records, cache)
                print(f"{doctype}: {downloaded} files downloaded, {failed} failed")


if __name__ == "__main__":
    main()
