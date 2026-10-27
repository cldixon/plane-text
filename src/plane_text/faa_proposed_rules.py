"""FAA proposed rules from DRS: AD proposed rules (ADNPRM) and general rulemaking proposals (NPRM).

DRS carries the Federal Register sections inline, but for many documents
since 2013 it holds only a link to the Federal Register in place of the
supplementary information and regulatory text. For those, the full document
text is fetched from the Federal Register API (public domain) and cached at
`<data_dir>/cache/federal-register/<document number>.txt`.

- raw `ad-proposed-rules` and `proposed-rules`: every ADNPRM and NPRM record
  as read, one row each (duplicates kept), with the API's own field names
  and values as text (list-valued fields stay lists of text), plus
  `page_offset`/`page_row` (as in faa_ads).
- raw `federal-register-documents`: one row per cached Federal Register text
  that a record links to: `document_number`, `raw_text` (the API's plain
  text, as fetched) and `retrieved_at`. It may be empty.
- processed `ad-proposed-rules` and `proposed-rules`: built from raw. One row
  per guid, text cleaned, dates typed, the Federal Register number parsed
  from the link and its text joined in as `federal_register_text`. `action`
  and `title` are one line (DRS wraps some, escapes some line breaks as a
  literal `\\n` and ends some with a row of dashes); escaped line breaks in
  the body sections are restored.

    uv run python -m plane_text.faa_proposed_rules                    # raw, then processed
    uv run python -m plane_text.faa_proposed_rules --step processed   # rebuild processed from raw
    uv run python -m plane_text.faa_proposed_rules --upload           # ...and upload to R2

The raw step needs DRS_API_KEY (only to read what is not cached yet) and,
for Federal Register texts not yet cached, access to www.federalregister.gov;
without it those rows keep only the link (the first network failure stops
further attempts). The processed step needs neither.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import polars as pl
from faa_drs import Document, DRSClient

from plane_text import drs, storage
from plane_text.faa_ads import clean_list, clean_text, raw_frame, records

DATASET = "faa-proposed-rules"
FEDERAL_REGISTER = "federal-register-documents"
FR_SOURCE = "federal-register"
FR_API = "https://www.federalregister.gov/api/v1/documents/{number}.json?fields[]=raw_text_url"
# The first link is the document itself; corrections and extensions may follow.
# Numbers look like 2019-06275, E6-10596 or R1-2022-22698.
FR_LINK = re.compile(
    r"(?:federalregister\.gov/(?:a|d|documents/\d{4}/\d{2}/\d{2})|gpo\.gov/fdsys/pkg/FR-[\d-]+/pdf)/([A-Za-z0-9]+(?:-\d+)+)"
)
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"
FR_SCHEMA = {"document_number": pl.String, "raw_text": pl.String, "retrieved_at": pl.String}
RULE_LINE = re.compile(r"^-{5,}$", re.MULTILINE)


@dataclass(frozen=True)
class DocType:
    code: str
    table: str
    prefix: str
    text: dict[str, str]
    lists: dict[str, str]
    dates: dict[str, str]


def fields(prefix: str, names: dict[str, str]) -> dict[str, str]:
    return {
        column: name if name.startswith("drs:") else f"drs:{prefix}{name}" for column, name in names.items()
    }


AD_NPRM = DocType(
    code="ADNPRM",
    table="ad-proposed-rules",
    prefix="adnprm",
    text=fields(
        "adnprm",
        {
            "docket_number": "drs:documentNumber",
            "product_identifier": "ProductIdentifier",
            "status": "drs:status",
            "action": "Action",
            "title": "drs:title",
            "citation": "Citation",
            "pages": "PageNumber",
        },
    ),
    lists=fields(
        "adnprm",
        {
            "make": "Make",
            "model": "Model",
            "product_type": "ProductType",
            "product_subtype": "ProductSubtype",
        },
    ),
    dates=fields(
        "adnprm",
        {"publish_date": "CitationPublishDate", "issue_date": "IssueDate", "comment_date": "CommentDate"},
    ),
)
NPRM = DocType(
    code="NPRM",
    table="proposed-rules",
    prefix="nprm",
    text=fields(
        "nprm",
        {
            "notice_number": "drs:documentNumber",
            "docket_number": "Docket",
            "status": "drs:status",
            "action": "Action",
            "title": "drs:title",
            "citation": "Citation",
            "pages": "PageNumber",
        },
    ),
    lists=fields("nprm", {"cfr_parts": "drs:partNumber"}),
    dates=fields("nprm", {"issue_date": "IssueDate", "comment_date": "CommentDate"}),
)
DOCTYPES = (AD_NPRM, NPRM)
BODY = {"summary": "Summary", "supplementary_info": "SupplementaryInfo", "regulatory_text": "RegulatoryText"}
ONE_LINE = {"action", "title"}


# --- text -------------------------------------------------------------------


def unescape(value: Any) -> Any:
    """Restore line breaks DRS stores as a literal backslash-n."""
    return value.replace("\\n", "\n") if isinstance(value, str) else value


def one_line(value: Any) -> str | None:
    """A short field as one line: rows of dashes dropped, line breaks as spaces."""
    text = clean_text(RULE_LINE.sub("", unescape(value)) if isinstance(value, str) else value)
    return " ".join(text.split()) if text else None


def body_text(value: Any) -> str | None:
    """A body section: NPRM stores some as lists of paragraphs; a bare link is not text."""
    if isinstance(value, list):
        value = "\n\n".join(str(v) for v in value if v)
    text = clean_text(unescape(value))
    return None if text is None or text.startswith("http") else text


def federal_register_number(doc: Document, spec: DocType) -> str | None:
    """The FR document number from a link left in place of a body section."""
    for name in BODY.values():
        value = doc.get(f"drs:{spec.prefix}{name}")
        if isinstance(value, str) and (m := FR_LINK.search(value)):
            return m.group(1)
    return None


def unique_docs(records: list[dict[str, Any]]) -> list[Document]:
    """One document per guid, the first seen (a read across a DRS update can repeat one)."""
    docs: dict[str, Document] = {}
    for record in records:
        doc = Document.model_validate(record)
        docs.setdefault(doc.guid, doc)
    return list(docs.values())


# --- Federal Register -------------------------------------------------------


def fetch_federal_register(http: httpx.Client, number: str, cache: Path) -> str | None:
    """Full plain text of one FR document, cached; None if the FR has no text for it.

    HTTP errors for one document are printed and skipped; network errors
    (`httpx.TransportError`) propagate so the caller can stop trying.
    """
    path = cache / f"{number}.txt"
    if path.exists():
        return path.read_text()
    try:
        meta = http.get(FR_API.format(number=number))
        meta.raise_for_status()
        url = meta.json().get("raw_text_url")
        if not url:
            return None
        text = http.get(url)
        text.raise_for_status()
    except httpx.HTTPStatusError as error:
        print(f"  federal register {number}: {error}")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.text)
    time.sleep(0.2)
    return text.text


def fetch_all_federal_register(numbers: list[str], cache: Path) -> None:
    """Cache every FR text not cached yet; stop at the first network failure."""
    missing = [n for n in numbers if not (cache / f"{n}.txt").exists()]
    print(f"federal register: {len(numbers) - len(missing)}/{len(numbers)} cached, fetching {len(missing)}")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True) as http:
        for i, number in enumerate(missing, 1):
            try:
                fetch_federal_register(http, number, cache)
            except httpx.TransportError as error:
                print(f"  federal register unreachable ({type(error).__name__}); {len(missing) - i + 1} left")
                return
            if i % 250 == 0:
                print(f"  federal register {i}/{len(missing)}")


def federal_register_frame(numbers: list[str], cache: Path) -> pl.DataFrame:
    """The cached FR texts among `numbers`, as raw rows."""
    rows = []
    for number in sorted(set(numbers)):
        path = cache / f"{number}.txt"
        if path.exists():
            rows.append(
                {
                    "document_number": number,
                    "raw_text": path.read_text(),
                    "retrieved_at": storage.modified_at(path),
                }
            )
    return pl.DataFrame(rows, schema=FR_SCHEMA)


def add_federal_register(frame: pl.DataFrame, texts: pl.DataFrame) -> pl.DataFrame:
    """`federal_register_text` from the raw FR rows, for rows whose DRS body is only a link."""
    cleaned = {row["document_number"]: clean_text(row["raw_text"]) for row in texts.iter_rows(named=True)}
    return frame.with_columns(
        pl.col("federal_register_number")
        .replace_strict(cleaned, default=None, return_dtype=pl.String)
        .alias("federal_register_text")
    )


# --- processed --------------------------------------------------------------


def to_row(doc: Document, spec: DocType) -> dict[str, Any]:
    row: dict[str, Any] = {"guid": doc.guid}
    row |= {
        column: (one_line if column in ONE_LINE else clean_text)(unescape(doc.get(key)))
        for column, key in spec.text.items()
    }
    row |= {
        column: (list(dict.fromkeys(v)) if (v := clean_list(doc, key)) else None)
        for column, key in spec.lists.items()
    }
    row |= {column: doc.get_date(key) for column, key in spec.dates.items()}
    row |= {column: body_text(doc.get(f"drs:{spec.prefix}{name}")) for column, name in BODY.items()}
    row |= {
        "federal_register_number": federal_register_number(doc, spec),
        "drs_url": doc.url,
        "last_modified": doc.last_modified,
    }
    return row


def build(records: list[dict[str, Any]], spec: DocType) -> pl.DataFrame:
    """One row per guid from raw records, before the Federal Register text is joined."""
    schema: dict[str, Any] = {
        "guid": pl.String,
        **dict.fromkeys(spec.text, pl.String),
        **dict.fromkeys(spec.lists, pl.List(pl.String)),
        **dict.fromkeys(spec.dates, pl.Date),
        **dict.fromkeys(BODY, pl.String),
        "federal_register_number": pl.String,
        "drs_url": pl.String,
        "last_modified": pl.Datetime("us", "UTC"),
    }
    return pl.DataFrame([to_row(d, spec) for d in unique_docs(records)], schema=schema)


def to_frame(records: list[dict[str, Any]], texts: pl.DataFrame, spec: DocType) -> pl.DataFrame:
    return add_federal_register(build(records, spec), texts)


# --- pipeline ---------------------------------------------------------------


def build_raw(data_dir: Path, snapshot: str) -> list[Path]:
    paths, numbers = [], []
    with DRSClient() as client:
        for spec in DOCTYPES:
            cache = drs.cache_dir(data_dir, spec.code, snapshot)
            pages = drs.fetch_pages(client, spec.code, cache)
            frame = raw_frame(pages)
            path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, spec.table)
            storage.write_parquet(
                frame,
                path,
                provenance={
                    "dataset": DATASET,
                    "layer": storage.RAW,
                    "table": spec.table,
                    "source": f"FAA Dynamic Regulatory System, document type {spec.code}",
                    "source_url": f"https://drs.faa.gov/api/drs/data-pull/{spec.code}",
                    "snapshot": snapshot,
                    "retrieved_at": storage.modified_at(*drs.page_files(cache)),
                },
            )
            print(f"{spec.code}: wrote {frame.height} rows to {path}")
            paths.append(path)
            docs = unique_docs([r for page in pages for r in page])
            numbers += [n for d in docs if (n := federal_register_number(d, spec))]

    cache = storage.cache_dir(data_dir, FR_SOURCE)
    numbers = sorted(set(numbers))
    fetch_all_federal_register(numbers, cache)
    texts = federal_register_frame(numbers, cache)
    path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, FEDERAL_REGISTER)
    storage.write_parquet(
        texts,
        path,
        provenance={
            "dataset": DATASET,
            "layer": storage.RAW,
            "table": FEDERAL_REGISTER,
            "source": "Federal Register API, full text of documents DRS links to in place of the body",
            "source_url": "https://www.federalregister.gov/api/v1/documents/{document_number}.json",
            "snapshot": snapshot,
            "linked_documents": str(len(numbers)),
            "cached_documents": str(texts.height),
            "retrieved_at": max(texts["retrieved_at"].to_list(), default="none cached"),
        },
    )
    print(f"federal register: wrote {texts.height} of {len(numbers)} linked documents to {path}")
    return [*paths, path]


def build_processed(data_dir: Path, snapshot: str) -> list[Path]:
    fr_path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, FEDERAL_REGISTER)
    texts = pl.read_parquet(fr_path)
    paths = []
    for spec in DOCTYPES:
        source = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, spec.table)
        frame = to_frame(records(pl.read_parquet(source)), texts, spec)
        path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, spec.table)
        provenance = storage.derived_provenance(
            source,
            data_dir,
            built_from=", ".join(storage.object_key(p, data_dir) for p in (source, fr_path)),
            federal_register_texts=str(texts.height),
        )
        provenance["source"] += "; Federal Register API"
        storage.write_parquet(frame, path, provenance)
        filled = frame["federal_register_text"].is_not_null().sum()
        linked = frame["federal_register_number"].is_not_null().sum()
        print(f"{spec.code}: wrote {frame.height} rows ({filled}/{linked} Federal Register texts) to {path}")
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
            print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = storage.parse_args(parser)
    run(args.data_dir, args.snapshot, args.steps, args.upload)


if __name__ == "__main__":
    main()
