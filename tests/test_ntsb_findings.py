import polars as pl

from plane_text import ntsb_findings as findings
from plane_text import ntsb_lookups


def table(output: str) -> findings.Table:
    return next(t for t in findings.TABLES if t.output == output)


def entry(table: str, column: str, code: str, meaning: str) -> dict:
    """A processed `codes` row, its match and code part derived as the lookups pipeline does."""
    match = "leading" if code.endswith(("X", "x")) else "trailing" if code.startswith(("X", "x")) else "exact"
    part = code if match == "exact" else code.strip("Xx")
    return {"table": table, "column": column, "code": code, "match": match, "code_part": part} | {
        "meaning": meaning,
        "code_set": None,
    }


LOOKUPS = ntsb_lookups.Lookups(
    codes=pl.DataFrame(
        [
            entry("Findings", "findings_code", "02041510XX", "Personnel issues - Pilot - Decision"),
            entry("Findings", "findings_code", "03034040XX", "Environmental issues - Crosswind"),
            entry("Findings", "modifier_no", "xxxxxxxx44", "Pilot"),
            entry("Events_Sequence", "Occurrence_Code", "350xxx", "Initial Climb"),
            entry("Events_Sequence", "Occurrence_Code", "xxx250", "Midair collision"),
            entry("Events_Sequence", "Occurrence_Code", "250xxx", "Taxi"),
            entry("Occurrences", "Occurrence_Code", "350", "Loss of engine power"),
            entry("Occurrences", "Phase_of_Flight", "560", "Approach"),
            entry("seq_of_events", "group_code", "1", "Aircraft/Environment"),
            entry("seq_of_events", "Subj_Code", "10322", "Flight control, flap attachment"),
            entry("seq_of_events", "Cause_Factor", "C", "Cause of Accident"),
            entry("seq_of_events", "Cause_Factor", "F", "Factor in Accident"),
            entry("seq_of_events", "Modifier_Code", "1224", "Fractured"),
            entry("seq_of_events", "Person_Code", "4123", "Manufacturer"),
        ]
    ),
    code_sets=pl.DataFrame(schema={"code_set": pl.String, "code": pl.String, "meaning": pl.String}),
    countries=pl.DataFrame(schema={"database": pl.String, "code": pl.String, "name": pl.String}),
)

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
        "Cause_Factor": ["c", " "],
        "lchg_date": ["2020-09-25T18:05:31", "2020-09-25T18:05:31"],
        "lchg_userid": [None, None],
        "cm_inPc": ["T", "F"],
    }
).cast(pl.String)


def processed(output: str, *frames: tuple[pl.DataFrame, str]) -> pl.DataFrame:
    built = [findings.build(raw, table(output), database) for raw, database in frames]
    return findings.process(built, table(output), LOOKUPS)[0]


def test_findings_types_flags_and_labels():
    frame = processed("findings", (FINDINGS, "avall"))
    assert frame.columns == [
        "ev_id",
        "aircraft_key",
        "finding_no",
        "finding_code",
        "finding",
        "modifier_code",
        "modifier",
        "finding_description",
        "category_no",
        "subcategory_no",
        "section_no",
        "subsection_no",
        "in_probable_cause",
        "cause_factor_code",
        "cause_factor",
        "source_database",
    ]
    assert frame.schema["aircraft_key"] == pl.Int16
    assert frame.schema["finding_no"] == pl.Int32
    assert frame["in_probable_cause"].to_list() == [True, False]
    assert frame["finding_code"].to_list() == ["0204151044", "0303404099"]  # codes stay text
    # The finding is the first eight digits, the modifier the last two.
    assert frame["finding"].to_list() == [
        "Personnel issues - Pilot - Decision",
        "Environmental issues - Crosswind",
    ]
    assert frame["modifier"].to_list() == ["Pilot", None]
    assert frame["modifier_code"].to_list() == ["44", "99"]
    # A stray lowercase flag is uppercased; a blank one is null.
    assert frame["cause_factor_code"].to_list() == ["C", None]
    assert frame["cause_factor"].to_list() == ["Cause of Accident", None]
    assert "lchg_userid" not in frame.columns
    assert frame["source_database"].unique().to_list() == ["avall"]


def test_columns_missing_from_older_releases_become_null():
    frame = processed("findings", (FINDINGS.drop("cm_inPc"), "Pre2008"))
    assert frame["in_probable_cause"].to_list() == [None, None]


def test_events_sequence_phase_and_event_from_the_composite_code():
    raw = pl.DataFrame(
        {
            "ev_id": ["20080107X00026", "20080107X00026"],
            "Aircraft_Key": ["2", "2"],
            "Occurrence_No": ["1", "2"],
            "Occurrence_Code": ["350250", "250350"],
            "Occurrence_Description": ["Initial climb Midair collision", "Taxi Something"],
            "phase_no": ["350", "250"],
            "eventsoe_no": ["250", "350"],
            "Defining_ev": ["1", "0"],
            "lchg_date": ["2020-09-25T18:05:31", None],
            "lchg_userid": [None, None],
        }
    ).cast(pl.String)
    frame = processed("events_sequence", (raw, "avall"))
    columns = frame.columns
    assert columns[columns.index("phase_code") :][:4] == ["phase_code", "phase", "event_code", "event"]
    first, second = frame.iter_rows(named=True)
    assert (first["phase_code"], first["phase"], first["event_code"], first["event"]) == (
        "350",
        "Initial Climb",
        "250",
        "Midair collision",
    )
    assert first["defining_event"] is True
    # 350 is a phase only at the front; 250 an event only at the back.
    assert (second["phase"], second["event"]) == ("Taxi", None)


def test_pre2008_tables_decode_codes_and_treat_zero_as_none():
    occurrences = pl.DataFrame(
        {
            "ev_id": ["20001204X00000"],
            "Aircraft_Key": ["1"],
            "Occurrence_No": ["1"],
            "Occurrence_Code": ["350"],
            "Phase_of_Flight": ["560"],
            "Altitude": ["0"],
            "lchg_date": [None],
            "lchg_userid": [None],
        }
    ).cast(pl.String)
    row = processed("occurrences", (occurrences, "Pre2008")).row(0, named=True)
    assert (row["occurrence_code"], row["occurrence"]) == ("350", "Loss of engine power")
    assert (row["phase_of_flight_code"], row["phase_of_flight"]) == ("560", "Approach")
    assert row["altitude"] == 0

    sequence = pl.DataFrame(
        {
            "ev_id": ["20001204X00000"] * 2,
            "Aircraft_Key": ["1", "1"],
            "Occurrence_No": ["1", "1"],
            "seq_event_no": ["1", "2"],
            "group_code": ["1", "1"],
            "Subj_Code": ["10322", "99999"],
            "Cause_Factor": ["f", "C"],
            "Modifier_Code": ["1224", "0"],
            "Person_Code": ["0", "4123"],
            "lchg_date": [None, None],
            "lchg_userid": [None, None],
        }
    ).cast(pl.String)
    frame = processed("sequence_of_events", (sequence, "Pre2008"))
    first, second = frame.iter_rows(named=True)
    assert (first["group_code"], first["group"]) == ("1", "Aircraft/Environment")
    assert (first["subject_code"], first["subject"]) == ("10322", "Flight control, flap attachment")
    assert (first["cause_factor_code"], first["cause_factor"]) == ("F", "Factor in Accident")
    assert (first["modifier_code"], first["modifier"]) == ("1224", "Fractured")
    assert (first["person_code"], first["person"]) == (None, None)  # 0: no person
    assert (second["subject_code"], second["subject"]) == ("99999", None)  # unknown code kept, no label
    assert (second["modifier_code"], second["person"]) == (None, "Manufacturer")

    coverage = {
        r["label"]: r
        for r in findings.label_coverage(frame, table("sequence_of_events")).iter_rows(named=True)
    }
    assert (coverage["subject"]["coded"], coverage["subject"]["labeled"]) == (2, 1)
    assert (coverage["person"]["coded"], coverage["person"]["labeled"]) == (1, 1)


def test_newer_database_wins_per_key():
    frame = processed(
        "findings",
        (FINDINGS, "avall"),
        (FINDINGS.with_columns(pl.lit("Old").alias("finding_description")), "Pre2008"),
    )
    assert frame.height == 2
    assert frame["source_database"].unique().to_list() == ["avall"]


def test_unparseable_numbers_are_counted():
    frame = FINDINGS.with_columns(pl.Series("finding_no", ["1", "x"]))
    built = [findings.build(frame, table("findings"), "avall")]
    out, unparsed = findings.process(built, table("findings"), LOOKUPS)
    assert out["finding_no"].to_list() == [None, 1]  # sorted: nulls first
    assert unparsed["finding_no"] == 1
