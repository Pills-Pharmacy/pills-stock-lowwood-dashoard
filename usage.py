"""Usage analysis from the PMR alone: what is dispensed, by month and by product.

This is what can honestly be said without knowing how much stock the robot holds. It cannot
classify stock (dead, over, under) because that needs a stock quantity; it shows instead what
the pharmacy dispenses, which products are rising or falling, and which products used to be
dispensed and no longer are (the nearest thing to dead stock a PMR can show). The robot's own
activity counts are set beside the PMR as a sanity check.

The usage window is the same one `analyse` uses (whole months for a month-only PMR), so the two
views agree on what "the period" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analyse import _month_window, _prepare_usage
from config import DEFAULT_SETTINGS, TREND_MIN_SCRIPTS, TREND_THRESHOLD, Settings
from match import _add_pmr_key

MONTHLY_COLUMNS = ("month", "scripts", "quantity", "products")
PRODUCT_COLUMNS = (
    "product",
    "scripts",
    "quantity",
    "rate_per_day",
    "share_of_scripts",
    "previous_quantity",
    "change",
    "trend",
    "last_dispensed",
)
STOPPED_COLUMNS = ("product", "last_dispensed", "months_since", "scripts_before", "quantity_before")
COMPARE_COLUMNS = (
    "month",
    "weekdays_covered",
    "robot_per_weekday",
    "pmr_scripts_per_weekday",
    "ratio",
)
RISING, FALLING, STEADY, NEW, FEW = "rising", "falling", "steady", "new", "few scripts"


@dataclass
class UsageResult:
    monthly: pd.DataFrame  # every month in the PMR: scripts, quantity, distinct products
    products: pd.DataFrame  # products dispensed in the window, busiest first
    stopped: pd.DataFrame  # dispensed earlier, nothing in the window
    comparison: pd.DataFrame  # robot per weekday against PMR scripts per weekday, by month
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _top(values: pd.Series) -> str:
    return values.value_counts().index[0] if len(values) else ""


def _previous_window(meta: dict, monthly: bool) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The window of the same length immediately before the usage window."""
    start, end = meta["period_start"], meta["period_end"]
    if monthly:
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        return start - pd.DateOffset(months=months), start - pd.DateOffset(months=1)
    return start - pd.Timedelta(days=meta["rate_days"]), start - pd.Timedelta(days=1)


def _months_between(earlier: pd.Series, later: pd.Timestamp) -> pd.Series:
    return (later.year - earlier.dt.year) * 12 + later.month - earlier.dt.month


def _comparison(monthly: pd.DataFrame, daily: pd.DataFrame | None) -> pd.DataFrame:
    """Robot dispensed per weekday against PMR scripts per weekday, for months both cover.

    Both count roughly one item per script line, so they should be of the same order; the
    robot's own weekend dispensing is ignored. Needs at least three weekdays of robot data.
    """
    empty = pd.DataFrame(columns=list(COMPARE_COLUMNS))
    if daily is None or daily.empty or monthly.empty:
        return empty
    robot = daily.assign(
        month=daily["activity_date"].dt.to_period("M").dt.to_timestamp(),
        weekday=daily["activity_date"].dt.day_of_week < 5,
    )
    robot = (
        robot[robot["weekday"]]
        .groupby("month")
        .agg(weekdays_covered=("activity_date", "nunique"), dispensed=("dispensed", "sum"))
    )
    rows = []
    for month, scripts in zip(monthly["month"], monthly["scripts"]):
        if month not in robot.index or robot.loc[month, "weekdays_covered"] < 3:
            continue
        in_month = np.busday_count(month.date(), (month + pd.offsets.MonthBegin(1)).date())
        covered = int(robot.loc[month, "weekdays_covered"])
        per_robot = robot.loc[month, "dispensed"] / covered
        per_pmr = scripts / in_month
        rows.append(
            [month, covered, per_robot, per_pmr, per_robot / per_pmr if per_pmr else np.nan]
        )
    return pd.DataFrame(rows, columns=list(COMPARE_COLUMNS))


def analyse_usage(
    usage: pd.DataFrame,
    settings: Settings = DEFAULT_SETTINGS,
    activity_daily: pd.DataFrame | None = None,
    as_of: str | None = None,
    include_latest_month: bool = False,
) -> UsageResult:
    """Summarise the cleaned PMR usage table. Nothing is dropped silently: rows with a missing
    or negative quantity, or no date, are counted in a warning and left out."""
    problems = settings.problems()
    if problems:
        raise ValueError("; ".join(problems))
    period = settings.period_days.value
    warnings: list[str] = []
    keyed = _add_pmr_key(usage)
    u, meta = _prepare_usage(keyed, period, as_of, warnings, include_latest_month)
    valid = u[(~u["issue"]) & (u["pmr_key"] != "")]
    monthly_basis = meta["date_basis"] == "month"
    blank = int((u["pmr_key"] == "").sum())
    if blank:
        warnings.append(
            f"{blank} PMR row(s) have no product name and are not in the product tables"
        )

    by_month = valid.assign(month=valid["dispense_date"].dt.to_period("M").dt.to_timestamp())
    monthly = (
        by_month.groupby("month")
        .agg(scripts=("dispensed_qty", "size"), quantity=("dispensed_qty", "sum"),
             products=("pmr_key", "nunique"))
        .reset_index()
    )  # fmt: skip

    window = valid[valid["in_window"]]
    if monthly_basis:
        prev_start, prev_end = _previous_window(meta, True)
    elif pd.notna(meta["period_start"]):
        prev_start, prev_end = _previous_window(meta, False)
    else:
        prev_start = prev_end = pd.NaT
    previous = valid[valid["dispense_date"].between(prev_start, prev_end)]

    names = valid.groupby("pmr_key")["item_name"].agg(_top).rename("product")
    current = window.groupby("pmr_key").agg(
        scripts=("dispensed_qty", "size"), quantity=("dispensed_qty", "sum")
    )
    before = previous.groupby("pmr_key")["dispensed_qty"].sum().rename("previous_quantity")
    last = valid.groupby("pmr_key")["dispense_date"].max().rename("last_dispensed")

    products = (
        current.join(names)
        .join(before)
        .join(last)
        .fillna({"previous_quantity": 0})
        .reset_index()
    )
    total = float(products["quantity"].sum())
    total_scripts = float(products["scripts"].sum())
    rate_days = meta["rate_days"]
    products["rate_per_day"] = products["quantity"] / rate_days if rate_days else np.nan
    products["share_of_scripts"] = products["scripts"] / total_scripts if total_scripts else np.nan
    products["change"] = np.where(
        products["previous_quantity"] > 0,
        products["quantity"]
        / products["previous_quantity"].where(products["previous_quantity"] > 0)
        - 1,
        np.nan,
    )
    products["trend"] = np.select(
        [
            products["scripts"] < TREND_MIN_SCRIPTS,
            products["previous_quantity"] <= 0,
            products["change"] >= TREND_THRESHOLD,
            products["change"] <= -TREND_THRESHOLD,
        ],
        [FEW, NEW, RISING, FALLING],
        default=STEADY,
    )
    products = (
        products[[*PRODUCT_COLUMNS, "pmr_key"]]  # pmr_key lets suggest.py join to the robot
        .sort_values(["scripts", "quantity"], ascending=False)  # scripts: units differ by product
        .reset_index(drop=True)
    )

    in_window = set(window.groupby("pmr_key")["dispensed_qty"].sum().loc[lambda s: s > 0].index)
    earlier = (
        valid[valid["dispense_date"] < meta["period_start"]]
        if pd.notna(meta["period_start"])
        else valid.iloc[0:0]
    )
    ghosts = earlier[~earlier["pmr_key"].isin(in_window)]
    stopped = (
        ghosts.groupby("pmr_key")
        .agg(last_dispensed=("dispense_date", "max"), scripts_before=("dispensed_qty", "size"),
             quantity_before=("dispensed_qty", "sum"))
        .join(names)
        .reset_index(drop=True)
    )  # fmt: skip
    if len(stopped):
        stopped["months_since"] = _months_between(stopped["last_dispensed"], meta["period_end"])
    else:
        stopped["months_since"] = pd.Series(dtype=int)
    stopped = (
        stopped[list(STOPPED_COLUMNS)]
        .sort_values(["scripts_before", "last_dispensed"], ascending=[False, False])
        .reset_index(drop=True)
    )

    meta = {
        **meta,
        "period_days": period,
        "scripts": len(window),
        "quantity": total,
        "products": len(products),
        "products_stopped": len(stopped),
        "qty_basis": sorted(u["qty_basis"].dropna().unique().tolist()),
        "months_in_data": len(monthly),
        "trend_threshold": TREND_THRESHOLD,
        "trend_min_scripts": TREND_MIN_SCRIPTS,
        "settings": settings.values(),
        "setting_labels": settings.labels(),
    }
    comparison = (
        _comparison(monthly, activity_daily)
        if monthly_basis
        else _comparison(monthly.iloc[0:0], None)
    )
    return UsageResult(
        monthly=monthly[list(MONTHLY_COLUMNS)],
        products=products,
        stopped=stopped,
        comparison=comparison,
        warnings=warnings,
        meta=meta,
    )


__all__ = ["UsageResult", "_month_window", "analyse_usage"]
