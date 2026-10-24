import json
from datetime import date
from pathlib import Path

import polars as pl
from faa_drs import Document

from plane_text import faa_guidance, pdf_text

# Real DRS records, with long fields trimmed and contact fields removed.
RECORDS = json.loads((Path(__file__).parent / "fixtures" / "drs-documents.json").read_text())


def frame(code: str, extracted: pdf_text.Extracted | None) -> pl.DataFrame:
    spec = faa_guidance.DOCTYPES[code]
    rows = [faa_guidance.to_row(Document.model_validate(r), spec, extracted) for r in RECORDS[code]]
    return pl.DataFrame(rows, schema=faa_guidance.schema(spec))


def test_saib_row():
    row = frame("SAIB", pdf_text.Extracted(pages=["Page one.", "Page two."], ocr_pages=[2])).row(
        0, named=True
    )
    assert row["document_number"] == "2026-15"
    assert row["title"] == "Forward Pressure Bulkhead"
    assert row["issue_date"] == date(2026, 9, 9)
    assert row["product_subtype"] == ["Small Airplane"]
    assert row["text"] == "Page one.\n\nPage two."
    assert (row["pages"], row["ocr_pages"]) == (2, [2])


def test_ac_row_dedupes_repeated_list_values():
    row = frame("AC", pdf_text.Extracted(pages=["Text."], ocr_pages=[])).row(0, named=True)
    assert row["document_number"] == "AC 21-46A"
    assert row["cancels"] == ["AC 21-46"]
    assert row["cfr_parts"] == ["Part 21"]  # DRS repeats the part once per section
    assert row["approval_date"] == date(2017, 2, 23)


def test_row_without_a_file_has_no_text():
    row = frame("SAIB", None).row(0, named=True)
    assert (row["text"], row["pages"], row["ocr_pages"]) == (None, None, None)
