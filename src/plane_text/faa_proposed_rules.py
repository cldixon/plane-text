"""FAA proposed rules from DRS: AD proposed rules (ADNPRM) and general rulemaking proposals (NPRM).

One row per DRS document. DRS carries the Federal Register sections inline,
but for many documents since 2013 it holds only a link to the Federal Register
in place of the supplementary information and regulatory text. For those, the
full document text is fetched from the Federal Register API (public domain)
and stored in `federal_register_text`.

    uv run python -m plane_text.faa_proposed_rules            # write parquet
    uv run python -m plane_text.faa_proposed_rules --upload   # ...and upload to R2

Needs DRS_API_KEY. Federal Register text needs access to www.federalregister.gov;
without it those rows keep only the link.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
from faa_drs import Document, DRSClient

from plane_text import drs, storage
from plane_text.faa_ads import clean_list, clean_text

DATASET = "faa-proposed-rules"
FR_API = "https://www.federalregister.gov/api/v1/documents/{number}.json?fields[]=raw_text_url"
# The first link is the document itself; corrections and extensions may follow.
# Numbers look like 2019-06275, E6-10596 or R1-2022-22698.
FR_LINK = re.compile(
    r"(?:federalregister\.gov/(?:a|d|documents/\d{4}/\d{2}/\d{2})|gpo\.gov/fdsys/pkg/FR-[\d-]+/pdf)/([A-Za-z0-9]+(?:-\d+)+)"
)
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"


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


def body_text(value: Any) -> str | None:
    """A body section: NPRM stores some as lists of paragraphs; a bare link is not text."""
    if isinstance(value, list):
        value = "\n\n".join(str(v) for v in value if v)
    text = clean_text(value)
    return None if text is None or text.startswith("http") else text


def federal_register_number(doc: Document, spec: DocType) -> str | None:
    """The FR document number from a link left in place of a body section."""
    for name in BODY.values():
        value = doc.get(f"drs:{spec.prefix}{name}")
        if isinstance(value, str) and (m := FR_LINK.search(value)):
            return m.group(1)
    return None


def to_row(doc: Document, spec: DocType) -> dict[str, Any]:
    row: dict[str, Any] = {"guid": doc.guid}
    row |= {column: clean_text(doc.get(key)) for column, key in spec.text.items()}
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


def fetch_federal_register(http: httpx.Client, number: str, cache: Path) -> str | None:
    """Full plain text of one FR document, cached; None if it cannot be fetched."""
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
    except httpx.HTTPError as error:
        print(f"  federal register {number}: {error}")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.text)
    time.sleep(0.2)
    return text.text


def add_federal_register(frame: pl.DataFrame, cache: Path) -> pl.DataFrame:
    """Fill `federal_register_text` for rows whose DRS body is only a link."""
    numbers = (
        frame.filter(pl.col("federal_register_number").is_not_null())["federal_register_number"]
        .unique()
        .to_list()
    )
    texts: dict[str, str | None] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True) as http:
        for i, number in enumerate(numbers, 1):
            texts[number] = clean_text(fetch_federal_register(http, number, cache))
            if i % 250 == 0:
                print(f"  federal register {i}/{len(numbers)}")
    return frame.with_columns(
        pl.col("federal_register_number")
        .replace_strict(texts, default=None, return_dtype=pl.String)
        .alias("federal_register_text")
    )


def build(records: list[dict[str, Any]], spec: DocType) -> pl.DataFrame:
    docs = list({d.guid: d for d in (Document.model_validate(r) for r in records)}.values())
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
    return pl.DataFrame([to_row(d, spec) for d in docs], schema=schema)


def run(data_dir: Path, upload: bool) -> list[Path]:
    snapshot = datetime.now(UTC).date().isoformat()
    fr_cache = data_dir / "raw" / "federal-register"
    paths = []
    with DRSClient() as client:
        for spec in DOCTYPES:
            records = drs.fetch_pages(client, spec.code, drs.cache_dir(data_dir, spec.code, snapshot))
            frame = add_federal_register(build(records, spec), fr_cache)
            path = data_dir / DATASET / f"v{snapshot}" / f"{spec.table}.parquet"
            storage.write_parquet(
                frame,
                path,
                provenance={
                    "dataset": DATASET,
                    "table": spec.table,
                    "source": f"FAA DRS, document type {spec.code}; Federal Register API",
                    "source_url": f"https://drs.faa.gov/browse/{spec.code}/doctypeDetails",
                    "snapshot": snapshot,
                    "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            filled = frame["federal_register_text"].is_not_null().sum()
            linked = frame["federal_register_number"].is_not_null().sum()
            print(
                f"{spec.code}: wrote {frame.height} rows ({filled}/{linked} Federal Register texts) to {path}"
            )
            paths.append(path)
            if upload:
                print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--upload", action="store_true", help="upload the parquet to R2")
    args = parser.parse_args()
    run(args.data_dir, args.upload)


if __name__ == "__main__":
    main()
