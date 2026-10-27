from pathlib import Path

import polars as pl
import pytest

from plane_text import ntsb_lookups as lookups

# Rows shaped like `mdb-export` of the embedded eADMSPUB_DataDictionary table.
RAW_DICTIONARY = pl.DataFrame(
    {
        "Category of Data": [
            "eADMS Data",
            "eADMS Data",
            "eADMS Data",
            "eADMS Data",
            "eADMS Data",
            "Pre eADMS coding",
            None,
        ],
        "Table": ["aircraft", "aircraft", "Findings", "Findings", "Events_Sequence", "Occurrences", None],
        "Column": [
            "damage",
            "damage",
            "findings_code",
            "modifier_no",
            "Occurrence_Code",
            "Phase_of_Flight",
            None,
        ],
        "ct_name": ["ct_damage   ", "ct_damage   ", None, None, None, None, None],
        "code_iaids": ["DEST", "SUBS", "01022214XX", "xxxxxxxx01", "152xxx", "540", None],
        "meaning": [
            "Destroyed",
            "Substantial",
            "Aircraft - Autopilot trim indicator",
            "Failure",
            "Standing-Engine(s) Start-up",
            "Cruise",
            None,
        ],
        "Data Type eADMS": ["char", "char", "char", "char", "char", "char", None],
        "Length eADMS": ["4", "4", "10", "10", "6", "3", None],
        "short_desc": [
            "Aircraft damage",
            "Aircraft damage",
            "Finding",
            "Finding",
            "Occurrence",
            "Phase",
            None,
        ],
        "Question_Def": ["Severity of  damage.", None, None, None, None, None, None],
        "Code meaning": [None, None, None, None, None, None, None],
        "typeofchange": [None, None, None, None, None, None, None],
        "Change_notes": [None, None, None, None, None, None, None],
    }
)


def test_columns_table_has_one_row_per_column():
    columns = lookups.columns_table(lookups.dictionary(RAW_DICTIONARY))
    assert columns.height == 5  # the empty row is dropped
    damage = columns.filter(pl.col("column") == "damage").row(0, named=True)
    assert damage["short_description"] == "Aircraft damage"
    assert damage["definition"] == "Severity of damage."  # whitespace collapsed


def test_codes_table_marks_composite_parts():
    codes = lookups.codes_table(lookups.dictionary(RAW_DICTIONARY))
    rows = {r["code"]: r for r in codes.iter_rows(named=True)}
    assert rows["DEST"]["match"] == "exact"
    assert rows["DEST"]["code_part"] == "DEST"
    assert rows["DEST"]["code_set"] == "ct_damage"
    assert (rows["01022214XX"]["match"], rows["01022214XX"]["code_part"]) == ("leading", "01022214")
    assert (rows["xxxxxxxx01"]["match"], rows["xxxxxxxx01"]["code_part"]) == ("trailing", "01")
    assert (rows["152xxx"]["match"], rows["152xxx"]["code_part"]) == ("leading", "152")
    assert rows["540"]["match"] == "exact"


def test_x_outside_composite_columns_is_a_literal_letter():
    raw = RAW_DICTIONARY.head(1).with_columns(pl.lit("PAX").alias("code_iaids"))
    codes = lookups.codes_table(lookups.dictionary(raw))
    assert codes.row(0, named=True)["match"] == "exact"
    assert codes.row(0, named=True)["code_part"] == "PAX"


def test_code_sets_table_types():
    raw = pl.DataFrame(
        {
            "ct_name": ["ct_acft_cat         "],
            "code_iaids": ["WSFT"],
            "meaning": ["Weight-Shift"],
            "seq": ["3"],
            "ntsb_type": [None],
            "ntsb_code": [None],
            "avn_code": [None],
            "ntsb_codes_more": [None],
            "not_for_ntsb_use": ["0"],
            "eADMS_use": ["1"],
            "notes": [""],
        }
    )
    sets = lookups.code_sets_table(raw)
    assert sets.row(0) == ("ct_acft_cat", "WSFT", "Weight-Shift", 3, None, False, True)


def test_countries_table_keeps_each_databases_codes():
    countries = lookups.countries_table(
        {
            "avall": pl.DataFrame(
                {"COUNTRY_CODE": ["AU", "USA"], "COUNTRY_NAME": ["Australia", "United States"]}
            ),
            "Pre2008": pl.DataFrame(
                {"COUNTRY_CODE": ["AU", " AS ", None], "COUNTRY_NAME": ["Austria", "Australia ", None]}
            ),
        }
    )
    assert countries.columns == ["database", "code", "name"]
    assert countries.rows() == [
        ("avall", "AU", "Australia"),
        ("avall", "USA", "United States"),
        ("Pre2008", "AU", "Austria"),
        ("Pre2008", "AS", "Australia"),
    ]


# --- decoding ---------------------------------------------------------------


def lookups_from(codes: list[tuple[str, str, str, str, str | None]]) -> lookups.Lookups:
    """Lookups from (table, column, code, meaning, code_set) dictionary rows, via the real code table."""
    entries = pl.DataFrame(
        codes, schema=["table", "column", "code", "meaning", "code_set"], orient="row"
    ).with_columns(
        pl.lit(None, pl.String).alias(c) for c in ("detail", "category", "change_type", "change_notes")
    )
    code_sets = pl.DataFrame(
        {
            "code_set": ["ct_acft_cat", "ct_acft_cat"],
            "code": ["AIR", "WSFT"],
            "meaning": ["Plane", "Weight-Shift"],
        }
    )
    countries = lookups.countries_table(
        {
            "avall": pl.DataFrame(
                {"COUNTRY_CODE": ["AU", "USA"], "COUNTRY_NAME": ["Australia", "United States"]}
            ),
            "Pre2008": pl.DataFrame({"COUNTRY_CODE": ["AU", "AS"], "COUNTRY_NAME": ["Austria", "Australia"]}),
        }
    )
    return lookups.Lookups(lookups.codes_table(entries), code_sets, countries)


LOOKUPS = lookups_from(
    [
        ("aircraft", "acft_category", "AIR", "Airplane", "ct_acft_cat"),
        ("aircraft", "far_part", "091", "Part 91: General Aviation", "ct_fars"),
        ("aircraft", "far_part", "091", "Part 91 General Aviation", "ct_fars"),
        ("Findings", "findings_code", "01022214XX", "Aircraft - Autopilot trim indicator", None),
        ("Findings", "modifier_no", "xxxxxxxx01", "Failure", None),
        ("Events_Sequence", "Occurrence_Code", "152xxx", "Standing-Engine(s) Start-up", None),
        ("Events_Sequence", "Occurrence_Code", "xxx091", "Tailstrike", None),
        ("Events_Sequence", "Occurrence_Code", "xxx152", "Not a phase", None),
    ]
)


def test_labels_prefer_the_dictionary_then_its_code_set():
    labels = LOOKUPS.labels(lookups.Label("c", "l", "aircraft", "acft_category"))
    assert labels == {"AIR": "Airplane", "WSFT": "Weight-Shift"}  # the dictionary wins; ct_iaids fills gaps
    far = LOOKUPS.labels(lookups.Label("c", "l", "aircraft", "far_part"))
    assert far == {"091": "Part 91: General Aviation"}  # the first wording listed wins


def test_composite_codes_decode_each_end():
    frame = pl.DataFrame({"finding_code": ["0102221401", "0102221499"], "modifier_code": ["01", "99"]})
    out = lookups.add_labels(
        frame,
        (
            lookups.Label("finding_code", "finding", "Findings", "findings_code", "leading"),
            lookups.Label("finding_code", "modifier", "Findings", "modifier_no", "trailing", "modifier_code"),
        ),
        LOOKUPS,
    )
    assert out.columns == ["finding_code", "finding", "modifier_code", "modifier"]
    assert out["finding"].to_list() == ["Aircraft - Autopilot trim indicator"] * 2
    assert out["modifier"].to_list() == ["Failure", None]  # 99 is not in the dictionary

    events = pl.DataFrame({"occurrence_code": ["152091", "091152"]})
    out = lookups.add_labels(
        events,
        (
            lookups.Label("occurrence_code", "phase", "Events_Sequence", "Occurrence_Code", "leading"),
            lookups.Label("occurrence_code", "event", "Events_Sequence", "Occurrence_Code", "trailing"),
        ),
        LOOKUPS,
    )
    # A phase is read only from the first three digits, an event only from the last three.
    assert out.rows() == [
        ("152091", "Standing-Engine(s) Start-up", "Tailstrike"),
        ("091152", None, "Not a phase"),
    ]


def test_country_names_are_per_database():
    assert LOOKUPS.country_names("avall") == {"AU": "Australia", "USA": "United States"}
    assert LOOKUPS.country_names("Pre2008") == {"AU": "Austria", "AS": "Australia"}


def test_place_after_and_coverage():
    frame = pl.DataFrame({"a_code": ["X", "Y", None], "b": [1, 2, 3], "a": ["Ex", None, None]})
    assert lookups.place_after(frame, {"a_code": ["a"]}).columns == ["a_code", "a", "b"]
    coverage = lookups.coverage(frame, [("a_code", "a")])
    row = coverage.row(0, named=True)
    assert (row["coded"], row["labeled"], row["share"]) == (2, 1, 0.5)
    assert lookups.coverage_json(coverage) == '{"a": [2, 1]}'


def test_processed_dir_picks_the_latest_version_not_after_the_snapshot(tmp_path: Path):
    root = tmp_path / "processed" / "ntsb-lookups"
    for version in ("v2026-01-01", "v2026-10-23", "v2026-12-01"):
        (root / version).mkdir(parents=True)
    assert lookups.processed_dir(tmp_path, "2026-10-24") == root / "v2026-10-23"
    with pytest.raises(FileNotFoundError):
        lookups.processed_dir(tmp_path, "2025-01-01")
