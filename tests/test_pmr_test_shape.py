"""The four-column PMR shape seen in pmr_test.csv, rebuilt with made-up values."""

import pandas as pd
import pytest

import load

PMR = "pmr"
HEADER = "Dispensed Date,EPES ID,Generic Name,Script Dispensed As"
ROWS = [
    "10/1/2026 9:29,ID-0001,ATORVASTATIN,ATORVASTATIN 20MG TABLETS",
    "10/1/2026 11:13,ID-0002,OMEPRAZOLE,OMEPRAZOLE 20MG CAPSULES GR",
    "10/3/2026 16:12,ID-0001,,CO-CODAMOL 30MG/500MG TABLETS",
    "10/6/2026 16:35,ID-0003,ATORVASTATIN,ATORVASTATIN 40MG TABLETS",
]
DATA = (HEADER + "\n" + "\n".join(ROWS) + "\n").encode()


def test_four_column_pmr_loads_and_maps():
    result = load.load_source(PMR, DATA)
    assert not result.stopped
    assert list(result.data.columns) == ["dispense_date", "generic_name", "item_name"]
    assert result.data.iloc[0]["item_name"] == "ATORVASTATIN 20MG TABLETS"


def test_epes_id_is_dropped_as_an_identifier():
    result = load.load_source(PMR, DATA)
    assert result.dropped_patient_columns == ["EPES ID"]
    assert "ID-0001" not in result.data.to_csv()
    [issue] = result.by_check("patient_fields")
    assert "ID-0001" not in issue.message


@pytest.mark.parametrize("name", ["Email", "Mobile No.", "Home Phone", "EPES ID"])
def test_contact_and_identifier_headers_are_patient_fields(name):
    assert load.detect_patient_columns([name, "Generic Name"]) == [name]


def test_missing_quantity_is_a_warning_not_a_stop():
    result = load.load_source(PMR, DATA)
    [issue] = result.by_check("no_quantity")
    assert issue.severity == load.WARNING and "one dispensing line" in issue.message


def test_quantity_column_when_present_is_still_checked():
    data = b"Dispensed Date,Script Dispensed As,Qty Dispensed\n01/02/2026,X,-3\n"
    result = load.load_source(PMR, data)
    assert not result.by_check("no_quantity")
    assert result.by_check("negative_or_missing_qty")


def test_month_first_dates_are_recognised_without_a_value_above_12():
    result = load.load_source(PMR, DATA)
    assert result.date_order == "month-first"
    [issue] = result.by_check("date_format")
    assert issue.severity == load.WARNING and "guessed" in issue.message
    dates = pd.to_datetime(result.data["dispense_date"], format="mixed", dayfirst=False)
    assert dates.min().day == 1 and dates.max().day == 6 and dates.max().month == 10


def test_a_value_above_12_settles_the_order():
    dates, order, basis = load.parse_dates(pd.Series(["13/02/2026", "01/02/2026"]))
    assert (order, basis) == ("day-first", "certain")
    assert dates.iloc[0].day == 13
    dates, order, basis = load.parse_dates(pd.Series(["02/13/2026", "02/01/2026"]))
    assert (order, basis) == ("month-first", "certain") and dates.iloc[0].day == 13


def test_iso_dates_need_no_guess():
    dates, order, basis = load.parse_dates(pd.Series(["2026-02-03", "2026-03-04"]))
    assert (order, basis) == ("iso", "iso") and dates.iloc[0].month == 2


def test_fully_ambiguous_dates_fall_back_to_day_first():
    _, order, basis = load.parse_dates(pd.Series(["01/02/2026"]))
    assert (order, basis) == ("day-first", "guessed")


def test_range_is_measured_in_whole_days_ignoring_time():
    result = load.load_source(PMR, DATA)
    [issue] = result.by_check("pmr_date_range")
    assert "covers 6 days" in issue.message and "2026-10-01 to 2026-10-06" in issue.message


def test_pmr_without_branch_still_warns():
    assert load.load_source(PMR, DATA).by_check("branch_present")
