import polars as pl

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
