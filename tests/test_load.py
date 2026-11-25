from pathlib import Path

import pytest

import load
from config import DEFAULT_SETTINGS
from load import ERROR, INFO, WARNING

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"


def fixture(name: str, source: str, settings=DEFAULT_SETTINGS) -> load.LoadResult:
    return load.load_source(source, FIXTURES / name, settings)


def problems(result: load.LoadResult) -> list[load.Issue]:
    return [i for i in result.issues if i.severity != INFO]


# ---- clean baselines -------------------------------------------------------------------


def test_clean_robot_files_have_no_problems():
    for name, source in (("omnicell_robot_1.csv", R1), ("omnicell_robot_2.csv", R2)):
        result = fixture(name, source)
        assert not result.stopped and problems(result) == []
        assert len(result.data) == 6


def test_clean_pmr_only_flags_the_patient_column():
    result = fixture("pmr_usage.csv", PMR)
    assert [i.check for i in problems(result)] == ["patient_fields"]


# ---- encoding --------------------------------------------------------------------------


def test_utf8_file_reports_utf8():
    assert fixture("omnicell_robot_1.csv", R1).encoding == "utf-8"


def test_cp1252_falls_back_and_keeps_the_micro_sign():
    result = fixture("omnicell_robot_1__cp1252.csv", R1)
    assert result.encoding == "windows-1252"
    assert any("µg" in name for name in result.data["item_name"])
    assert problems(result) == []


def test_bom_is_stripped_and_first_header_still_maps():
    result = fixture("omnicell_robot_1__utf8_bom.csv", R1)
    assert result.encoding == "utf-8 (with BOM)"
    assert "item_code" in result.data.columns
    assert problems(result) == []


def test_undecodable_and_empty_files_stop_with_an_error():
    bad = load.load_source(R1, b"a,b\n\x81,1\n")
    assert bad.stopped and bad.by_check("unreadable_file")[0].severity == ERROR
    empty = load.load_source(R1, b"")
    assert empty.stopped and empty.by_check("unreadable_file")


def test_header_only_file_stops_as_empty():
    result = load.load_source(R1, b"Item Description,Qty On Hand\n")
    assert result.stopped and result.by_check("empty_file")


# ---- one test per planted problem --------------------------------------------------------


def test_missing_required_column_names_it_and_stops():
    result = fixture("omnicell_robot_1__missing_required_column.csv", R1)
    assert result.stopped
    [issue] = result.by_check("required_columns")
    assert issue.severity == ERROR and "stock_qty" in issue.message


def test_duplicates_are_reported_with_rows_and_conflict_severity():
    result = fixture("omnicell_robot_1__duplicate_rows.csv", R1)
    [issue] = result.by_check("duplicates")
    assert issue.rows == (2, 4, 8, 9)
    assert "2 item(s)" in issue.message and "1 with different quantities" in issue.message
    assert issue.severity == ERROR


def test_exact_duplicates_alone_are_a_warning():
    data = b"Item Code,Item Description,Qty On Hand\nA1,Drug X,5\nA1,Drug X,5\n"
    [issue] = load.load_source(R1, data).by_check("duplicates")
    assert issue.severity == WARNING and issue.rows == (2, 3)


def test_duplicates_fall_back_to_name_when_code_is_blank():
    data = b"Item Code,Item Description,Qty On Hand\n,Drug  X,5\n,drug x,5\n"
    assert load.load_source(R1, data).by_check("duplicates")


def test_negative_and_missing_quantities():
    result = fixture("omnicell_robot_1__negative_qty.csv", R1)
    issues = result.by_check("negative_or_missing_qty")
    assert {i.rows for i in issues} == {(6,), (3,)}
    assert not result.stopped  # rows are kept, the analysis decides what to do


def test_pmr_negative_quantity():
    result = fixture("pmr_usage__negative_qty.csv", PMR)
    [issue] = result.by_check("negative_or_missing_qty")
    assert len(issue.rows) == 1 and "dispensed_qty" in issue.message


def test_pack_unit_inside_one_export():
    result = fixture("omnicell_robot_1__unit_mismatch.csv", R1)
    [issue] = result.by_check("unit_mismatch")
    assert issue.rows == (2,)


def test_unit_mismatch_between_robot_and_pmr_when_codes_are_shared():
    robot = fixture("omnicell_robot_1__unit_mismatch.csv", R1)
    pmr = fixture("pmr_usage__shared_codes.csv", PMR)
    [issue] = [i for i in load.validate_across({R1: robot, PMR: pmr}) if i.check == "unit_mismatch"]
    assert issue.source == R1 and issue.rows == (2,)


def test_no_unit_mismatch_across_when_units_agree_or_codes_differ():
    robot = fixture("omnicell_robot_1.csv", R1)
    for name in ("pmr_usage.csv", "pmr_usage__shared_codes.csv"):
        pmr = fixture(name, PMR)
        found = load.validate_across({R1: robot, PMR: pmr})
        assert not [i for i in found if i.check == "unit_mismatch"]


def test_short_pmr_range_warns_and_depends_on_period():
    result = fixture("pmr_usage__short_range.csv", PMR)
    [issue] = result.by_check("pmr_date_range")
    assert issue.severity == WARNING and "90 days" in issue.message
    relaxed = DEFAULT_SETTINGS.with_value("period_days", 20)
    assert not fixture("pmr_usage__short_range.csv", PMR, relaxed).by_check("pmr_date_range")


def test_full_range_pmr_does_not_warn():
    assert not fixture("pmr_usage.csv", PMR).by_check("pmr_date_range")


def test_unreadable_dates_are_flagged():
    data = b"Product Name,Qty Dispensed,Date Dispensed\nX,1,not a date\nX,1,01/02/2026\n"
    [issue] = [i for i in load.load_source(PMR, data).by_check("pmr_date_range") if i.rows]
    assert issue.rows == (2,) and issue.severity == ERROR


def test_blank_branch_rows_are_flagged():
    result = fixture("pmr_usage__missing_branch.csv", PMR)
    [issue] = result.by_check("branch_present")
    assert issue.severity == ERROR and len(issue.rows) == 5 and issue.rows[:3] == (2, 3, 4)


def test_pmr_without_branch_column_warns():
    data = b"Product Name,Qty Dispensed,Date Dispensed\nX,1,01/02/2026\n"
    [issue] = load.load_source(PMR, data).by_check("branch_present")
    assert issue.severity == WARNING


def test_robot_missing_from_registry_is_an_error(monkeypatch):
    monkeypatch.delitem(load.ROBOT_REGISTRY, "robot_1")
    [issue] = fixture("omnicell_robot_1.csv", R1).by_check("branch_present")
    assert issue.severity == ERROR and "robot_1" in issue.message


def test_zero_usage_rows_load_cleanly():
    result = fixture("pmr_usage__zero_usage_item.csv", PMR)
    assert not result.by_check("negative_or_missing_qty")


def test_items_in_one_file_only_or_both_robots_are_not_load_errors():
    # These are matching and analysis concerns (M5, M6); load must let them through untouched.
    for name, source in (
        ("omnicell_robot_1__item_not_in_pmr.csv", R1),
        ("omnicell_robot_2__item_in_both_robots.csv", R2),
    ):
        assert problems(fixture(name, source)) == []
    assert [i.check for i in problems(fixture("pmr_usage__item_not_in_robots.csv", PMR))] == [
        "patient_fields"
    ]


def test_different_robot_headers_warn_and_same_headers_do_not():
    r1 = fixture("omnicell_robot_1.csv", R1)
    r2 = fixture("omnicell_robot_2.csv", R2)
    [issue] = load.validate_across({R1: r1, R2: r2})
    assert issue.check == "same_headers" and issue.severity == WARNING
    same = fixture("omnicell_robot_2__same_headers_as_robot_1.csv", R2)
    assert load.validate_across({R1: r1, R2: same}) == []


def test_same_headers_check_needs_both_robots():
    r1 = fixture("omnicell_robot_1.csv", R1)
    assert load.validate_across({R1: r1}) == []


# ---- patient fields --------------------------------------------------------------------


def test_patient_column_is_dropped_flagged_and_never_quoted():
    result = fixture("pmr_usage.csv", PMR)
    assert result.dropped_patient_columns == ["Patient ID"]
    assert "Patient ID" not in result.raw_headers
    assert not any("patient" in c.lower() for c in result.data.columns)
    [issue] = result.by_check("patient_fields")
    assert "Patient ID" in issue.message and "PT-FAKE" not in issue.message
    assert "PT-FAKE" not in result.data.to_csv()


def test_patient_columns_are_dropped_even_when_required_columns_are_missing():
    data = b"Patient Name,Qty On Hand\nJane Doe,5\n"
    result = load.load_source(R1, data)
    assert result.stopped and result.dropped_patient_columns == ["Patient Name"]
    assert "Patient Name" not in result.raw_headers


def test_patient_patterns_are_configurable():
    headers = ["Item", "Ward Bed"]
    assert load.detect_patient_columns(headers) == []
    assert load.detect_patient_columns(headers, ["bed"]) == ["Ward Bed"]


# ---- mapping ---------------------------------------------------------------------------


def test_ambiguous_and_unmapped_columns_are_reported():
    data = b"Item Code,Item ID,Item Description,Qty On Hand,Colour\nA,B,Drug,5,red\n"
    result = load.load_source(R1, data)
    assert result.by_check("ambiguous_columns")
    assert result.by_check("unmapped_columns")[0].severity == INFO
    assert list(result.data.columns) == ["item_code", "item_name", "stock_qty"]
    assert result.data.iloc[0]["item_code"] == "A"


def test_custom_column_map_is_respected():
    cmap = {"omnicell_robot_1": {"item_name": ["Thing"], "stock_qty": ["How Many"]}}
    result = load.load_source(R1, b"Thing,How Many\nX,3\n", column_map=cmap)
    assert list(result.data.columns) == ["item_name", "stock_qty"]


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.csv")))
def test_every_fixture_loads_without_crashing(name):
    source = {
        "omnicell_robot_1": R1,
        "omnicell_robot_2": R2,
        "pmr_usage": PMR,
        "omnicell_activity": "omnicell_activity",
    }[name.split("__")[0].removesuffix(".csv")]
    result = fixture(name, source)
    assert result.stopped == name.endswith("missing_required_column.csv")
