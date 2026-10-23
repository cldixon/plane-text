from datetime import date

import polars as pl

from plane_text import ntsb


def tables(ev_id: str, ev_date: str, factual: str | None, summary: str | None) -> dict[str, pl.DataFrame]:
    """Minimal text tables shaped like `mdb-export` output."""
    return {
        "narratives": pl.DataFrame(
            {
                "ev_id": [ev_id, ev_id],
                "Aircraft_Key": ["1", "2"],
                "narr_accp": [factual, None],
                "narr_accf": [summary, None],
                "narr_cause": ["The pilot's failure to maintain airspeed.", None],
                "narr_inc": [None, None],
                "lchg_date": ["2024-01-01T00:00:00", "2024-01-01T00:00:00"],
                "lchg_userid": ["someone", "someone"],
            }
        ),
        "events": pl.DataFrame(
            {
                "ev_id": [ev_id],
                "ntsb_no": ["ERA24LA001"],
                "ev_type": ["ACC"],
                "ev_date": [ev_date],
                "ev_city": ["Spotsylvania"],
                "ev_state": ["VA"],
                "ev_country": ["USA"],
                "ev_highest_injury": ["MINR"],
                "inj_tot_f": ["0"],
                "inj_tot_s": [""],
                "inj_tot_m": ["1"],
                "inj_tot_n": ["0"],
            }
        ),
        "aircraft": pl.DataFrame(
            {
                "ev_id": [ev_id, ev_id],
                "Aircraft_Key": ["1", "2"],
                "regis_no": ["N758FQ", "N1"],
                "acft_make": ["CESSNA", "PIPER"],
                "acft_model": ["R172K", "PA-28"],
                "acft_series": ["", ""],
                "acft_category": ["AIR", "AIR"],
                "homebuilt": ["N", "N"],
                "damage": ["SUBS", "NONE"],
                "far_part": ["091", "091"],
                "owner_street": ["1 Main St", "2 Main St"],
            }
        ),
    }


def test_build_joins_event_and_aircraft_and_drops_rows_without_text():
    frame = ntsb.build(
        tables("20080107X00026", "2008-01-05T00:00:00", "On January 5, 2008...", "Summary."), "avall"
    )
    assert frame.height == 1  # aircraft 2 has no narrative text
    row = frame.row(0, named=True)
    assert row["aircraft_key"] == 1
    assert row["event_date"] == date(2008, 1, 5)
    assert row["aircraft_make"] == "CESSNA"
    assert row["registration"] == "N758FQ"
    assert row["fatal_injuries"] == 0
    assert row["serious_injuries"] is None  # empty export value
    assert row["aircraft_series"] is None
    assert row["probable_cause"] == "The pilot's failure to maintain airspeed."
    assert row["source_database"] == "avall"
    assert "owner_street" not in frame.columns
    assert "narr_inc" not in frame.columns


def test_clean_text():
    frame = pl.DataFrame({"t": ["  Line one.  \r\nLine two.\r\n\r\n\r\n\r\nLine three. ", "   "]})
    assert frame.select(ntsb.clean_text("t"))["t"].to_list() == ["Line one.\nLine two.\n\nLine three.", None]


def test_combine_prefers_the_newer_database():
    newer = ntsb.build(tables("20221006106073", "2022-09-17T00:00:00", "Newer.", "Newer."), "avall")
    older = ntsb.build(tables("20221006106073", "2000-09-17T00:00:00", "Stale.", "Stale."), "Pre2008")
    combined = ntsb.combine([newer, older])
    assert combined.height == 1
    assert combined.row(0, named=True)["narrative_factual"] == "Newer."
    assert combined.row(0, named=True)["event_date"] == date(2022, 9, 17)
