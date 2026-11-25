from pathlib import Path

import pandas as pd
import pytest

import export
import match
import pipeline
from config import DEFAULT_SETTINGS, load_column_map

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"


def files(robot_1="omnicell_robot_1.csv", robot_2="omnicell_robot_2.csv", pmr="pmr_usage.csv"):
    chosen = {R1: robot_1, R2: robot_2, PMR: pmr}
    return {source: (FIXTURES / name).read_bytes() for source, name in chosen.items() if name}


# ---- the full chain ---------------------------------------------------------------------------


def test_baseline_fixtures_run_end_to_end():
    result = pipeline.run_pipeline(files(), generated_at="2026-10-29 12:00")
    assert result.ran and result.errors == []
    assert result.matching.match_rate == 1.0
    assert result.report.counts["overstocked"] == 2 and result.report.counts["understocked"] == 3
    assert result.report.counts["healthy"] == 7


def test_issues_gather_every_load_message_and_the_cross_file_check():
    result = pipeline.run_pipeline(files())
    checks = {issue.check for issue in result.issues}
    assert {"encoding", "patient_fields", "same_headers"} <= checks


def test_one_robot_and_the_pmr_is_enough():
    result = pipeline.run_pipeline(files(robot_2=None))
    assert result.ran
    assert result.matching.match_rate == 1.0  # the six robot-2 products are simply not stocked
    assert len(result.analysis.usage_no_stock) == 6


def test_changing_criteria_changes_the_result():
    base = pipeline.run_pipeline(files())
    changed = pipeline.run_pipeline(
        files(), settings=DEFAULT_SETTINGS.with_value("overstock_days", 100)
    )
    assert changed.report.counts["overstocked"] > base.report.counts["overstocked"]


def test_unmatched_items_reach_the_review_list():
    result = pipeline.run_pipeline(files(robot_1="omnicell_robot_1__item_not_in_pmr.csv"))
    assert result.report.counts["review"] == 1
    assert result.matching.match_rate == pytest.approx(12 / 13)


def test_confirmed_decisions_and_code_matching_are_passed_through():
    data = files(robot_1="omnicell_robot_1__item_not_in_pmr.csv")
    first = pipeline.run_pipeline(data)
    key = first.matching.review.query("side == 'robot'").iloc[0]["stock_key"]
    confirmed = match.confirm(match.empty_confirmed(), key, "", decision="no_match")
    second = pipeline.run_pipeline(data, confirmed=confirmed)
    assert second.report.counts["review"] == 0 and second.report.counts["dead_stock"] == 1
    shared = pipeline.run_pipeline(files(pmr="pmr_usage__shared_codes.csv"), use_code_match=True)
    assert shared.matching.level_counts == {1: 12}


def test_default_branch_is_used_when_the_pmr_has_none():
    data = b"Dispensed Date,Script Dispensed As\n10/1/2026 9:29,ATORVASTATIN 20MG TABLETS\n"
    robot = b"Item Description,Qty On Hand\nAtorvastatin 20mg Tablets,100\n"
    result = pipeline.run_pipeline({R1: robot, PMR: data}, default_branch="Branch A")
    assert result.ran and set(result.matching.usage["branch"]) == {"Branch A"}
    pooled = pipeline.run_pipeline({R1: robot, PMR: data})
    assert len(pooled.analysis.items) == 1
    assert pooled.analysis.meta["pooled_across_branches"] is True


# ---- problems are returned, not raised ----------------------------------------------------------


def test_missing_files_are_explained():
    only_robot = pipeline.run_pipeline(files(pmr=None))
    assert not only_robot.ran and any("PMR usage file" in e for e in only_robot.errors)
    nothing = pipeline.run_pipeline({})
    assert len(nothing.errors) == 2


def test_a_stopped_load_is_reported_and_blocks_the_run_when_it_was_the_only_one():
    result = pipeline.run_pipeline(
        files(robot_1="omnicell_robot_1__missing_required_column.csv", robot_2=None)
    )
    assert not result.ran
    assert any(i.check == "required_columns" for i in result.issues)


def test_a_stopped_robot_is_skipped_when_another_robot_loaded():
    result = pipeline.run_pipeline(files(robot_1="omnicell_robot_1__missing_required_column.csv"))
    assert result.ran and any(i.check == "required_columns" for i in result.issues)
    assert set(result.matching.matches["robot_id"]) == {"robot_2"}


def test_file_in_the_wrong_slot_is_rejected_with_a_clear_message():
    activity = b"DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED\n01.01.26;24;S;1;2;3\n"
    result = pipeline.run_pipeline({**files(), R1: activity})
    assert any(i.check == "wrong_file_type" for i in result.issues)
    assert result.ran  # robot 2 and the PMR are fine


def test_invalid_criteria_are_returned_as_errors():
    bad = DEFAULT_SETTINGS.with_value("understock_days", 999)
    result = pipeline.run_pipeline(files(), settings=bad)
    assert not result.ran and "understock_days" in result.errors[0]


def test_an_unexpected_failure_is_reported_without_row_values(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret row value 12345")

    monkeypatch.setattr(pipeline.match, "match_items", boom)
    result = pipeline.run_pipeline(files())
    assert not result.ran
    assert result.errors == ["The analysis failed unexpectedly (RuntimeError)."]
    assert "12345" not in " ".join(result.errors)


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pipeline.run_pipeline(files())
    assert list(tmp_path.iterdir()) == []


def test_no_patient_data_in_any_pipeline_output():
    result = pipeline.run_pipeline(files())
    for frame in (
        result.matching.matches,
        result.matching.usage,
        result.analysis.items,
        *result.report.tables.values(),
    ):
        assert not any("patient" in c.lower() for c in frame.columns)
        assert "PT-FAKE" not in frame.to_csv()


# ---- the optional column-mapping screen ----------------------------------------


def test_override_makes_an_unrecognised_file_usable():
    robot = b"Thing,Amount\nAtorvastatin 20mg Tablets,100\n"
    pmr = b"Dispensed Date,Script Dispensed As\n10/1/2026 9:29,ATORVASTATIN 20MG TABLETS\n"
    plain = pipeline.run_pipeline({R1: robot, PMR: pmr})
    assert not plain.ran and any(i.check == "required_columns" for i in plain.issues)
    mapped = pipeline.run_pipeline(
        {R1: robot, PMR: pmr},
        overrides={R1: {"item_name": "Thing", "stock_qty": "Amount"}},
        default_branch="Branch A",
    )
    assert mapped.ran and mapped.matching.match_rate == 1.0


def test_an_override_wins_over_an_existing_mapping():
    merged = pipeline.merge_column_map(load_column_map(), {R1: {"item_code": "Item Description"}})
    block = merged[R1]
    assert block["item_code"][0] == "Item Description"
    assert "Item Description" not in block["item_name"]
    assert "Item Description" in load_column_map()[R1]["item_name"]  # original untouched


def test_file_headers_hide_patient_columns_and_survive_bad_files():
    headers = pipeline.file_headers((FIXTURES / "pmr_usage.csv").read_bytes())
    assert "Patient ID" not in headers and "product_name" in headers
    assert pipeline.file_headers(b"") == [] and pipeline.file_headers(b"\x81") == []


def test_mapping_status_shows_the_current_automatic_choice():
    status = pipeline.mapping_status(R1, (FIXTURES / "omnicell_robot_1.csv").read_bytes())
    assert status["stock_qty"].strip() == "Qty On Hand"
    assert status["item_name"].strip() == "Item Description"
    assert status["unit"] == "Unit"
    pmr = pipeline.mapping_status(PMR, (FIXTURES / "pmr_usage.csv").read_bytes())
    assert pmr["dispense_date"] == "date_dispensed" and pmr["generic_name"] == ""


# ---- branch helpers -----------------------------------------------------------------------------


def test_branch_match_rate_for_one_branch_and_for_all():
    result = pipeline.run_pipeline(files(robot_1="omnicell_robot_1__item_not_in_pmr.csv"))
    assert pipeline.branch_match_rate(result.matching, None) == (pytest.approx(12 / 13), 12, 13)
    assert pipeline.branch_match_rate(result.matching, "Branch A") == (pytest.approx(6 / 7), 6, 7)
    assert pipeline.branch_match_rate(result.matching, "Branch B") == (1.0, 6, 6)
    assert pipeline.branch_match_rate(result.matching, "Nowhere") == (0.0, 0, 0)


def test_branch_view_filters_rows_and_keeps_branchless_ones():
    result = pipeline.run_pipeline(files(pmr="pmr_usage__item_not_in_robots.csv"))
    tables = result.report.tables
    everything = export.branch_view(tables, None)
    assert everything["healthy"].equals(tables["healthy"])
    view = export.branch_view(tables, "Branch A")
    assert set(view["healthy"]["Branch"]) == {"Branch A"}
    assert len(view["healthy"]) + len(export.branch_view(tables, "Branch B")["healthy"]) == len(
        tables["healthy"]
    )
    assert "" in set(view["review"]["Branch"])  # the PMR-side row has no branch
    assert isinstance(view["review"], pd.DataFrame)


# ---- stock-count option and the bulk guard ---------------------------------------------------------------


def test_stock_is_packs_option_multiplies_by_pack_size():
    base = pipeline.run_pipeline(files())
    packs = pipeline.run_pipeline(files(), stock_is_packs=True)

    def stock_of_paracetamol(result):
        items = result.analysis.items
        return items.loc[items["item_name"] == "Paracetamol 500mg Tablets", "stock_qty"].iloc[0]

    assert stock_of_paracetamol(packs) == stock_of_paracetamol(base) * 100  # pack size 100
    explicit_off = pipeline.run_pipeline(files(), stock_is_packs=False)
    assert stock_of_paracetamol(explicit_off) == stock_of_paracetamol(base)


def test_bulk_is_allowed_when_the_pmr_covers_the_period_and_blocked_when_it_does_not():
    full = pipeline.run_pipeline(files())
    assert pipeline.bulk_blocked_reason(full.analysis) is None
    short = pipeline.run_pipeline(files(pmr="pmr_usage__short_range.csv"))
    reason = pipeline.bulk_blocked_reason(short.analysis)
    assert "covers" in reason and "90 days" in reason and "switched off" in reason
    relaxed = pipeline.run_pipeline(
        files(pmr="pmr_usage__short_range.csv"),
        settings=DEFAULT_SETTINGS.with_value("period_days", 10),
    )
    assert pipeline.bulk_blocked_reason(relaxed.analysis) is None


# ---- robot activity statistics -------------------------------------------------------------------------------

ACTIVITY = (FIXTURES / "omnicell_activity.csv").read_bytes()
PULL_2 = (FIXTURES / "omnicell_activity__pull_2.csv").read_bytes()


def test_activity_alone_runs_without_stock_or_pmr_and_without_errors():
    result = pipeline.run_pipeline({}, activity_files=[ACTIVITY, PULL_2])
    assert result.activity_ran and not result.ran
    assert result.errors == [] and result.activity_errors == []
    assert len(result.activity.table) == 33 and len(result.activity_loads) == 2


def test_activity_runs_alongside_the_stock_audit_and_is_compared_with_the_pmr():
    result = pipeline.run_pipeline(files(), activity_files=[ACTIVITY, PULL_2])
    assert result.ran and result.activity_ran and result.errors == []
    table, summary = result.comparison
    assert summary["overlap_days"] > 20 and summary["pmr_basis"] == "reported"
    assert set(table.columns) == {"activity_date", "robot_dispensed", "pmr_total", "ratio"}
    assert "activity_summary" in result.report.tables and "activity_pmr" in result.report.tables


def test_without_activity_files_nothing_about_activity_appears():
    result = pipeline.run_pipeline(files())
    assert not result.activity_ran and result.comparison is None
    assert not any(key.startswith("activity") for key in result.report.tables)


def test_missing_stock_files_do_not_hide_the_activity_results():
    result = pipeline.run_pipeline(files(pmr=None), activity_files=[ACTIVITY])
    assert not result.ran and any("PMR usage file" in e for e in result.errors)
    assert result.activity_ran


def test_a_bad_activity_file_does_not_stop_the_stock_audit():
    stock_listing = (FIXTURES / "omnicell_robot_1__bestand_format.csv").read_bytes()
    result = pipeline.run_pipeline(files(), activity_files=[stock_listing])
    assert result.ran and not result.activity_ran
    assert result.activity_errors == [
        "No robot activity file could be read. Upload one and fix any errors."
    ]
    assert any(
        i.check == "wrong_file_type" and i.source == "omnicell_activity" for i in result.issues
    )


def test_many_pulls_report_coverage_once_not_once_per_pull():
    result = pipeline.run_pipeline({}, activity_files=[ACTIVITY] * 5 + [PULL_2])
    assert not any(i.check == "activity_range" for i in result.issues)
    assert len([i for i in result.issues if i.check == "date_format"]) == 1
    assert sum("covers" in w and "period" in w for w in result.activity.warnings) == 1


def test_an_activity_failure_is_contained(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("row value 777")

    monkeypatch.setattr(pipeline.activity, "run_activity", boom)
    result = pipeline.run_pipeline(files(), activity_files=[ACTIVITY])
    assert result.ran and not result.activity_ran
    assert result.activity_errors == ["The activity analysis failed unexpectedly (RuntimeError)."]
    assert "777" not in " ".join(result.activity_errors)


def test_the_mapping_override_works_for_activity_files_too():
    data = b"Day;Count\n01.10.26;5\n02.10.26;6\n"
    plain = pipeline.run_pipeline({}, activity_files=[data])
    assert not plain.activity_ran
    mapped = pipeline.run_pipeline(
        {},
        activity_files=[data],
        overrides={"omnicell_activity": {"activity_date": "Day", "dispensed": "Count"}},
    )
    assert mapped.activity_ran and mapped.activity.summary.iloc[0]["total_dispensed"] == 11


def test_activity_results_hold_no_patient_columns():
    result = pipeline.run_pipeline(files(), activity_files=[ACTIVITY])
    for frame in (result.activity.table, result.activity.summary, result.activity.daily):
        assert not any("patient" in c.lower() for c in frame.columns)


def test_nothing_is_written_to_disk_for_activity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pipeline.run_pipeline(files(), activity_files=[ACTIVITY, PULL_2])
    assert list(tmp_path.iterdir()) == []


# ---- usage analysis without a stock file -------------------------------------------------------------------------


ROBOT_FILE = (FIXTURES / "omnicell_robot_1.csv").read_bytes()


def grouped_only(**kwargs):
    pmr = (FIXTURES / "pmr_usage__grouped_report.csv").read_bytes()
    return pipeline.run_pipeline({PMR: pmr}, pharmacy="lowood", **kwargs)


def test_a_pmr_without_any_stock_file_gives_a_usage_analysis_and_no_error():
    result = grouped_only()
    assert result.errors == [] and result.usage_ran and result.usage_only
    assert not result.ran and result.matching is None and result.analysis is None
    assert result.report is not None and result.report.has_stock is False
    assert result.usage.meta["scripts"] == 72 and result.usage.meta["products"] == 12


def test_the_usage_only_report_has_usage_tables_and_no_stock_classes():
    tables = grouped_only().report.tables
    assert list(tables) == [
        "usage_products",
        "usage_stopped",
        "usage_monthly",
        "usage_robot",
        "suggest_abc",
        "suggest_add",
        "suggest_remove",
        "suggest_increase",
        "suggest_swaps",
    ]
    summary = dict(zip(*grouped_only().report.summary.to_dict("list").values()))
    assert summary["Stock"] == "Not classified: no stock file was uploaded"
    assert summary["Pharmacy"] == "Lowood" and "Match rate" not in summary


def test_a_pmr_with_an_unreadable_stock_file_is_still_an_error():
    broken = (FIXTURES / "omnicell_robot_1__missing_required_column.csv").read_bytes()
    result = pipeline.run_pipeline(
        {R1: broken, PMR: (FIXTURES / "pmr_usage.csv").read_bytes()}, pharmacy="lowood"
    )
    assert not result.ran and not result.usage_only
    assert any("No robot stock file could be read" in e for e in result.errors)


def test_stock_without_a_pmr_is_still_an_error_and_has_no_usage():
    result = pipeline.run_pipeline({R1: ROBOT_FILE}, pharmacy="lowood")
    assert any("No PMR usage file could be read" in e for e in result.errors)
    assert not result.usage_ran


def test_the_full_audit_also_carries_the_usage_analysis():
    result = pipeline.run_pipeline(
        {R1: ROBOT_FILE, PMR: (FIXTURES / "pmr_usage__grouped_report.csv").read_bytes()},
        pharmacy="lowood",
    )
    assert result.ran and result.usage_ran and not result.usage_only
    assert {"usage_products", "usage_stopped", "usage_monthly"} <= set(result.report.tables)
    assert result.report.has_stock is True


def test_usage_follows_the_same_window_as_the_stock_analysis():
    result = pipeline.run_pipeline(
        {R1: ROBOT_FILE, PMR: (FIXTURES / "pmr_usage__grouped_report.csv").read_bytes()},
        pharmacy="lowood",
    )
    assert result.usage.meta["period_start"] == result.analysis.meta["period_start"]
    assert result.usage.meta["rate_days"] == result.analysis.meta["rate_days"] == 92


def test_the_latest_month_option_reaches_the_usage_analysis():
    default = grouped_only().usage.meta
    latest = grouped_only(include_latest_month=True).usage.meta
    assert default["period_end"].month == 9 and latest["period_end"].month == 10


def test_usage_only_with_activity_compares_per_weekday_and_keeps_the_activity_view():
    sept = "\n".join(
        ["DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED"]
        + [f"{d:02d}.09.26;24;Medimat1;200;0;0" for d in range(30, 0, -1)]
    )
    result = grouped_only(activity_files=[sept.encode()])
    assert result.usage_only and result.activity_ran
    assert len(result.usage.comparison) == 1
    assert result.usage.comparison.iloc[0]["robot_per_weekday"] == 200
    assert {"activity_summary", "usage_robot"} <= set(result.report.tables)


def test_usage_only_settings_are_recorded_for_reproducing_the_report():
    settings = grouped_only().report.settings.set_index("Setting")
    assert settings.loc["period_days", "Status"] == "Assumption, to be confirmed"
    assert settings.loc["usage_date_basis", "Value"] == "month"
    assert settings.loc["usage_window_start", "Value"] == "2026-07-01"
    assert settings.loc["trend_min_scripts", "Value"] == 5


def test_a_usage_failure_is_contained(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret 4242")

    monkeypatch.setattr(pipeline.usage_analysis, "analyse_usage", boom)
    result = grouped_only()
    assert not result.usage_ran and not result.report
    assert result.errors == ["The usage analysis failed unexpectedly (RuntimeError)."]
    assert "4242" not in " ".join(result.errors)


def test_no_patient_data_in_the_usage_outputs():
    result = grouped_only()
    for frame in (result.usage.products, result.usage.stopped, result.usage.monthly):
        assert not any("patient" in c.lower() for c in frame.columns)
    text = " ".join(f.to_csv() for f in result.report.tables.values()) + export.to_html(
        result.report
    )
    assert "PT-FAKE" not in text
