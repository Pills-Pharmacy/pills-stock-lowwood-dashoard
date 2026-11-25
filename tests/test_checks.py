"""The upload check: found and missing columns, hints for unknown headers, rows removed."""

import checks

PMR = "pmr"


def check(csv: str, **kwargs):
    return checks.check_upload(PMR, csv.encode(), **kwargs)


def by_name(result):
    return {c.internal: c for c in result.columns}


def test_a_complete_file_is_ready_and_lists_what_each_column_was_found_as():
    result = check("Product Name,Date Dispensed,Disp Qty\nA 5MG,2026-01-02,28\n")
    assert result.ready and not result.missing_required
    assert by_name(result)["dispensed_qty"].found_as == "Disp Qty"
    assert {c.internal for c in result.missing_optional} >= {"unit", "pack_size"}


def test_an_unknown_quantity_header_is_suggested_for_the_missing_column():
    result = check("Product Name,Date Dispensed,Qty Out,Notes\nA 5MG,2026-01-02,28,x\n")
    column = by_name(result)["dispensed_qty"]
    assert not column.found and column.suggestions[0] == "Qty Out"
    assert "Notes" not in column.suggestions


def test_a_missing_required_column_blocks_and_suggests_a_lookalike():
    result = check("Drug Description,Date Dispensed\nA 5MG,2026-01-02\n")
    assert not result.ready
    assert [c.internal for c in result.missing_required] == ["item_name"]
    assert by_name(result)["item_name"].suggestions == ("Drug Description",)
    assert any("item_name" in b for b in result.blockers)


def test_removed_rows_and_dropped_patient_columns_are_counted_without_values():
    result = check(
        "Product Name,Date Dispensed,Disp Qty,MarkedForDeletion,NotDispensed,patient_hash\n"
        "A 5MG,2026-01-02,28,0,FALSE,x1\n"
        "B 5MG,2026-01-03,28,1,FALSE,x2\n"
        "C 5MG,2026-01-04,28,0,TRUE,x3\n"
    )
    assert result.rows_used == 1 and result.rows_read == 3
    assert result.removed == {"deleted in the PMR": 1, "never dispensed": 1}
    assert result.dropped_patient == ["patient_hash"]
    assert "x1" not in repr(result)


def test_an_unreadable_file_is_reported_not_raised():
    result = check("")
    assert not result.ready and result.blockers


def test_a_user_override_counts_as_found():
    result = check(
        "Product Name,Date Dispensed,Qty Out\nA 5MG,2026-01-02,28\n",
        overrides={PMR: {"dispensed_qty": "Qty Out"}},
    )
    assert by_name(result)["dispensed_qty"].found_as == "Qty Out"
