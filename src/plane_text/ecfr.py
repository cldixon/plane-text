"""Title 14 of the Code of Federal Regulations, from the eCFR versioner API.

The snapshot is the eCFR issue date: the title as it stood on that date. The
title's structure (JSON) and each non-reserved part's full XML are fetched
once and cached under `cache/ecfr-title14/<issue date>/`, so a re-run only
re-parses.

- raw `structure`: the structure JSON as delivered, one row per node of its
  tree in document order (`node`, `parent`, `depth`), with the API's own keys
  and values as text; `volumes` stays a list of text.
- raw `parts`: one row per part fetched, with the chapter and subchapter the
  structure places it under, its source URL, and its XML exactly as delivered.
- processed `sections`: built from raw `parts`. One row per section or
  appendix, with headings, body text (tables rendered row by row) and citation.

    uv run python -m plane_text.ecfr --snapshot 2026-10-15                 # raw, then processed
    uv run python -m plane_text.ecfr --snapshot 2026-10-15 --step processed
    uv run python -m plane_text.ecfr --snapshot 2026-10-15 --upload        # ...and upload to R2
    uv run python -m plane_text.ecfr --limit 5                             # first five parts only

A snapshot later than eCFR's latest issue of title 14 (the default, today,
usually is) builds that latest issue instead.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from plane_text import storage

DATASET = "ecfr-title14"
TITLE = 14
API = "https://www.ecfr.gov/api/versioner/v1"
USER_AGENT = "plane-text (+https://github.com/cldixon/plane-text)"
STRUCTURE = "structure"
PARTS = "parts"
SECTIONS = "sections"

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


def structure_url(date: str) -> str:
    return f"{API}/structure/{date}/title-{TITLE}.json"


def part_url(date: str, part: str) -> str:
    return f"{API}/full/{date}/title-{TITLE}.xml?part={part}"


def cached_get(http: httpx.Client, url: str, path: Path) -> bytes:
    """The body at `url`: from `path` if it was fetched before, else fetched and saved there."""
    if path.exists():
        return path.read_bytes()
    content = get(http, url).content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def fetch_structure(http: httpx.Client, date: str, cache: Path) -> bytes:
    return cached_get(http, structure_url(date), cache / f"{STRUCTURE}.json")


def fetch_part(http: httpx.Client, date: str, part: str, cache: Path) -> bytes:
    return cached_get(http, part_url(date, part), cache / f"part-{part}.xml")


def resolve_snapshot(http: httpx.Client, snapshot: str, cache_root: Path) -> str:
    """`snapshot`, or eCFR's latest issue date of the title if `snapshot` is later.

    A snapshot whose structure is already cached is used as is, with no request.
    """
    if (cache_root / snapshot / f"{STRUCTURE}.json").exists():
        return snapshot
    latest = latest_issue_date(http)
    if snapshot > latest:
        print(f"eCFR's latest issue of title {TITLE} is {latest}; building that")
        return latest
    return snapshot


# --- raw --------------------------------------------------------------------

NODE_COLUMNS = {"node": pl.Int32, "parent": pl.Int32, "depth": pl.Int16}

PARTS_SCHEMA = {
    "issue_date": pl.String,
    "chapter": pl.String,
    "subchapter": pl.String,
    "part": pl.String,
    "source_url": pl.String,
    "xml": pl.String,
}


def as_text(value: Any) -> Any:
    """A JSON value as text: booleans as `true`/`false`, lists item by item, null kept."""
    if isinstance(value, list):
        return [as_text(item) for item in value]
    if isinstance(value, bool):
        return json.dumps(value)
    return None if value is None else str(value)


def structure_frame(structure: dict[str, Any]) -> pl.DataFrame:
    """The structure tree as one row per node, in document (pre-)order.

    `node` is the row's position, `parent` its parent's `node` (null for the
    title) and `depth` 0 for the title; the other columns are the node's own
    keys with values as text. A key is a list column if any node has a list there.
    """
    rows: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], parent: int | None, depth: int) -> None:
        index = len(rows)
        fields = {k: as_text(v) for k, v in node.items() if k != "children"}
        rows.append({"node": index, "parent": parent, "depth": depth} | fields)
        for child in node.get("children") or []:
            walk(child, index, depth + 1)

    walk(structure, None, 0)
    keys = list(dict.fromkeys(k for row in rows for k in row if k not in NODE_COLUMNS))
    lists = {k for row in rows for k, v in row.items() if isinstance(v, list)}
    for row in rows:
        if mixed := [k for k in lists if row.get(k) is not None and not isinstance(row[k], list)]:
            raise ValueError(f"node {row['node']}: text where other nodes have lists: {mixed}")
    schema = {k: pl.List(pl.String) if k in lists else pl.String for k in keys}
    return pl.DataFrame(rows, schema=NODE_COLUMNS | schema)


def structure_tree(raw: pl.DataFrame) -> dict[str, Any]:
    """Raw structure rows back into a tree, values as text except `reserved`, a boolean again.

    Keys a node did not have are left out; children keep their document order.
    """
    nodes: list[dict[str, Any]] = []
    for row in raw.sort("node").iter_rows(named=True):
        node = {k: v for k, v in row.items() if k not in NODE_COLUMNS and v is not None}
        if "reserved" in node:
            node["reserved"] = json.loads(node["reserved"])
        nodes.append(node)
        if row["parent"] is not None:
            nodes[row["parent"]].setdefault("children", []).append(node)
    return nodes[0]


def part_row(ref: PartRef, date: str, xml: bytes) -> dict[str, str | None]:
    """A part as delivered: where the structure puts it, where it came from, and its XML."""
    return {
        "issue_date": date,
        "chapter": ref.chapter,
        "subchapter": ref.subchapter,
        "part": ref.part,
        "source_url": part_url(date, ref.part),
        "xml": xml.decode("utf-8"),  # strict, so the text encodes back to exactly the bytes delivered
    }


def parts_frame(rows: list[dict[str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=PARTS_SCHEMA)


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


def parse_part(xml: bytes, ref: PartRef, date: str, source_url: str | None = None) -> list[Section]:
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
                        source_url=source_url or part_url(date, ref.part),
                    )
                )
            elif child.tag.startswith("DIV"):
                walk(child, subpart, subpart_heading)  # subject groups and the like

    walk(part, None, None)
    return sections


def to_frame(sections: list[Section]) -> pl.DataFrame:
    return pl.DataFrame([asdict(s) for s in sections], schema=SCHEMA)


# --- processed --------------------------------------------------------------


def sections_from_raw(parts: pl.DataFrame) -> pl.DataFrame:
    """The processed sections table from raw `parts` rows, in their order."""
    sections: list[Section] = []
    for row in parts.iter_rows(named=True):
        ref = PartRef(row["part"], row["chapter"], row["subchapter"])
        sections += parse_part(row["xml"].encode("utf-8"), ref, row["issue_date"], row["source_url"])
    return to_frame(sections)


# --- pipeline ---------------------------------------------------------------


def build_raw(data_dir: Path, snapshot: str, limit: int | None = None) -> tuple[str, list[Path]]:
    """Write raw `structure` and `parts`; returns the snapshot built (see `resolve_snapshot`) and paths.

    Reads the cache where it can, so a fully cached snapshot makes no requests.
    """
    cache_root = storage.cache_dir(data_dir, DATASET)
    with client() as http:
        snapshot = resolve_snapshot(http, snapshot, cache_root)
        cache = cache_root / snapshot
        structure = json.loads(fetch_structure(http, snapshot, cache))
        refs = list_parts(structure)[:limit]
        rows = []
        for i, ref in enumerate(refs, 1):
            cached = (cache / f"part-{ref.part}.xml").exists()
            rows.append(part_row(ref, snapshot, fetch_part(http, snapshot, ref.part, cache)))
            print(f"[{i}/{len(refs)}] part {ref.part}{' (cached)' if cached else ''}")
            if not cached:
                time.sleep(0.25)

    provenance = {
        "dataset": DATASET,
        "layer": storage.RAW,
        "source": "eCFR versioner API",
        "snapshot": snapshot,
        "issue_date": snapshot,
    }
    tables = {
        STRUCTURE: (structure_frame(structure), structure_url(snapshot), [cache / f"{STRUCTURE}.json"]),
        PARTS: (
            parts_frame(rows),
            f"{API}/full/{snapshot}/title-{TITLE}.xml",
            [cache / f"part-{ref.part}.xml" for ref in refs],
        ),
    }
    paths = []
    for name, (frame, url, files) in tables.items():
        path = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, name)
        source = {"source_url": url, "retrieved_at": storage.modified_at(*files)}
        storage.write_parquet(frame, path, provenance | source)
        print(f"wrote {frame.height} rows to {path}")
        paths.append(path)
    return snapshot, paths


def build_processed(data_dir: Path, snapshot: str) -> Path:
    source = storage.layer_path(data_dir, storage.RAW, DATASET, snapshot, PARTS)
    frame = sections_from_raw(pl.read_parquet(source))
    path = storage.layer_path(data_dir, storage.PROCESSED, DATASET, snapshot, SECTIONS)
    storage.write_parquet(frame, path, storage.derived_provenance(source, data_dir))
    print(f"wrote {frame.height} rows to {path}")
    return path


def run(
    data_dir: Path, snapshot: str, steps: list[str], upload: bool, limit: int | None = None
) -> list[Path]:
    paths = []
    if storage.RAW in steps:
        snapshot, raw = build_raw(data_dir, snapshot, limit)
        paths += raw
    if storage.PROCESSED in steps:
        paths.append(build_processed(data_dir, snapshot))
    if upload:
        for path in paths:
            print(f"uploaded to r2://{storage.bucket()}/{storage.upload(path, data_dir)}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="raw: only the first N parts (for testing)")
    args = storage.parse_args(parser)
    run(args.data_dir, args.snapshot, args.steps, args.upload, args.limit)


if __name__ == "__main__":
    main()
