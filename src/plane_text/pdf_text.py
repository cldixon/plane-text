"""Text from PDFs: the embedded text layer where there is one, OCR where there is not.

Each page is read with `pdftotext`. A page with almost no text is treated as a
scan: it is rendered with `pdftoppm` and read with Tesseract. Results are
cached as JSON beside the source file, so OCR runs once per file. Needs
poppler-utils and tesseract-ocr (`apt-get install poppler-utils tesseract-ocr`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

# A page with fewer letters and digits than this is treated as a scan.
MIN_PAGE_CHARS = 25
OCR_DPI = 300
OCR_TIMEOUT = 180  # seconds per page; a page that times out keeps its text layer
# Tesseract is multi-threaded by default; files are already processed in
# parallel, so one thread each avoids oversubscribing the CPUs.
OCR_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}
WORD = re.compile(r"[A-Za-z0-9]")
SPACES = re.compile(r"[ \t]+")
BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Extracted:
    pages: list[str]
    ocr_pages: list[int]  # 1-based page numbers read with OCR
    error: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(p for p in self.pages if p)


def clean(text: str) -> str:
    lines = (SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def needs_ocr(page: str) -> bool:
    return len(WORD.findall(page)) < MIN_PAGE_CHARS


def text_layer(pdf: Path) -> list[str]:
    """Each page's embedded text. pdftotext separates pages with form feeds."""
    out = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        check=True,
        capture_output=True,
        timeout=600,
    ).stdout.decode("utf-8", errors="replace")
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]  # trailing form feed
    return pages


def ocr_page(pdf: Path, page: int) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "page"
        subprocess.run(
            [
                "pdftoppm",
                "-r",
                str(OCR_DPI),
                "-gray",
                "-f",
                str(page),
                "-l",
                str(page),
                "-png",
                str(pdf),
                str(stem),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        image = next(Path(tmp).glob("page*.png"))
        return subprocess.run(
            ["tesseract", str(image), "-", "-l", "eng", "--psm", "1"],
            check=True,
            capture_output=True,
            timeout=OCR_TIMEOUT,
            env=OCR_ENV,
        ).stdout.decode("utf-8", errors="replace")


def extract(pdf: Path, ocr: bool = True) -> Extracted:
    try:
        pages = text_layer(pdf)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return Extracted(pages=[], ocr_pages=[], error=f"pdftotext: {error}")
    ocr_pages = []
    for number, page in enumerate(pages, 1):
        if ocr and needs_ocr(page):
            try:
                pages[number - 1] = ocr_page(pdf, number)
                ocr_pages.append(number)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, StopIteration):
                pass  # keep whatever the text layer had
    return Extracted(pages=[clean(p) for p in pages], ocr_pages=ocr_pages)


def cache_path(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".text.json")


def extract_cached(pdf: Path) -> Extracted:
    """Extract once; later calls read the cached result."""
    path = cache_path(pdf)
    if path.exists():
        return Extracted(**json.loads(path.read_text()))
    result = extract(pdf)
    path.write_text(json.dumps(asdict(result)))
    return result


def extract_all(pdfs: list[Path], workers: int = 4) -> dict[Path, Extracted]:
    """Extract many files in parallel, printing progress every 50 files."""
    results: dict[Path, Extracted] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, (pdf, result) in enumerate(
            zip(pdfs, pool.map(extract_cached, pdfs, chunksize=4), strict=True), 1
        ):
            results[pdf] = result
            if i % 50 == 0 or i == len(pdfs):
                print(f"  extracted {i}/{len(pdfs)}")
    return results


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract and cache text for every PDF under the given directories."
    )
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    for directory in args.dirs:
        pdfs = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        print(f"{directory}: {len(pdfs)} PDFs")
        results = extract_all(pdfs, args.workers)
        ocr = sum(len(r.ocr_pages) for r in results.values())
        errors = sum(1 for r in results.values() if r.error)
        print(f"{directory}: {ocr} pages read with OCR, {errors} unreadable files")


if __name__ == "__main__":
    main()
