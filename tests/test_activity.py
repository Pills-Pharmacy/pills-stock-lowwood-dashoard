from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import activity
import load
from config import DEFAULT_SETTINGS
from frames import usage_of, usage_row
from schema import STANDARD_ACTIVITY_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures"
SRC = "omnicell_activity"
HEADER = b"DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED\n"

# Five hand-made days, newest first. 2026-10-01 is a Thursday, so the 3rd and 4th are a weekend.
FIVE_DAYS = HEADER + (
    b"05.10.26;24;Medimat1;100;50;0\n"  # Monday
    b"04.10.26;24;Medimat1;0;0;0\n"  # Sunday
    b"03.10.26;24;Medimat1;0;0;0\n"  # Saturday
    b"02.10.26;24;Medimat1;200;60;10\n"  # Friday
    b"01.10.26;24;Medimat1;300;70;0\n"  # Thursday
)


def fx(name: str) -> load.LoadResult:
    return load.load_source(SRC, FIXTURES / name)


def problems(result: load.LoadResult) -> list[tuple[str, str]]:
    return [(i.check, i.severity) for i in result.issues if i.severity != load.INFO]


def cleaned(content: bytes, pull: int = 0) -> pd.DataFrame:
    return activity.clean_activity(load.load_source(SRC, content), pull)


# ---- load checks: one test per planted problem -----------------------------------------------------


def test_baseline_loads_and_only_notes_that_it_is_shorter_than_the_period():
    result = fx("omnicell_activity.csv")
    assert not result.stopped and len(result.data) == 31 and result.delimiter == ";"
    assert result.date_order == "day-first" and result.detected_type == "omnicell_activity"
    assert problems(result) == [("activity_range", "warning")]


def test_activity_range_follows_the_period_setting():
    short = DEFAULT_SETTINGS.with_value("period_days", 20)
    result = load.load_source(SRC, FIXTURES / "omnicell_activity.csv", short)
    assert problems(result) == []


def test_shuffled_columns_give_the_same_data_as_the_baseline():
    base = fx("omnicell_activity.csv").data
    shuffled = fx("omnicell_activity__shuffled_columns.csv")
    assert shuffled.raw_headers[0] == "PERIOD(H)" and shuffled.raw_headers[-1] == "DATE"
    pd.testing.assert_frame_equal(shuffled.data[list(base.columns)], base)


def test_a_missing_day_is_reported():
    [issue] = fx("omnicell_activity__missing_day.csv").by_check("missing_days")
    assert issue.severity == load.WARNING
    assert "1 day(s) missing between 2026-09-19 and 2026-10-19" in issue.message


def test_repeated_days_are_reported_with_conflicts_as_errors():
    [issue] = fx("omnicell_activity__duplicate_days.csv").by_check("duplicates")
    assert issue.severity == load.ERROR
    assert "2 day(s) appear more than once, 1 with different counts" in issue.message
    assert issue.rows == (2, 5, 33, 34)


def test_exact_repeats_alone_are_only_a_warning():
    data = FIVE_DAYS + b"05.10.26;24;Medimat1;100;50;0\n"
    [issue] = load.load_source(SRC, data).by_check("duplicates")
    assert issue.severity == load.WARNING and "with different counts" not in issue.message


def test_a_period_other_than_24_hours_is_flagged():
    [issue] = fx("omnicell_activity__period_not_24.csv").by_check("period_hours")
    assert issue.rows == (2,) and "other than 24 hours" in issue.message


def test_negative_and_missing_counts_are_flagged():
    result = fx("omnicell_activity__negative_count.csv")
    found = {
        i.message.split(" have ")[1]: i.rows for i in result.by_check("negative_or_missing_qty")
    }
    assert found == {"a negative dispensed": (5,), "a blank or non-numeric stocked": (6,)}


def test_an_unknown_system_is_flagged_because_its_branch_is_unknown():
    [issue] = fx("omnicell_activity__unknown_system.csv").by_check("system_registry")
    assert "Medimat2" in issue.message and issue.severity == load.WARNING


def test_a_missing_required_column_stops_the_load_and_names_it():
    result = fx("omnicell_activity__missing_required_column.csv")
    assert result.stopped
    assert "dispensed" in result.by_check("required_columns")[0].message


def test_unreadable_dates_are_an_error():
    data = HEADER + b"not a date;24;Medimat1;1;2;3\n01.10.26;24;Medimat1;1;2;3\n"
    [issue] = load.load_source(SRC, data).by_check("activity_dates")
    assert issue.severity == load.ERROR and issue.rows == (2,)


def test_dates_that_could_be_either_order_are_guessed_and_warned():
    data = HEADER + b"05.10.26;24;Medimat1;1;2;3\n04.10.26;24;Medimat1;1;2;3\n"
    result = load.load_source(SRC, data)
    [issue] = result.by_check("date_format")
    assert issue.severity == load.WARNING and result.date_order == "day-first"


def test_the_real_header_names_are_mapped_by_name_not_position():
    result = load.load_source(SRC, FIVE_DAYS)
    assert list(result.data.columns) == [
        "activity_date",
        "period_hours",
        "system_name",
        "dispensed",
        "stocked",
        "rearranged",
    ]


# ---- cleaning -----------------------------------------------------------------------------------------


def test_clean_gives_the_standard_table_with_branch_from_the_registry():
    table = cleaned(FIVE_DAYS, pull=3)
    assert tuple(table.columns) == STANDARD_ACTIVITY_COLUMNS
    assert set(table["robot_id"]) == {"robot_1"} and set(table["branch"]) == {"Branch A"}
    assert set(table["pull"]) == {3} and table["source_row"].tolist() == [2, 3, 4, 5, 6]
    assert table["activity_date"].iloc[0] == pd.Timestamp("2026-10-05")
    assert table["dispensed"].dtype == float


def test_an_unknown_system_gets_the_unknown_branch():
    table = activity.clean_activity(fx("omnicell_activity__unknown_system.csv"))
    row = table[table["system_name"] == "Medimat2"].iloc[0]
    assert row["robot_id"] == "" and row["branch"] == "Unknown"


def test_clean_refuses_a_stopped_load():
    with pytest.raises(ValueError):
        activity.clean_activity(fx("omnicell_activity__missing_required_column.csv"))


def test_bad_counts_become_nan_and_no_row_is_lost():
    table = activity.clean_activity(fx("omnicell_activity__negative_count.csv"))
    assert (
        len(table) == 31 and table["dispensed"].iloc[3] == -4 and np.isnan(table["stocked"].iloc[4])
    )


# ---- merging repeated pulls -------------------------------------------------------------------------------


def test_a_later_pull_wins_and_every_changed_count_is_reported():
    base = activity.clean_activity(fx("omnicell_activity.csv"), 0)
    pull_2 = activity.clean_activity(fx("omnicell_activity__pull_2.csv"), 1)
    merged, conflicts = activity.merge_pulls([base, pull_2])
    assert len(merged) == 33  # 31 days plus 20.10 and 21.10
    assert merged["activity_date"].min() == pd.Timestamp("2026-09-19")
    assert merged["activity_date"].max() == pd.Timestamp("2026-10-21")
    assert len(conflicts) == 3
    by = {(r.activity_date.strftime("%m-%d"), r.field): r for r in conflicts.itertuples()}
    assert set(by) == {("10-19", "dispensed"), ("10-19", "rearranged"), ("10-16", "stocked")}
    assert (
        by[("10-19", "rearranged")].earlier_value == 0
        and by[("10-19", "rearranged")].kept_value == 829
    )
    assert by[("10-19", "dispensed")].kept_value == by[("10-19", "dispensed")].earlier_value + 1
    kept = merged.set_index("activity_date")
    assert kept.loc[pd.Timestamp("2026-10-19"), "rearranged"] == 829


def test_merge_does_not_depend_on_the_order_the_pulls_are_listed():
    base = activity.clean_activity(fx("omnicell_activity.csv"), 0)
    pull_2 = activity.clean_activity(fx("omnicell_activity__pull_2.csv"), 1)
    forward, _ = activity.merge_pulls([base, pull_2])
    backward, _ = activity.merge_pulls([pull_2, base])
    key = ["system_name", "activity_date", "dispensed", "stocked", "rearranged"]
    pd.testing.assert_frame_equal(forward[key], backward[key])


def test_pulls_that_agree_report_no_changes():
    base = activity.clean_activity(fx("omnicell_activity.csv"), 0)
    merged, conflicts = activity.merge_pulls([base, base.assign(pull=1)])
    assert len(merged) == 31 and conflicts.empty


def test_a_tie_on_the_newest_day_goes_to_the_later_pull_in_the_list():
    first = cleaned(HEADER + b"01.10.26;24;Medimat1;100;0;0\n", 0)
    second = cleaned(HEADER + b"01.10.26;24;Medimat1;999;0;0\n", 1)
    merged, conflicts = activity.merge_pulls([first, second])
    assert merged["dispensed"].tolist() == [999] and conflicts["earlier_value"].tolist() == [100]


def test_rows_without_a_readable_date_are_left_out_of_the_merge():
    data = HEADER + b"nope;24;Medimat1;5;5;5\n01.10.26;24;Medimat1;1;1;1\n"
    merged, _ = activity.merge_pulls([cleaned(data)])
    assert len(merged) == 1


def test_merging_nothing_gives_empty_tables():
    merged, conflicts = activity.merge_pulls([])
    assert merged.empty and conflicts.empty
    assert tuple(merged.columns) == STANDARD_ACTIVITY_COLUMNS


# ---- the hand-calculated analysis ---------------------------------------------------------------------------


def analyse(content: bytes = FIVE_DAYS, settings=DEFAULT_SETTINGS, **kwargs):
    return activity.analyse_activity(cleaned(content), settings, **kwargs)


def test_summary_numbers_are_hand_checked():
    [row] = analyse().summary.to_dict("records")
    assert row["total_dispensed"] == 600 and row["total_stocked"] == 180
    assert row["total_rearranged"] == 10
    assert row["days_with_data"] == 5 and row["missing_days"] == 0
    assert row["active_days"] == 3 and row["quiet_days"] == 2
    assert row["avg_dispensed_per_day"] == pytest.approx(120.0)  # 600 / 5
    assert row["avg_dispensed_per_active_day"] == pytest.approx(200.0)  # 600 / 3
    assert row["busiest_day"] == pd.Timestamp("2026-10-01") and row["busiest_day_dispensed"] == 300
    assert row["first_day"] == pd.Timestamp("2026-10-01") and row["branch"] == "Branch A"


def test_weekday_averages_are_hand_checked():
    weekday = analyse().weekday.set_index("weekday")
    assert weekday.loc["Thursday", "avg_dispensed"] == 300
    assert weekday.loc["Friday", "avg_dispensed"] == 200
    assert weekday.loc["Monday", "avg_dispensed"] == 100
    assert (
        weekday.loc["Saturday", "avg_dispensed"] == 0
        and weekday.loc["Sunday", "avg_dispensed"] == 0
    )
    assert list(analyse().weekday["weekday"]) == [
        "Monday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]


def test_daily_table_is_oldest_first_with_a_rolling_seven_day_average():
    daily = analyse().daily
    assert list(daily["activity_date"].dt.day) == [1, 2, 3, 4, 5]
    assert daily["dispensed_7d_avg"].tolist() == pytest.approx([300, 250, 500 / 3, 125, 120])


def test_the_usage_window_limits_the_totals():
    result = analyse(settings=DEFAULT_SETTINGS.with_value("period_days", 3))
    [row] = result.summary.to_dict("records")
    assert row["total_dispensed"] == 100 and row["days_with_data"] == 3  # 3rd, 4th and 5th
    assert result.meta["period_start"] == pd.Timestamp("2026-10-03")
    assert not any("cover only part" in w for w in result.warnings)


def test_as_of_fixes_the_last_day_of_the_window():
    result = analyse(settings=DEFAULT_SETTINGS.with_value("period_days", 2), as_of="2026-10-02")
    assert result.summary.iloc[0]["total_dispensed"] == 500  # 1st and 2nd


def test_a_short_range_warns_that_totals_cover_part_of_the_period():
    assert any("covers 5 day(s) but the period is 90" in w for w in analyse().warnings)


def test_missing_days_are_counted_in_the_summary():
    result = activity.analyse_activity(
        activity.merge_pulls(
            [
                cleaned(
                    HEADER
                    + b"05.10.26;24;Medimat1;1;1;1\n03.10.26;24;Medimat1;1;1;1\n01.10.26;24;Medimat1;1;1;1\n"
                )
            ]
        )[0]
    )
    assert result.summary.iloc[0]["missing_days"] == 2


def test_two_systems_are_summarised_separately():
    data = FIVE_DAYS + b"05.10.26;24;Medimat2;7;7;7\n"
    result = analyse(data)
    assert sorted(result.summary["system_name"]) == ["Medimat1", "Medimat2"]
    assert any("not in the robot registry" in w for w in result.warnings)


def test_missing_counts_are_noted_and_left_out_of_totals():
    data = HEADER + b"01.10.26;24;Medimat1;10;;0\n02.10.26;24;Medimat1;20;5;0\n"
    result = analyse(data)
    assert result.summary.iloc[0]["total_stocked"] == 5
    assert any("missing or unreadable" in w for w in result.warnings)


def test_no_readable_rows_gives_an_empty_result_with_a_warning():
    empty = activity.merge_pulls([])[0]
    result = activity.analyse_activity(empty)
    assert result.summary.empty and result.warnings == ["No readable activity rows"]


def test_invalid_settings_are_refused():
    with pytest.raises(ValueError):
        analyse(settings=DEFAULT_SETTINGS.with_value("period_days", 0))


def test_real_shape_pulls_merge_and_report_the_revised_day():
    loads = [fx("omnicell_activity.csv"), fx("omnicell_activity__pull_2.csv")]
    result = activity.run_activity(loads)
    [row] = result.summary.to_dict("records")
    assert row["days_with_data"] == 33 and row["missing_days"] == 0
    assert row["quiet_days"] == 10 and row["active_days"] == 23  # ten weekend days in 33
    assert any("2 day(s) changed between pulls" in w for w in result.warnings)
    assert len(result.conflicts) == 3


def test_run_activity_skips_stopped_loads_and_returns_none_when_nothing_loaded():
    good = fx("omnicell_activity.csv")
    bad = fx("omnicell_activity__missing_required_column.csv")
    assert activity.run_activity([bad, good]) is not None
    assert activity.run_activity([bad]) is None and activity.run_activity([]) is None


# ---- comparing with the PMR --------------------------------------------------------------------------------


def usage_days(*pairs, basis="reported"):
    rows = [usage_row("Aspirin 75mg Tablets", qty=q, date=d) for d, q in pairs]
    frame = usage_of(*rows)
    frame["qty_basis"] = basis
    return frame


def test_comparison_uses_only_the_days_both_sources_cover():
    daily = analyse().daily  # robot: 1st=300, 2nd=200, 3rd=0, 4th=0, 5th=100
    usage = usage_days(("2026-10-01", 100), ("2026-10-01", 50), ("2026-10-02", 400),
                       ("2026-10-04", 7), ("2026-10-09", 999))  # fmt: skip
    table, summary = activity.compare_with_pmr(daily, usage)
    assert list(table["activity_date"].dt.day) == [1, 2, 4]  # the 9th is outside the robot data
    assert table["robot_dispensed"].tolist() == [300, 200, 0]
    assert table["pmr_total"].tolist() == [150, 400, 7]
    assert table["ratio"].iloc[:2].tolist() == pytest.approx([0.5, 2.0])
    assert np.isnan(table["ratio"].iloc[2])  # robot dispensed nothing: no division by zero
    assert summary["overlap_days"] == 3 and summary["robot_total"] == 500
    assert summary["pmr_total"] == 557 and summary["ratio"] == pytest.approx(557 / 500)
    assert summary["pmr_basis"] == "reported"


def test_comparison_names_the_pmr_basis_when_it_counts_lines():
    daily = analyse().daily
    _, summary = activity.compare_with_pmr(daily, usage_days(("2026-10-01", 1), basis="line_count"))
    assert summary["pmr_basis"] == "line_count"


def test_comparison_ignores_negative_and_missing_pmr_quantities():
    usage = usage_days(("2026-10-01", 100), ("2026-10-01", -5), ("2026-10-01", float("nan")))
    table, _ = activity.compare_with_pmr(analyse().daily, usage)
    assert table["pmr_total"].tolist() == [100]


def test_no_shared_days_gives_an_empty_comparison():
    table, summary = activity.compare_with_pmr(analyse().daily, usage_days(("2027-01-01", 5)))
    assert table.empty and summary["overlap_days"] == 0 and np.isnan(summary["ratio"])
