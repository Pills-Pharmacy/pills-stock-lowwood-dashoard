"""The full PMR 'script' table (CamelCase headers): mapped, filtered and explained on upload."""

import load

CSV = (
    "ScriptID,patient_hash,ScriptDispensedAs,DateDispensed,Quantity,PackSize,"
    "ScanDrugSnomedUnit,MarkedForDeletion,NotDispensed,ScanDrugDmadCode\n"
    "1,abc,BECONASE 50MCG SPRAY,2025-10-06 12:56:47,1,1,DOSE,0,FALSE,3.9E+16\n"
    "2,abc,AMOXICILLIN 500MG CAPS,2025-10-07 09:00:00,21,21,CAPSULE,1,FALSE,3.9E+16\n"
    "3,def,RAMIPRIL 5MG TABS,2025-10-08 10:00:00,28,28,TABLET,0,True,3.9E+16\n"
    "4,def,RAMIPRIL 5MG TABS,2025-10-09 10:00:00,28,28,TABLET,0,FALSE,3.9E+16\n"
)


def test_script_table_headers_are_mapped_and_inactive_rows_removed():
    result = load.load_source("pmr", CSV.encode())
    assert not result.stopped
    assert list(result.data["item_name"]) == ["BECONASE 50MCG SPRAY", "RAMIPRIL 5MG TABS"]
    assert {"dispensed_qty", "pack_size", "unit", "dispense_date"} <= set(result.data.columns)
    assert "patient_hash" in result.dropped_patient_columns
    assert [i.message for i in result.by_check("inactive_rows")] == [
        "1 row(s) flagged marked_deleted were removed (they are not real usage)",
        "1 row(s) flagged not_dispensed were removed (they are not real usage)",
    ]
    assert list(result.data.index) == [0, 3]  # source_row still points at the export line


def test_missing_required_columns_tell_the_user_what_to_provide():
    result = load.load_source("pmr", b"Foo,Bar\n1,2\n")
    [issue] = result.by_check("required_columns")
    assert "item_name" in issue.message and "drug name" in issue.message
    assert "dispense_date" in issue.message and "re-export" in issue.message


def test_absent_optional_columns_are_listed_but_do_not_stop_the_analysis():
    result = load.load_source("pmr", b"Product Name,Date Dispensed\nA 5MG,2026-01-02\n")
    assert not result.stopped
    [issue] = result.by_check("optional_columns")
    assert "dispensed_qty" in issue.message and issue.severity == "info"


def test_an_xlsx_workbook_is_read_like_the_same_csv(tmp_path):
    import datetime as dt
    import io

    import openpyxl
    import pytest

    book = openpyxl.Workbook()
    book.active.append(["Product Name", "Date Dispensed", "Disp Qty"])
    book.active.append(["RAMIPRIL 5MG TABS", dt.datetime(2026, 1, 2, 9, 30), 28.0])
    buffer = io.BytesIO()
    book.save(buffer)
    csv_bytes = load.xlsx_to_csv_bytes(buffer.getvalue())
    assert b"2026-01-02 09:30:00" in csv_bytes and b",28" in csv_bytes
    result = load.load_source("pmr", csv_bytes)
    assert not result.stopped and list(result.data["item_name"]) == ["RAMIPRIL 5MG TABS"]
    with pytest.raises(ValueError):
        load.xlsx_to_csv_bytes(b"not a workbook")
