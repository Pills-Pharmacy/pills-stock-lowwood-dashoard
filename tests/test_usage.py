import numpy as np
import pandas as pd
import pytest

import usage
from config import DEFAULT_SETTINGS
from frames import usage_of, usage_row

MONTHLY = DEFAULT_SETTINGS  # 90 days = 3 whole months


def rows(name, *months_and_quantities, basis="month"):
    """One script per (date, quantity) pair. Month data is dated the 1st of the month."""
    out = []
    for date, qty in months_and_quantities:
        row = usage_row(name, qty=qty, date=date)
        row["date_basis"] = basis
        out.append(row)
    return out


def table(*groups):
    return usage_of(*[row for group in groups for row in group])


def two_scripts(name, quantities_by_month):
    """Two equal scripts a month (so quantity q a month is two scripts of q/2)."""
    pairs = []
    for date, qty in quantities_by_month.items():
        pairs += [(date, qty / 2), (date, qty / 2)]
    return rows(name, *pairs)


# Window for 90 days with the latest month (Oct) left out: Jul, Aug, Sep (92 days).
# Previous window: Apr, May, Jun.
def scenario():
    return table(
        # Alpha: 100 a month before, 150 a month in the window -> +50%, 6 scripts: rising
        two_scripts(
            "Alpha 10mg Tablets",
            {
                "2026-04-01": 100,
                "2026-05-01": 100,
                "2026-06-01": 100,
                "2026-07-01": 150,
                "2026-08-01": 150,
                "2026-09-01": 150,
                "2026-10-01": 7,
            },
        ),
        # Beta: only 4 scripts in the window: too few to call a trend
        rows(
            "Beta 5mg Tablets",
            ("2026-07-01", 10),
            ("2026-07-01", 10),
            ("2026-08-01", 10),
            ("2026-09-01", 10),
            ("2026-05-01", 500),
        ),
        # Gamma: 290 before, 300 in the window, 6 scripts: steady
        two_scripts(
            "Gamma 20mg Tablets",
            {
                "2026-04-01": 100,
                "2026-05-01": 100,
                "2026-06-01": 90,
                "2026-07-01": 100,
                "2026-08-01": 100,
                "2026-09-01": 100,
            },
        ),
        # Delta: 1000 before, 600 in the window: -40%, 6 scripts: falling
        two_scripts(
            "Delta 1mg Tablets",
            {
                "2026-04-01": 400,
                "2026-05-01": 300,
                "2026-06-01": 300,
                "2026-07-01": 200,
                "2026-08-01": 200,
                "2026-09-01": 200,
            },
        ),
        # Echo: nothing before, 120 in the window, 6 scripts: new
        two_scripts("Echo 2mg Tablets", {"2026-07-01": 40, "2026-08-01": 40, "2026-09-01": 40}),
        # Foxtrot: dispensed Jan to Jun, nothing in the window: stopped
        rows("Foxtrot 3mg Tablets", ("2026-01-01", 30), ("2026-01-01", 30), ("2026-04-01", 30)),
        # Golf: first dispensed in October, the month left out
        rows("Golf 4mg Tablets", ("2026-10-01", 5)),
    )


def run(data=None, **kwargs):
    return usage.analyse_usage(scenario() if data is None else data, **kwargs)


def product(result, name):
    found = result.products[result.products["product"] == name]
    assert len(found) == 1, name
    return found.iloc[0]


# ---- the window and the monthly table ---------------------------------------------------------------


def test_the_window_is_three_whole_months_with_the_latest_month_left_out():
    result = run()
    meta = result.meta
    assert meta["date_basis"] == "month" and meta["rate_days"] == 92
    assert meta["period_start"] == pd.Timestamp("2026-07-01")
    assert meta["period_end"] == pd.Timestamp("2026-09-30")
    assert any("latest month (Oct 2026) was left out" in w for w in result.warnings)


def test_including_the_latest_month_moves_the_window():
    result = run(include_latest_month=True)
    assert result.meta["period_start"] == pd.Timestamp("2026-08-01")
    assert result.meta["period_end"] == pd.Timestamp("2026-10-31")
    assert not any("left out" in w for w in result.warnings)


def test_the_monthly_table_covers_every_month_including_the_one_left_out():
    monthly = run().monthly
    assert list(monthly["month"].dt.strftime("%Y-%m")) == [
        "2026-01", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
    ]  # fmt: skip
    july = monthly[monthly["month"] == pd.Timestamp("2026-07-01")].iloc[0]
    # Alpha 2 scripts, Beta 2, Gamma 2, Delta 2, Echo 2 = 10 scripts; 150+20+100+200+40 = 510
    assert july["scripts"] == 10 and july["quantity"] == 510 and july["products"] == 5
    october = monthly[monthly["month"] == pd.Timestamp("2026-10-01")].iloc[0]
    assert october["scripts"] == 3 and october["products"] == 2  # Alpha twice, Golf once


# ---- products, ranking and trends ---------------------------------------------------------------------


def test_products_are_ranked_by_scripts_then_quantity_because_units_differ():
    products = run().products
    # four products have 6 scripts, so quantity (600, 450, 300, 120) breaks the tie; Beta has 4
    assert list(products["product"]) == [
        "Delta 1mg Tablets",
        "Alpha 10mg Tablets",
        "Gamma 20mg Tablets",
        "Echo 2mg Tablets",
        "Beta 5mg Tablets",
    ]  # Foxtrot stopped and Golf is only in the month left out


def test_window_totals_rates_and_shares_are_hand_checked():
    result = run()
    alpha = product(result, "Alpha 10mg Tablets")
    assert alpha["scripts"] == 6 and alpha["quantity"] == 450
    assert alpha["rate_per_day"] == pytest.approx(450 / 92)
    total_scripts = 6 + 4 + 6 + 6 + 6
    assert alpha["share_of_scripts"] == pytest.approx(6 / total_scripts)
    assert (
        result.meta["scripts"] == total_scripts
        and result.meta["quantity"] == 450 + 40 + 300 + 600 + 120
    )
    assert result.meta["products"] == 5


@pytest.mark.parametrize(
    "name, previous, change, trend",
    [
        ("Alpha 10mg Tablets", 300, 0.5, usage.RISING),
        ("Gamma 20mg Tablets", 290, 300 / 290 - 1, usage.STEADY),
        ("Delta 1mg Tablets", 1000, -0.4, usage.FALLING),
        ("Echo 2mg Tablets", 0, None, usage.NEW),
        ("Beta 5mg Tablets", 500, 40 / 500 - 1, usage.FEW),
    ],
)
def test_trends_compare_with_the_window_of_the_same_length_before(name, previous, change, trend):
    row = product(run(), name)
    assert row["previous_quantity"] == previous and row["trend"] == trend
    if change is None:
        assert np.isnan(row["change"])
    else:
        assert row["change"] == pytest.approx(change)


def test_the_trend_threshold_and_script_minimum_come_from_config(monkeypatch):
    monkeypatch.setattr(usage, "TREND_THRESHOLD", 0.6)  # +50% is no longer a rise
    assert product(run(), "Alpha 10mg Tablets")["trend"] == usage.STEADY
    monkeypatch.setattr(usage, "TREND_MIN_SCRIPTS", 7)  # six scripts is now too few
    assert set(run().products["trend"]) == {usage.FEW}


def test_last_dispensed_is_the_latest_month_the_product_ever_appears():
    alpha = product(run(), "Alpha 10mg Tablets")
    assert alpha["last_dispensed"] == pd.Timestamp("2026-10-01")  # even though Oct is left out


# ---- products that stopped ----------------------------------------------------------------------------


def test_a_product_dispensed_earlier_but_not_in_the_window_is_listed_as_stopped():
    stopped = run().stopped
    assert list(stopped["product"]) == ["Foxtrot 3mg Tablets"]
    row = stopped.iloc[0]
    assert row["last_dispensed"] == pd.Timestamp("2026-04-01")
    assert row["months_since"] == 5  # April to the end of the window (September)
    assert row["scripts_before"] == 3 and row["quantity_before"] == 90


def test_stopped_products_are_sorted_by_how_regularly_they_were_dispensed():
    data = table(
        rows("Few 1mg Tablets", ("2026-02-01", 1)),
        rows("Many 1mg Tablets", ("2026-01-01", 1), ("2026-02-01", 1), ("2026-03-01", 1)),
        rows("Late 1mg Tablets", ("2026-06-01", 1), ("2026-05-01", 1)),
        rows("Now 1mg Tablets", ("2026-08-01", 1), ("2026-10-01", 1)),
    )
    stopped = run(data).stopped
    assert list(stopped["product"]) == ["Many 1mg Tablets", "Late 1mg Tablets", "Few 1mg Tablets"]


def test_a_product_with_only_zero_quantities_in_the_window_counts_as_stopped():
    data = table(
        rows("Zero 1mg Tablets", ("2026-03-01", 5), ("2026-08-01", 0)),
        rows("Live 1mg Tablets", ("2026-08-01", 9), ("2026-10-01", 1)),
    )
    result = run(data)
    assert list(result.stopped["product"]) == ["Zero 1mg Tablets"]


# ---- day-based PMR files ------------------------------------------------------------------------------------


def test_a_day_dated_pmr_uses_a_day_window_and_the_window_before_it():
    data = table(
        rows(
            "Alpha 10mg Tablets",
            ("2026-10-10", 90),
            ("2026-09-01", 30),
            ("2026-05-01", 30),
            basis="day",
        ),
        rows("Beta 5mg Tablets", ("2026-03-01", 5), basis="day"),
    )
    result = run(data, as_of="2026-10-19")
    meta = result.meta
    assert meta["date_basis"] == "day" and meta["rate_days"] == 90
    assert meta["period_start"] == pd.Timestamp("2026-07-22")
    alpha = product(result, "Alpha 10mg Tablets")
    assert (
        alpha["quantity"] == 120 and alpha["previous_quantity"] == 30
    )  # 2026-05-01 is 23 Apr to 21 Jul
    assert list(result.stopped["product"]) == ["Beta 5mg Tablets"]


# ---- awkward data -------------------------------------------------------------------------------------------


def test_bad_rows_are_counted_in_a_warning_and_left_out():
    data = table(
        rows(
            "Alpha 10mg Tablets",
            ("2026-08-01", 10),
            ("2026-08-01", -5),
            ("2026-08-01", float("nan")),
        ),
        rows("Beta 5mg Tablets", ("2026-09-01", 1)),
    )
    result = run(data)
    assert product(result, "Alpha 10mg Tablets")["quantity"] == 10
    assert any("2 PMR row(s) with a missing or negative quantity" in w for w in result.warnings)


def test_rows_with_no_product_name_are_noted_and_kept_out_of_the_product_tables():
    data = table(rows("", ("2026-08-01", 3)), rows("Alpha 10mg Tablets", ("2026-08-01", 3)))
    result = run(data)
    assert list(result.products["product"]) == ["Alpha 10mg Tablets"]
    assert any("no product name" in w for w in result.warnings)


def test_an_empty_pmr_gives_empty_tables_not_an_error():
    result = run(usage_of())
    assert result.products.empty and result.stopped.empty and result.monthly.empty
    assert result.meta["products"] == 0


def test_a_single_month_still_gives_products_and_no_previous_window():
    data = table(two_scripts("Alpha 10mg Tablets", {"2026-09-01": 60}))
    result = run(data)
    assert any("single month" in w for w in result.warnings)
    assert product(result, "Alpha 10mg Tablets")["trend"] == usage.FEW
    assert result.stopped.empty


def test_invalid_settings_are_refused():
    with pytest.raises(ValueError):
        run(settings=DEFAULT_SETTINGS.with_value("understock_days", 999))


def test_the_period_setting_changes_the_window():
    one_month = run(settings=DEFAULT_SETTINGS.with_value("period_days", 30))
    assert one_month.meta["period_start"] == pd.Timestamp("2026-09-01")
    assert one_month.meta["rate_days"] == 30
    assert product(one_month, "Alpha 10mg Tablets")["quantity"] == 150


def test_quantity_basis_is_recorded():
    assert run().meta["qty_basis"] == ["reported"]


# ---- robot against PMR, per weekday ---------------------------------------------------------------------------


def robot_days(days: list[str], dispensed: float = 200.0) -> pd.DataFrame:
    return pd.DataFrame(
        {"activity_date": pd.to_datetime(days), "dispensed": dispensed, "system_name": "Medimat1"}
    )


def test_robot_and_pmr_are_compared_per_weekday_for_months_both_cover():
    # September 2026: Monday 21 to Friday 25 and Monday 28 to Wednesday 30 are 8 weekdays;
    # the weekend days are ignored. The PMR month has 6 + 4 + 6 + 6 + 6 = 28 scripts over the
    # 22 weekdays of September.
    september = [f"2026-09-{d}" for d in (21, 22, 23, 24, 25, 26, 27, 28, 29, 30)]
    result = run(activity_daily=robot_days(september))
    table_ = result.comparison
    assert list(table_["month"]) == [pd.Timestamp("2026-09-01")]
    row = table_.iloc[0]
    assert row["weekdays_covered"] == 8
    assert row["robot_per_weekday"] == pytest.approx(200.0)
    assert row["pmr_scripts_per_weekday"] == pytest.approx(
        run().monthly.set_index("month").loc[pd.Timestamp("2026-09-01"), "scripts"] / 22
    )
    assert row["ratio"] == pytest.approx(row["robot_per_weekday"] / row["pmr_scripts_per_weekday"])


def test_a_month_with_fewer_than_three_robot_weekdays_is_not_compared():
    result = run(activity_daily=robot_days(["2026-09-29", "2026-09-30"]))
    assert result.comparison.empty


def test_no_robot_data_means_no_comparison():
    assert run().comparison.empty
    assert run(activity_daily=robot_days([])).comparison.empty


def test_the_comparison_is_for_month_only_pmr_files():
    data = table(rows("Alpha 10mg Tablets", ("2026-09-22", 10), basis="day"))
    result = run(data, activity_daily=robot_days(["2026-09-21", "2026-09-22", "2026-09-23"]))
    assert result.comparison.empty
