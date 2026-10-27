from datetime import date

import polars as pl

from plane_text import ntsb, ntsb_lookups


def tables(ev_id: str, ev_date: str, factual: str | None, summary: str | None) -> dict[str, pl.DataFrame]:
    """Minimal raw tables, shaped like `mdb-export` output (owner columns already withheld): all text."""
    frames = {
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
                "acft_category": ["air ", "AIR"],
                "homebuilt": ["N", "N"],
                "damage": ["SUBS", "NONE"],
                "far_part": ["091", "091"],
                "oper_individual_name": ["Y", "N"],
            }
        ),
    }
    return {name: frame.cast(pl.String) for name, frame in frames.items()}


def code(table: str, column: str, value: str, meaning: str, code_set: str | None = None) -> dict:
    entry = {"table": table, "column": column, "code": value, "match": "exact", "code_part": value}
    return entry | {"meaning": meaning, "code_set": code_set}


LOOKUPS = ntsb_lookups.Lookups(
    codes=pl.DataFrame(
        [
            code("events", "ev_type", "ACC", "Accident"),
            code("events", "ev_highest_injury", "MINR", "Minor"),
            code("events", "ev_state", "VA", "VIRGINIA", "ct_states"),
            code("events", "ev_state", "BC", "BOTSWANA", "ct_states"),
            code("aircraft", "acft_category", "AIR", "Airplane", "ct_acft_cat"),
            code("aircraft", "homebuilt", "N", "No"),
            code("aircraft", "damage", "SUBS", "Substantial", "ct_damage"),
            code("aircraft", "far_part", "091", "Part 91: General Aviation", "ct_fars"),
        ]
    ),
    code_sets=pl.DataFrame({"code_set": ["ct_acft_cat"], "code": ["WSFT"], "meaning": ["Weight-Shift"]}),
    countries=pl.DataFrame(
        {
            "database": ["avall", "avall", "Pre2008", "Pre2008"],
            "code": ["USA", "AU", "USA", "AU"],
            "name": ["United States", "Australia", "United States", "Austria"],
        }
    ),
)


def processed(*raw: tuple[str, dict[str, pl.DataFrame]]) -> pl.DataFrame:
    return ntsb.process(dict(raw), LOOKUPS)[0]


def test_build_joins_event_and_aircraft_and_drops_rows_without_text():
    frame = processed(
        ("avall", tables("20080107X00026", "2008-01-05T00:00:00", "On January 5, 2008...", "Summary."))
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
    assert "oper_individual_name" not in frame.columns
    assert "narr_inc" not in frame.columns


def test_labels_sit_next_to_their_codes():
    frame = processed(("avall", tables("20080107X00026", "2008-01-05T00:00:00", "Text.", None)))
    columns = frame.columns
    for label in (*ntsb.LABELS, ntsb.STATE):
        assert columns.index(label.label) == columns.index(label.column) + 1
    assert columns.index("country") == columns.index("country_code") + 1
    row = frame.row(0, named=True)
    assert (row["event_type_code"], row["event_type"]) == ("ACC", "Accident")
    assert (row["highest_injury_code"], row["highest_injury"]) == ("MINR", "Minor")
    assert (row["aircraft_category_code"], row["aircraft_category"]) == (
        "AIR",
        "Airplane",
    )  # trimmed, uppercased
    assert (row["homebuilt_code"], row["homebuilt"]) == ("N", "No")
    assert (row["damage_code"], row["damage"]) == ("SUBS", "Substantial")
    assert (row["far_part_code"], row["far_part"]) == ("091", "Part 91: General Aviation")
    assert (row["state_code"], row["state"]) == ("VA", "VIRGINIA")
    assert (row["country_code"], row["country"]) == ("USA", "United States")


def test_states_only_for_us_events_and_countries_per_database():
    def foreign(database: str, state: str, category: str) -> pl.DataFrame:
        raw = tables("20080107X00026", "2008-01-05T00:00:00", "Text.", None)
        raw["events"] = raw["events"].with_columns(
            pl.lit(state).alias("ev_state"), pl.lit("AU").alias("ev_country")
        )
        raw["aircraft"] = raw["aircraft"].with_columns(pl.lit(category).alias("acft_category"))
        return processed((database, raw))

    newer = foreign("avall", "BC", "WSFT").row(0, named=True)
    assert (newer["state_code"], newer["state"]) == ("BC", None)  # not a state outside the US
    assert newer["country"] == "Australia"
    assert newer["aircraft_category"] == "Weight-Shift"  # from the column's code set
    older = foreign("Pre2008", "OF", "XYZ").row(0, named=True)
    assert older["country"] == "Austria"  # the same code, the older database's meaning
    assert (older["aircraft_category_code"], older["aircraft_category"]) == ("XYZ", None)


def test_clean_text():
    frame = pl.DataFrame({"t": ["  Line one.  \r\nLine two.\r\n\r\n\r\n\r\nLine three. ", "   "]})
    assert frame.select(ntsb.clean_text("t"))["t"].to_list() == ["Line one.\nLine two.\n\nLine three.", None]


def test_newer_database_wins():
    combined = processed(
        ("avall", tables("20221006106073", "2022-09-17T00:00:00", "Newer.", "Newer.")),
        ("Pre2008", tables("20221006106073", "2000-09-17T00:00:00", "Stale.", "Stale.")),
    )
    assert combined.height == 1
    assert combined.row(0, named=True)["narrative_factual"] == "Newer."
    assert combined.row(0, named=True)["event_date"] == date(2022, 9, 17)


def test_unparseable_values_become_null_and_are_counted():
    raw = tables("20080107X00026", "2008-13-45T00:00:00", "Text.", None)
    raw["events"] = raw["events"].with_columns(pl.lit("two").alias("inj_tot_f"))
    frame, unparsed = ntsb.process({"avall": raw}, LOOKUPS)
    assert frame.row(0, named=True)["event_date"] is None
    assert frame.row(0, named=True)["fatal_injuries"] is None
    assert unparsed["event_date"] == 1 and unparsed["fatal_injuries"] == 1
    assert unparsed["serious_injuries"] == 0  # empty to begin with is not a loss


def test_label_coverage_counts_states_over_us_events_only():
    frame = pl.concat(
        [
            processed(("avall", tables("20080107X00026", "2008-01-05T00:00:00", "Text.", None))),
            processed(
                ("Pre2008", tables("20000107X00001", "2000-01-05T00:00:00", "Text.", None))
            ).with_columns(
                pl.lit("AU").alias("country_code"),
                pl.lit("OF").alias("state_code"),
                pl.lit(None).alias("state"),
            ),
        ]
    )
    coverage = {row["label"]: row for row in ntsb.label_coverage(frame).iter_rows(named=True)}
    assert (coverage["state"]["coded"], coverage["state"]["labeled"]) == (1, 1)
    assert coverage["event_type"]["coded"] == 2
