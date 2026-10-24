import json
from pathlib import Path

from plane_text import pdf_text


def test_clean_collapses_spaces_and_blank_lines():
    assert pdf_text.clean("  Title   here  \n\n\n\nBody\t text ") == "Title here\n\nBody text"


def test_needs_ocr_on_nearly_empty_pages():
    assert pdf_text.needs_ocr("")
    assert pdf_text.needs_ocr("  12  \n  - 3 -  ")
    assert not pdf_text.needs_ocr("The pilot reported a loss of engine power during cruise flight.")


def test_extract_ocrs_only_pages_without_text(monkeypatch):
    layer = ["Advisory Circular 43.13-1B, acceptable methods and practices.", "", "  7  "]
    monkeypatch.setattr(pdf_text, "text_layer", lambda pdf: list(layer))
    calls = []

    def fake_ocr(pdf, page):
        calls.append(page)
        return f"scanned page {page} text"

    monkeypatch.setattr(pdf_text, "ocr_page", fake_ocr)
    result = pdf_text.extract(Path("doc.pdf"))
    assert calls == [2, 3]
    assert result.ocr_pages == [2, 3]
    assert result.pages[0] == layer[0]
    assert result.text == f"{layer[0]}\n\nscanned page 2 text\n\nscanned page 3 text"


def test_extract_cached_reads_cache(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    pdf_text.cache_path(pdf).write_text(json.dumps({"pages": ["Cached."], "ocr_pages": [], "error": None}))
    monkeypatch.setattr(
        pdf_text, "extract", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not cached"))
    )
    assert pdf_text.extract_cached(pdf).text == "Cached."
