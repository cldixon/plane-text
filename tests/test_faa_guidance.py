import json
from datetime import date
from pathlib import Path

import polars as pl
from faa_drs import Document

from plane_text import drs, faa_guidance, pdf_text
from plane_text.faa_ads import raw_frame, records

# Real DRS records, with long fields trimmed and contact fields removed.
RECORDS = json.loads((Path(__file__).parent / "fixtures" / "drs-documents.json").read_text())
SAIB = RECORDS["SAIB"][0]["guid"]
AC = RECORDS["AC"][0]["guid"]


def pages(guid: str, extracted: pdf_text.Extracted, file_name: str = "doc.pdf") -> pl.DataFrame:
    return pl.DataFrame(faa_guidance.page_rows(guid, file_name, extracted), schema=faa_guidance.PAGES_SCHEMA)


def frame(code: str, page_rows: pl.DataFrame | None = None) -> pl.DataFrame:
    spec = faa_guidance.DOCTYPES[code]
    raw = raw_frame([RECORDS[code]])
    empty = pl.DataFrame(schema=faa_guidance.PAGES_SCHEMA)
    return faa_guidance.to_frame(records(raw), empty if page_rows is None else page_rows, spec)


def test_page_rows_record_the_method():
    extracted = pdf_text.Extracted(pages=["Layer.", "Scanned.", ""], ocr_pages=[2], ocr_pending_pages=[3])
    rows = pages(SAIB, extracted)
    assert rows["page"].to_list() == [1, 2, 3]
    assert rows["method"].to_list() == ["text_layer", "ocr", "ocr_pending"]
    assert rows["text"].to_list() == ["Layer.", "Scanned.", ""]  # empty pages are kept


def test_unreadable_file_is_one_row_with_the_error():
    rows = pages(SAIB, pdf_text.Extracted(pages=[], ocr_pages=[], error="pdftotext: bad"))
    assert rows.rows() == [(SAIB, "doc.pdf", None, "unreadable", "pdftotext: bad")]


def test_raw_documents_keep_api_names_and_text():
    raw = raw_frame([RECORDS["AC"]])
    assert raw.columns[:3] == ["doctype", "documentGuid", "documentURL"]
    assert raw["hasMoreAttachments"].to_list() == ["true"]
    assert raw.schema["drs:partNumber"] == pl.List(pl.String)
    assert raw["drs:partNumber"].to_list() == [["Part 21"] * 6]  # repeats are removed in processed, not raw


def test_saib_row():
    extracted = pdf_text.Extracted(pages=["Page one.", "Page two."], ocr_pages=[2])
    row = frame("SAIB", pages(SAIB, extracted)).row(0, named=True)
    assert row["document_number"] == "2026-15"
    assert row["title"] == "Forward Pressure Bulkhead"
    assert row["issue_date"] == date(2026, 9, 9)
    assert row["product_subtype"] == ["Small Airplane"]
    assert row["text"] == "Page one.\n\nPage two."
    assert (row["pages"], row["ocr_pages"], row["ocr_pending_pages"]) == (2, [2], [])


def test_pending_and_empty_pages_count_but_add_no_text():
    extracted = pdf_text.Extracted(pages=["One.", "", "x"], ocr_pages=[], ocr_pending_pages=[2, 3])
    row = frame("SAIB", pages(SAIB, extracted)).row(0, named=True)
    assert (row["text"], row["pages"], row["ocr_pending_pages"]) == ("One.\n\nx", 3, [2, 3])


def test_ac_row_dedupes_repeated_list_values():
    row = frame("AC", pages(AC, pdf_text.Extracted(pages=["Text."], ocr_pages=[]))).row(0, named=True)
    assert row["document_number"] == "AC 21-46A"
    assert row["cancels"] == ["AC 21-46"]
    assert row["cfr_parts"] == ["Part 21"]  # DRS repeats the part once per section
    assert row["approval_date"] == date(2017, 2, 23)


def test_row_without_a_file_has_no_text():
    row = frame("SAIB").row(0, named=True)
    assert (row["text"], row["pages"], row["ocr_pages"]) == (None, None, None)


def test_unreadable_file_has_zero_pages():
    rows = pages(SAIB, pdf_text.Extracted(pages=[], ocr_pages=[], error="pdftotext: bad"))
    row = frame("SAIB", rows).row(0, named=True)
    assert (row["text"], row["pages"], row["ocr_pages"]) == (None, 0, [])


def test_one_row_per_guid_and_schema():
    spec = faa_guidance.DOCTYPES["SAIB"]
    raw = raw_frame([RECORDS["SAIB"], RECORDS["SAIB"]])  # a document read twice
    out = faa_guidance.to_frame(records(raw), pl.DataFrame(schema=faa_guidance.PAGES_SCHEMA), spec)
    assert out.height == 1
    assert out.schema == pl.Schema(faa_guidance.schema(spec))


def test_pdfs_by_guid_finds_cached_files(tmp_path: Path):
    record = RECORDS["SAIB"][0]
    path = drs.file_path(tmp_path, Document.model_validate(record))
    assert path is not None
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF")
    assert faa_guidance.pdfs_by_guid([record, record], tmp_path) == {SAIB: path}
    assert drs.cache_dir(tmp_path, "SAIB", "2026-10-24") == tmp_path / "cache" / "drs" / "SAIB" / "2026-10-24"
