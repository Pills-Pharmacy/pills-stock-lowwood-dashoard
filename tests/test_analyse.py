from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import analyse
import clean
import load
import match
from config import DEFAULT_SETTINGS
from frames import stock_of, stock_row, usage_of, usage_row

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"
AS_OF = "2026-10-19"  # with 90 days the window is 2026-07-22 to 2026-10-19
IN_WINDOW = "2026-10-10"


def run(stock_rows, usage_rows, settings=DEFAULT_SETTINGS, confirmed=None, as_of=AS_OF):
    result = match.match_items(stock_of(*stock_rows), usage_of(*usage_rows), confirmed=confirmed)
    return analyse.analyse(result, settings, as_of=as_of)


def row_for(frame, name):
    [row] = frame[frame["item_name"] == name].to_dict("records")
    return row


# The main hand-calculated scenario (period 90, over 180, under 14), one branch, one robot.
#   Healthy    stock 600,  450 dispensed -> 5/day,  600/5   = 120 days
#   Overstock  stock 2000, 180 dispensed -> 2/day,  2000/2  = 1000 days
#   Understock stock 10,   900 dispensed -> 10/day, 10/10   = 1 day
#   Dead       stock 50,   rows of 0 dispensed      -> no usage
#   Stale      stock 50,   100 dispensed in January -> outside the window, so no usage
def main_scenario():
    stock = [
        stock_row("Healthy 10mg Tablets", qty=600.0),
        stock_row("Overstock 10mg Tablets", qty=2000.0),
        stock_row("Understock 10mg Tablets", qty=10.0),
        stock_row("Dead 10mg Tablets", qty=50.0),
        stock_row("Stale 10mg Tablets", qty=50.0),
    ]
    usage = [
        usage_row("Healthy 10mg Tablets", qty=200.0, date=IN_WINDOW),
        usage_row("Healthy 10mg Tablets", qty=250.0, date="2026-09-01"),
        usage_row("Overstock 10mg Tablets", qty=180.0, date=IN_WINDOW),
        usage_row("Understock 10mg Tablets", qty=900.0, date=IN_WINDOW),
        usage_row("Dead 10mg Tablets", qty=0.0, date=IN_WINDOW),
        usage_row("Stale 10mg Tablets", qty=100.0, date="2026-01-01"),
    ]
    return stock, usage


def test_hand_calculated_rates_cover_and_classes():
    result = run(*main_scenario())
    items = result.items
    expected = {
        "Healthy 10mg Tablets": (450, 5.0, 120.0, analyse.HEALTHY),
        "Overstock 10mg Tablets": (180, 2.0, 1000.0, analyse.OVER),
        "Understock 10mg Tablets": (900, 10.0, 1.0, analyse.UNDER),
    }
    for name, (dispensed, rate, cover, label) in expected.items():
        row = row_for(items, name)
        assert row["dispensed_in_period"] == dispensed
        assert row["usage_rate_per_day"] == pytest.approx(rate)
        assert row["days_of_cover"] == pytest.approx(cover)
        assert row["classification"] == label
    assert result.summary["dead_stock"] == 2 and len(items) == 5


def test_zero_usage_is_dead_stock_with_no_division_by_zero():
    result = run(*main_scenario())
    for name in ("Dead 10mg Tablets", "Stale 10mg Tablets"):
        row = row_for(result.items, name)
        assert row["classification"] == analyse.DEAD
        assert row["usage_rate_per_day"] == 0 and row["dispensed_in_period"] == 0
        assert np.isnan(row["days_of_cover"])
    assert not np.isinf(result.items["days_of_cover"].dropna()).any()


def test_usage_outside_the_window_is_not_counted():
    result = run(*main_scenario())
    healthy = row_for(result.items, "Healthy 10mg Tablets")
    assert healthy["dispensed_in_period"] == 450  # the 2026-09-01 row is inside, January is not
    assert result.meta["period_start"] == pd.Timestamp("2026-07-22")
    assert result.meta["period_end"] == pd.Timestamp("2026-10-19")


def test_limits_are_strict_so_a_value_on_the_limit_is_healthy():
    stock = [
        stock_row("Edge Over 10mg Tablets", qty=1260.0),  # 630 over 90 days = 7/day -> 180 days
        stock_row("Just Over 10mg Tablets", qty=1261.0),
        stock_row("Edge Under 10mg Tablets", qty=98.0),  # 98 / 7 = 14 days
        stock_row("Just Under 10mg Tablets", qty=97.0),
    ]
    usage = [
        usage_row(f"{n} 10mg Tablets", qty=630.0, date=IN_WINDOW)
        for n in ("Edge Over", "Just Over", "Edge Under", "Just Under")
    ]
    items = run(stock, usage).items
    labels = {r["item_name"]: r["classification"] for r in items.to_dict("records")}
    assert labels == {
        "Edge Over 10mg Tablets": analyse.HEALTHY,
        "Just Over 10mg Tablets": analyse.OVER,
        "Edge Under 10mg Tablets": analyse.HEALTHY,
        "Just Under 10mg Tablets": analyse.UNDER,
    }


# ---- thresholds come from settings and change the result -----------------------------------------


def test_changing_the_overstock_limit_changes_the_class():
    base = run(*main_scenario())
    assert row_for(base.items, "Healthy 10mg Tablets")["classification"] == analyse.HEALTHY
    stricter = DEFAULT_SETTINGS.with_value("overstock_days", 100)
    changed = run(*main_scenario(), settings=stricter)
    assert row_for(changed.items, "Healthy 10mg Tablets")["classification"] == analyse.OVER
    assert changed.meta["settings"]["overstock_days"] == 100


def test_changing_the_understock_limit_changes_the_class():
    settings = DEFAULT_SETTINGS.with_value("understock_days", 130)
    changed = run(*main_scenario(), settings=settings)
    assert row_for(changed.items, "Healthy 10mg Tablets")["classification"] == analyse.UNDER


def test_changing_the_period_changes_rate_cover_and_class():
    stock = [stock_row("Flip 10mg Tablets", qty=100.0)]
    usage = [usage_row("Flip 10mg Tablets", qty=450.0, date=IN_WINDOW)]
    base = row_for(run(stock, usage).items, "Flip 10mg Tablets")
    assert base["usage_rate_per_day"] == pytest.approx(5.0)  # 450 / 90
    assert base["days_of_cover"] == pytest.approx(20.0) and base["classification"] == "healthy"
    short = row_for(
        run(stock, usage, DEFAULT_SETTINGS.with_value("period_days", 30)).items, "Flip 10mg Tablets"
    )
    assert short["usage_rate_per_day"] == pytest.approx(15.0)  # 450 / 30
    assert short["days_of_cover"] == pytest.approx(100 / 15)
    assert short["classification"] == analyse.UNDER


def test_invalid_settings_are_refused():
    bad = DEFAULT_SETTINGS.with_value("understock_days", 400)
    with pytest.raises(ValueError, match="understock_days"):
        run(*main_scenario(), settings=bad)


def test_settings_and_their_labels_are_recorded_for_reproducing_a_report():
    meta = run(*main_scenario()).meta
    assert meta["settings"] == DEFAULT_SETTINGS.values()
    assert set(meta["setting_labels"].values()) == {"Assumption, to be confirmed"}
    assert meta["match_rate"] == 1.0 and meta["usage_basis"] == ["reported"]


# ---- awkward quantities and unmatched rows ----------------------------------------------------


def test_stock_with_no_usage_row_in_pmr_waits_for_review_instead_of_being_called_dead():
    stock = [stock_row("Healthy 10mg Tablets", qty=600.0), stock_row("Mystery 1mg Tablets")]
    usage = [usage_row("Healthy 10mg Tablets", qty=450.0, date=IN_WINDOW)]
    result = run(stock, usage)
    assert list(result.items["item_name"]) == ["Healthy 10mg Tablets"]
    assert list(result.awaiting_review["item_name"]) == ["Mystery 1mg Tablets"]
    assert result.summary["awaiting_review"] == 1


def test_a_confirmed_no_match_makes_the_item_dead_stock():
    stock = [stock_row("Mystery 1mg Tablets", qty=40.0)]
    usage = [usage_row("Healthy 10mg Tablets", qty=450.0, date=IN_WINDOW)]
    key = match.match_items(stock_of(*stock), usage_of(*usage)).matches.loc[0, "stock_key"]
    confirmed = match.confirm(match.empty_confirmed(), key, "", decision="no_match")
    result = run(stock, usage, confirmed=confirmed)
    assert row_for(result.items, "Mystery 1mg Tablets")["classification"] == analyse.DEAD
    assert result.summary["awaiting_review"] == 0


def test_usage_with_no_stock_is_listed_separately():
    stock = [stock_row("Healthy 10mg Tablets", qty=600.0)]
    usage = [
        usage_row("Healthy 10mg Tablets", qty=450.0, date=IN_WINDOW),
        usage_row("Orphan 5mg Tablets", qty=40.0, date=IN_WINDOW),
        usage_row("Orphan 5mg Tablets", qty=0.0, date=IN_WINDOW),
    ]
    result = run(stock, usage)
    [orphan] = result.usage_no_stock.to_dict("records")
    assert orphan["item_name"] == "Orphan 5mg Tablets" and orphan["dispensed_in_period"] == 40
    assert orphan["usage_rate_per_day"] == pytest.approx(40 / 90)
    assert orphan["dispensing_lines"] == 2
    assert result.summary["usage_no_stock"] == 1


def test_zero_stock_with_usage_is_understocked_with_zero_cover():
    result = run(
        [stock_row("Empty 10mg Tablets", qty=0.0)],
        [usage_row("Empty 10mg Tablets", qty=90.0, date=IN_WINDOW)],
    )
    row = row_for(result.items, "Empty 10mg Tablets")
    assert row["classification"] == analyse.UNDER and row["days_of_cover"] == 0


def test_zero_stock_and_zero_usage_is_unclassified():
    result = run(
        [stock_row("Nothing 10mg Tablets", qty=0.0)],
        [usage_row("Nothing 10mg Tablets", qty=0.0, date=IN_WINDOW)],
    )
    assert result.items.empty
    assert result.unclassified.iloc[0]["reason"] == "no stock and no usage in the period"


@pytest.mark.parametrize("bad", [-5.0, float("nan")])
def test_negative_or_missing_stock_is_unclassified_not_guessed(bad):
    result = run(
        [stock_row("Odd 10mg Tablets", qty=bad)],
        [usage_row("Odd 10mg Tablets", qty=90.0, date=IN_WINDOW)],
    )
    assert result.items.empty
    assert result.unclassified.iloc[0]["reason"] == "missing or negative stock quantity"
    assert result.summary["unclassified"] == 1


def test_negative_and_missing_usage_rows_are_excluded_and_counted():
    stock, usage = main_scenario()
    usage += [
        usage_row("Healthy 10mg Tablets", qty=-5.0, date=IN_WINDOW),
        usage_row("Healthy 10mg Tablets", qty=float("nan"), date=IN_WINDOW),
    ]
    result = run(stock, usage)
    healthy = row_for(result.items, "Healthy 10mg Tablets")
    assert healthy["dispensed_in_period"] == 450 and healthy["usage_rows_excluded"] == 2
    assert any("left out of the usage totals" in w for w in result.warnings)


def test_short_pmr_coverage_warns():
    result = run(*main_scenario())  # January row makes this wide; build a narrow one instead
    assert not any("understated" in w for w in result.warnings)
    narrow = run(
        [stock_row("Healthy 10mg Tablets", qty=600.0)],
        [usage_row("Healthy 10mg Tablets", qty=45.0, date=IN_WINDOW)],
    )
    assert any("covers 1 day(s) but the period is 90" in w for w in narrow.warnings)


def test_empty_usage_leaves_everything_awaiting_review():
    result = run([stock_row("Healthy 10mg Tablets", qty=600.0)], [])
    assert result.items.empty and len(result.awaiting_review) == 1
    assert result.meta["observed_days"] == 0


def test_empty_stock_gives_empty_tables_and_zero_counts():
    result = run([], [usage_row("Healthy 10mg Tablets", qty=45.0, date=IN_WINDOW)])
    assert result.items.empty and result.summary["dead_stock"] == 0
    assert result.summary["usage_no_stock"] == 1
    assert list(result.items.columns) == list(analyse.ITEM_COLUMNS)


# ---- classification level and branches --------------------------------------------------------


def two_robots_one_branch():
    stock = [
        stock_row("Shared 10mg Tablets", robot="robot_1", qty=1000.0),
        stock_row("Shared 10mg Tablets", robot="robot_2", qty=10.0),
    ]
    usage = [usage_row("Shared 10mg Tablets", qty=450.0, date=IN_WINDOW)]
    return stock, usage


def test_combined_per_branch_sums_robots_into_one_row():
    result = run(*two_robots_one_branch())
    [row] = result.items.to_dict("records")
    assert row["stock_qty"] == 1010 and row["robot_id"] == "robot_1, robot_2"
    assert row["usage_rate_per_day"] == pytest.approx(5.0)
    assert row["days_of_cover"] == pytest.approx(202.0) and row["classification"] == analyse.OVER
    assert row["robots_sharing"] == 2


def test_per_robot_splits_the_branch_usage_and_classifies_each_robot():
    settings = DEFAULT_SETTINGS.with_value("classification_level", "per_robot")
    result = run(*two_robots_one_branch(), settings=settings)
    rows = {r["robot_id"]: r for r in result.items.to_dict("records")}
    assert set(rows) == {"robot_1", "robot_2"}
    assert rows["robot_1"]["dispensed_in_period"] == 225
    assert rows["robot_1"]["days_of_cover"] == pytest.approx(400.0)
    assert rows["robot_1"]["classification"] == analyse.OVER
    assert rows["robot_2"]["days_of_cover"] == pytest.approx(4.0)
    assert rows["robot_2"]["classification"] == analyse.UNDER
    assert any("split equally" in w for w in result.warnings)


def test_the_level_changes_the_result_for_the_same_data():
    combined = run(*two_robots_one_branch())
    per_robot = run(
        *two_robots_one_branch(), DEFAULT_SETTINGS.with_value("classification_level", "per_robot")
    )
    assert len(combined.items) == 1 and len(per_robot.items) == 2
    assert set(per_robot.items["classification"]) != set(combined.items["classification"])


def test_branches_are_kept_apart():
    stock = [
        stock_row("Split 10mg Tablets", robot="robot_1", branch="Branch A", qty=100.0),
        stock_row("Split 10mg Tablets", robot="robot_2", branch="Branch B", qty=200.0),
    ]
    usage = [usage_row("Split 10mg Tablets", qty=90.0, date=IN_WINDOW, branch="Branch A")]
    items = run(stock, usage).items
    by_branch = {r["branch"]: r for r in items.to_dict("records")}
    assert by_branch["Branch A"]["classification"] == analyse.HEALTHY  # 100 / 1 per day
    assert by_branch["Branch B"]["classification"] == analyse.DEAD  # no usage in branch B


def test_a_pmr_without_branches_pools_stock_and_usage_across_branches():
    stock = [
        stock_row("Pooled 10mg Tablets", robot="robot_1", branch="Branch A", qty=100.0),
        stock_row("Pooled 10mg Tablets", robot="robot_2", branch="Branch B", qty=200.0),
    ]
    usage = [usage_row("Pooled 10mg Tablets", qty=90.0, date=IN_WINDOW, branch="Unknown")]
    result = run(stock, usage)
    [row] = result.items.to_dict("records")
    assert row["branch"] == analyse.POOLED_BRANCH and row["stock_qty"] == 300
    assert row["days_of_cover"] == pytest.approx(300.0)
    assert result.meta["pooled_across_branches"] is True
    assert any("pooled across all branches" in w for w in result.warnings)


def test_pmr_rows_without_a_branch_are_left_out_when_other_rows_have_one():
    stock = [stock_row("Mixed 10mg Tablets", qty=100.0)]
    usage = [
        usage_row("Mixed 10mg Tablets", qty=90.0, date=IN_WINDOW, branch="Branch A"),
        usage_row("Mixed 10mg Tablets", qty=900.0, date=IN_WINDOW, branch="Unknown"),
    ]
    result = run(stock, usage)
    assert row_for(result.items, "Mixed 10mg Tablets")["dispensed_in_period"] == 90
    assert any("have no branch and were left out" in w for w in result.warnings)


def test_earliest_expiry_and_match_info_are_carried():
    first = stock_row("Dated 10mg Tablets", robot="robot_1", qty=5.0)
    second = stock_row("Dated 10mg Tablets", robot="robot_2", qty=5.0)
    first["expiry_date"] = pd.Timestamp("2027-05-01")
    second["expiry_date"] = pd.Timestamp("2027-02-01")
    result = run([first, second], [usage_row("Dated 10mg Tablets", qty=9.0, date=IN_WINDOW)])
    row = result.items.iloc[0]
    assert row["earliest_expiry"] == pd.Timestamp("2027-02-01")
    assert row["match_level"] == match.LEVEL_KEY and bool(row["confirmed"]) is False


def test_items_are_ordered_by_branch_then_class():
    result = run(*main_scenario())
    labels = list(result.items["classification"])
    order = [analyse.CLASSES.index(label) for label in labels]
    assert order == sorted(order)


# ---- the whole pipeline on the synthetic fixtures ---------------------------------------------


def pipeline(robot_1="omnicell_robot_1.csv", robot_2="omnicell_robot_2.csv", pmr="pmr_usage.csv",
             settings=DEFAULT_SETTINGS, **kwargs):  # fmt: skip
    results = {
        R1: load.load_source(R1, FIXTURES / robot_1),
        R2: load.load_source(R2, FIXTURES / robot_2),
        PMR: load.load_source(PMR, FIXTURES / pmr),
    }
    stock, usage = clean.clean_all(results)
    return analyse.analyse(match.match_items(stock, usage), settings, **kwargs)


# stock / (dispensed / 90) for the baseline items, from the comments in make_fixtures.py
FIXTURE_COVER = {
    "Paracetamol 500mg Tablets": 120.0,
    "Amoxicillin 500mg Capsules": 40 * 90 / 630,
    "Metformin 500mg Tablets": 2000 * 90 / 840,
    "Atorvastatin 20mg Tablets": 45.0,
    "Omeprazole 20mg Capsules": 300 * 90 / 280,
    "Levothyroxine 100μg Tablets": 4.0,  # cleaning turns the micro sign into Greek mu
    "IBUPROFEN 400MG TABS": 60.0,
    "Amlodipine 5mg Tabs": 3000 * 90 / 280,
    "Furosemide 40mg Tabs": 30.0,
    "Morphine Sulf 10mg/5ml Oral Soln": 30.0,
    "Ceftriaxone 1g Inj": 4.0,
    "Prednisolone 5mg Tabs": 18.0,
}


def test_baseline_fixtures_give_the_hand_calculated_days_of_cover():
    result = pipeline()
    assert len(result.items) == 12 and result.warnings == []
    assert result.meta["period_start"] == pd.Timestamp("2026-07-22")
    for name, cover in FIXTURE_COVER.items():
        assert row_for(result.items, name)["days_of_cover"] == pytest.approx(cover), name
    assert result.summary == {
        "dead_stock": 0,
        "overstocked": 2,
        "understocked": 3,
        "healthy": 7,
        "unclassified": 0,
        "awaiting_review": 0,
        "usage_no_stock": 0,
    }


def test_baseline_fixtures_give_the_same_classes_at_both_levels():
    combined = pipeline().items.set_index("item_name")["classification"]
    per_robot = pipeline(
        settings=DEFAULT_SETTINGS.with_value("classification_level", "per_robot")
    ).items.set_index("item_name")["classification"]
    pd.testing.assert_series_equal(combined.sort_index(), per_robot.sort_index())


def test_zero_usage_fixture_makes_amoxicillin_dead_stock():
    result = pipeline(pmr="pmr_usage__zero_usage_item.csv")
    assert row_for(result.items, "Amoxicillin 500mg Capsules")["classification"] == "dead_stock"
    assert result.summary["dead_stock"] == 1


def test_robot_item_missing_from_pmr_waits_for_review_and_pmr_only_item_is_listed():
    result = pipeline(robot_1="omnicell_robot_1__item_not_in_pmr.csv")
    assert list(result.awaiting_review["item_name"]) == ["Zzz Placeholder Drug 10mg Tablets"]
    result = pipeline(pmr="pmr_usage__item_not_in_robots.csv")
    assert list(result.usage_no_stock["item_name"]) == ["Warfarin 3mg Tablets"]
    assert result.usage_no_stock.iloc[0]["dispensed_in_period"] == 280


def test_item_in_both_robots_is_dead_in_the_branch_with_no_usage():
    result = pipeline(robot_2="omnicell_robot_2__item_in_both_robots.csv")
    levo = result.items[result.items["name_norm"] == "levothyroxine tablets"]
    by_branch = {r["branch"]: r for r in levo.to_dict("records")}
    assert by_branch["Branch A"]["classification"] == analyse.UNDER
    assert by_branch["Branch B"]["classification"] == analyse.DEAD
    assert by_branch["Branch B"]["stock_qty"] == 30


def test_short_pmr_range_and_negative_pmr_quantity_are_reported():
    short = pipeline(pmr="pmr_usage__short_range.csv")
    assert any("covers" in w and "understated" in w for w in short.warnings)
    negative = pipeline(pmr="pmr_usage__negative_qty.csv")
    assert any("left out of the usage totals" in w for w in negative.warnings)
    assert negative.items["usage_rows_excluded"].sum() == 1


def test_shared_code_fixture_gives_the_same_answers_as_the_baseline():
    base = pipeline().items.set_index("item_name")["days_of_cover"].sort_index()
    shared = pipeline(pmr="pmr_usage__shared_codes.csv")
    pd.testing.assert_series_equal(
        shared.items.set_index("item_name")["days_of_cover"].sort_index(), base
    )


def test_no_patient_columns_reach_the_results():
    result = pipeline()
    for table in (result.items, result.unclassified, result.awaiting_review, result.usage_no_stock):
        assert not any("patient" in c.lower() for c in table.columns)
