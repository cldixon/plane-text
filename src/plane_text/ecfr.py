"""Title 14 of the Code of Federal Regulations, from the eCFR versioner API.

One row per section or appendix. Parts are fetched one at a time, and the
raw XML is cached so a re-run only re-parses.

    uv run python -m plane_text.ecfr            # latest issue, write parquet
    uv run python -m plane_text.ecfr --upload   # ...and upload to R2
"""

from __future__ import annotations

import argparse
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from plane_text import storage

DATASET = "ecfr-title14"
TITLE = 14
API = "https://www.ecfr.gov/api/versioner/v1"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"

# Child elements of a section that are not body text.
NON_BODY = {"HEAD", "CITA", "SECAUTH", "EDNOTE"}
SECTION_PREFIX = re.compile(r"^§+\s*[\w.\-]+\s*")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class PartRef:
    """A part of the title, with the chapter and subchapter above it."""

    part: str
    chapter: str | None
    subchapter: str | None


@dataclass(frozen=True)
class Section:
    identifier: str
    kind: str
    title: int
    chapter: str | None
    subchapter: str | None
    part: str
    part_heading: str
    subpart: str | None
    subpart_heading: str | None
    heading: str
    text: str
    citation: str | None
    issue_date: str
    source_url: str


SCHEMA = {
    "identifier": pl.String,
    "kind": pl.String,
    "title": pl.Int16,
    "chapter": pl.String,
    "subchapter": pl.String,
    "part": pl.String,
    "part_heading": pl.String,
    "subpart": pl.String,
    "subpart_heading": pl.String,
    "heading": pl.String,
    "text": pl.String,
    "citation": pl.String,
    "issue_date": pl.String,
    "source_url": pl.String,
}


# --- fetching ---------------------------------------------------------------


def client() -> httpx.Client:
    # No custom transport: it would stop httpx from honoring proxy settings in the environment.
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True)


def get(http: httpx.Client, url: str, attempts: int = 4) -> httpx.Response:
    """GET with retries on transport errors and 429/5xx, backing off 2s, 4s, 8s."""
    for attempt in range(attempts):
        try:
            response = http.get(url)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        if attempt < attempts - 1:
            time.sleep(2 ** (attempt + 1))
    response.raise_for_status()
    return response


def latest_issue_date(http: httpx.Client) -> str:
    response = get(http, f"{API}/titles.json")
    for entry in response.json()["titles"]:
        if entry["number"] == TITLE:
            return entry["latest_issue_date"]
    raise LookupError(f"title {TITLE} not listed by eCFR")


def fetch_structure(http: httpx.Client, date: str) -> dict[str, Any]:
    response = get(http, f"{API}/structure/{date}/title-{TITLE}.json")
    return response.json()


def part_url(date: str, part: str) -> str:
    return f"{API}/full/{date}/title-{TITLE}.xml?part={part}"


def fetch_part(http: httpx.Client, date: str, part: str, cache: Path) -> bytes:
    path = cache / f"part-{part}.xml"
    if path.exists():
        return path.read_bytes()
    response = get(http, part_url(date, part))
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


# --- parsing ----------------------------------------------------------------


def list_parts(structure: dict[str, Any]) -> list[PartRef]:
    """Flatten the structure tree into its non-reserved parts, in order."""
    parts: list[PartRef] = []

    def walk(node: dict[str, Any], chapter: str | None, subchapter: str | None) -> None:
        kind = node.get("type")
        if kind == "chapter":
            chapter = node.get("identifier")
        elif kind == "subchapter":
            subchapter = node.get("identifier")
        elif kind == "part":
            if not node.get("reserved"):
                parts.append(PartRef(node["identifier"], chapter, subchapter))
            return
        for child in node.get("children") or []:
            walk(child, chapter, subchapter)

    walk(structure, None, None)
    return parts


def clean(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def text_of(element: ET.Element) -> str:
    return clean("".join(element.itertext()))


def heading_of(element: ET.Element) -> str:
    head = element.find("HEAD")
    return text_of(head) if head is not None else ""


def table_of(table: ET.Element) -> str:
    """A table as its caption, then one line per row with cells separated by ` | `."""
    lines = [text_of(caption) for caption in table.findall("CAPTION")]
    for row in table.iter("TR"):
        cells = [text_of(cell) for cell in row if cell.tag in ("TD", "TH")]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(line for line in lines if line)


def block_of(element: ET.Element) -> str:
    if element.tag == "TABLE":
        return table_of(element)
    if element.find(".//TABLE") is not None:  # tables arrive wrapped in DIVs
        return "\n\n".join(b for b in (block_of(child) for child in element) if b)
    return text_of(element)


def body_of(element: ET.Element) -> str:
    blocks = (block_of(child) for child in element if child.tag not in NON_BODY)
    return "\n\n".join(block for block in blocks if block)


def citation_of(element: ET.Element) -> str | None:
    citations = [text_of(c) for c in element.findall("CITA")]
    return " ".join(c for c in citations if c) or None


def parse_part(xml: bytes, ref: PartRef, date: str) -> list[Section]:
    """Parse one part's XML into its sections and appendices.

    Elements are matched by their TYPE attribute, not their DIV level, so the
    parser does not depend on how deep the part sits in the document.
    """
    root = ET.fromstring(xml)
    part = root if root.get("TYPE") == "PART" else root.find(".//*[@TYPE='PART']")
    if part is None:
        raise ValueError(f"no PART element in XML for part {ref.part}")

    sections: list[Section] = []

    def walk(node: ET.Element, subpart: str | None, subpart_heading: str | None) -> None:
        for child in node:
            kind = child.get("TYPE")
            if kind == "SUBPART":
                walk(child, child.get("N"), heading_of(child))
            elif kind in ("SECTION", "APPENDIX"):
                text = body_of(child)
                if not text:
                    continue  # reserved or empty
                heading = heading_of(child)
                if kind == "SECTION":
                    heading = SECTION_PREFIX.sub("", heading)
                sections.append(
                    Section(
                        identifier=child.get("N") or "",
                        kind=kind.lower(),
                        title=TITLE,
                        chapter=ref.chapter,
                        subchapter=ref.subchapter,
                        part=ref.part,
                        part_heading=heading_of(part),
                        subpart=subpart,
                        subpart_heading=subpart_heading,
                        heading=heading,
                        text=text,
                        citation=citation_of(child),
                        issue_date=date,
                        source_url=part_url(date, ref.part),
                    )
                )
            elif child.tag.startswith("DIV"):
                walk(child, subpart, subpart_heading)  # subject groups and the like

    walk(part, None, None)
    return sections


def to_frame(sections: list[Section]) -> pl.DataFrame:
    return pl.DataFrame([asdict(s) for s in sections], schema=SCHEMA)


# --- pipeline ---------------------------------------------------------------


def run(date: str | None, data_dir: Path, upload: bool, limit: int | None) -> Path:
    with client() as http:
        date = date or latest_issue_date(http)
        parts = list_parts(fetch_structure(http, date))[:limit]
        cache = data_dir / "raw" / DATASET / date
        sections: list[Section] = []
        for i, ref in enumerate(parts, 1):
            xml = fetch_part(http, date, ref.part, cache)
            sections.extend(parse_part(xml, ref, date))
            print(f"[{i}/{len(parts)}] part {ref.part}: {len(sections)} rows so far")
            time.sleep(0.25)

    frame = to_frame(sections)
    path = data_dir / DATASET / f"v{date}" / "sections.parquet"
    storage.write_parquet(
        frame,
        path,
        provenance={
            "dataset": DATASET,
            "source": "eCFR versioner API",
            "source_url": f"{API}/full/{date}/title-{TITLE}.xml",
            "issue_date": date,
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
    parser.add_argument("--date", help="eCFR issue date (YYYY-MM-DD); default latest")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--upload", action="store_true", help="upload the parquet to R2")
    parser.add_argument("--limit", type=int, help="only the first N parts (for testing)")
    args = parser.parse_args()
    run(args.date, args.data_dir, args.upload, args.limit)


if __name__ == "__main__":
    main()
