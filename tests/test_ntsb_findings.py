import polars as pl

from plane_text import ntsb_findings as findings


def table(output: str) -> findings.Table:
    return next(t for t in findings.TABLES if t.output == output)


FINDINGS = pl.DataFrame(
    {
        "ev_id": ["20080107X00026", "20080107X00026"],
        "Aircraft_Key": ["1", "1"],
        "finding_no": ["1", "2"],
        "finding_code": ["0204151044", "0303404099"],
        "finding_description": ["Personnel issues-...-Pilot - C", "Environmental issues-...-Crosswind - F"],
        "category_no": ["02", "03"],
        "subcategory_no": ["04", "03"],
        "section_no": ["15", "40"],
        "subsection_no": ["10", "40"],
        "modifier_no": ["44", "99"],
        "Cause_Factor": ["C", " "],
        "lchg_date": ["2020-09-25T18:05:31", "2020-09-25T18:05:31"],
        "lchg_userid": [None, None],
        "cm_inPc": ["T", "F"],
    }
)


def test_findings_types_and_flags():
    frame = findings.build(FINDINGS, table("findings"), "avall")
    assert frame.columns[:3] == ["ev_id", "aircraft_key", "finding_no"]
    assert frame.schema["aircraft_key"] == pl.Int16
    assert frame.schema["finding_no"] == pl.Int32
    assert frame["in_probable_cause"].to_list() == [True, False]
    assert frame["cause_factor"].to_list() == ["C", None]  # a blank flag is null
    assert frame["finding_code"].to_list() == ["0204151044", "0303404099"]  # codes stay text
    assert "lchg_userid" not in frame.columns
    assert frame["source_database"].unique().to_list() == ["avall"]


def test_columns_missing_from_older_releases_become_null():
    frame = findings.build(FINDINGS.drop("cm_inPc"), table("findings"), "Pre2008")
    assert frame["in_probable_cause"].to_list() == [None, None]


def test_events_sequence_defining_event():
    raw = pl.DataFrame(
        {
            "ev_id": ["20080107X00026"],
            "Aircraft_Key": ["2"],
            "Occurrence_No": ["1"],
            "Occurrence_Code": ["350250"],
            "Occurrence_Description": ["Initial climb Midair collision"],
            "phase_no": ["350"],
            "eventsoe_no": ["250"],
            "Defining_ev": ["1"],
            "lchg_date": ["2020-09-25T18:05:31"],
            "lchg_userid": [None],
        }
    )
    row = findings.build(raw, table("events_sequence"), "avall").row(0, named=True)
    assert (row["phase_no"], row["event_no"], row["defining_event"]) == ("350", "250", True)


def test_combine_keeps_the_newer_database_per_key():
    newer = findings.build(FINDINGS, table("findings"), "avall")
    older = findings.build(
        FINDINGS.with_columns(pl.lit("Old").alias("finding_description")), table("findings"), "Pre2008"
    )
    combined = findings.combine([newer, older], table("findings"))
    assert combined.height == 2
    assert combined["source_database"].unique().to_list() == ["avall"]
