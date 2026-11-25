"""Usage rate, days of cover and the four stock classes.

    usage rate    = quantity dispensed in the period / days in the period
    days of cover = stock quantity / usage rate

Classes: dead stock (stock but no usage in the period), overstocked (cover above the upper
limit), understocked (cover below the lower limit), healthy (everything else). Limits and the
period come only from `Settings`; nothing is hard-coded here.

Rows that cannot be classified honestly are never dropped or guessed: they go to
`unclassified` (bad quantities), `awaiting_review` (stock not matched to the PMR yet) or
`usage_no_stock` (dispensed in the PMR but not in the robot).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import COMBINED_PER_BRANCH, DEFAULT_SETTINGS, PER_ROBOT, UNKNOWN_BRANCH, Settings
from match import MATCHED, NO_MATCH, REVIEW, MatchResult

DEAD, OVER, UNDER, HEALTHY = "dead_stock", "overstocked", "understocked", "healthy"
CLASSES = (DEAD, OVER, UNDER, HEALTHY)
UNCLASSIFIED = "unclassified"
POOLED_BRANCH = "All branches"  # used when the PMR carries no branch

ITEM_COLUMNS = (
    "branch",
    "robot_id",
    "item_name",
    "name_norm",
    "strength_norm",
    "item_id",
    "stock_qty",
    "dispensed_in_period",
    "usage_rate_per_day",
    "days_of_cover",
    "classification",
    "reason",
    "match_level",
    "confirmed",
    "usage_rows_excluded",
    "robots_sharing",
    "earliest_expiry",
    "unit",
)
USAGE_NO_STOCK_COLUMNS = (
    "branch",
    "item_name",
    "item_id",
    "dispensed_in_period",
    "usage_rate_per_day",
    "dispensing_lines",
)


@dataclass
class AnalysisResult:
    items: pd.DataFrame  # classified rows (CLASSES only)
    unclassified: pd.DataFrame  # stock rows with missing or negative quantities, etc.
    awaiting_review: pd.DataFrame  # stock rows not yet matched to the PMR
    usage_no_stock: pd.DataFrame  # PMR products dispensed with no robot stock in that branch
    summary: dict[str, int] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def by_class(self, classification: str) -> pd.DataFrame:
        return self.items[self.items["classification"] == classification].reset_index(drop=True)


def _top(values: pd.Series) -> str:
    return values.value_counts().index[0] if len(values) else ""


def _month_window(
    months: pd.Series, period_days: int, as_of, include_latest: bool, warnings: list[str]
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(first month, last month) of a whole-month usage window. Dates are first-of-month.

    The window is round(period_days / 30.44) whole months, ending with the latest month in the
    data, or the one before it unless `include_latest`, because the latest month of a report
    run mid-month is probably incomplete.
    """
    count = max(1, round(period_days / 30.4375))
    latest, earliest = months.max(), months.min()
    if as_of is not None:
        last = pd.Timestamp(as_of).to_period("M").to_timestamp()
    elif include_latest or latest == earliest:
        last = latest
        if latest == earliest and not include_latest:
            warnings.append("The PMR holds a single month, so it was used even if incomplete")
    else:
        last = latest - pd.offsets.MonthBegin(1)
        warnings.append(
            f"The latest month ({latest:%b %Y}) was left out because it may be incomplete"
        )
    first = last - pd.DateOffset(months=count - 1)
    return first, last


def _prepare_usage(
    usage: pd.DataFrame,
    period_days: int,
    as_of,
    warnings: list[str],
    include_latest_month: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """Flag unusable rows, fix the usage window and decide how usage maps to branches."""
    u = usage.copy()
    u["dispensed_qty"] = pd.to_numeric(u["dispensed_qty"], errors="coerce")
    u["dispense_date"] = pd.to_datetime(u["dispense_date"], errors="coerce")
    quantity_bad = u["dispensed_qty"].isna() | (u["dispensed_qty"] < 0)
    date_bad = u["dispense_date"].isna()
    u["issue"] = quantity_bad | date_bad
    if quantity_bad.any() or date_bad.any():
        warnings.append(
            f"{int(u['issue'].sum())} PMR row(s) with a missing or negative quantity, or no "
            "date, were left out of the usage totals"
        )
    valid_dates = u.loc[~u["issue"], "dispense_date"]
    basis = u["date_basis"].fillna("day") if "date_basis" in u else pd.Series("day", index=u.index)
    monthly = bool(len(valid_dates)) and bool((basis[~u["issue"]] == "month").all())
    if monthly:  # whole months: the window is a number of calendar months
        first, last = _month_window(valid_dates, period_days, as_of, include_latest_month, warnings)
        start, end = first, last + pd.offsets.MonthEnd(0)
        u["in_window"] = (~u["issue"]) & u["dispense_date"].between(first, last)
        rate_days = int((end - start).days + 1)
        observed = int((valid_dates.max() + pd.offsets.MonthEnd(0) - valid_dates.min()).days + 1)
        held = valid_dates[(valid_dates >= first) & (valid_dates <= last)].nunique()
        wanted = (last.year - first.year) * 12 + last.month - first.month + 1
        if held < wanted:
            warnings.append(
                f"The PMR holds {held} of the {wanted} whole month(s) in the usage window "
                f"({first:%b %Y} to {last:%b %Y}), so usage rates are understated"
            )
    else:
        end = pd.Timestamp(as_of) if as_of is not None else valid_dates.max()
        start = end - pd.Timedelta(days=period_days - 1) if pd.notna(end) else pd.NaT
        in_window = (
            (~u["issue"]) & u["dispense_date"].between(start, end) if pd.notna(end) else False
        )
        u["in_window"] = in_window
        rate_days = period_days
        observed = int((valid_dates.max() - valid_dates.min()).days + 1) if len(valid_dates) else 0
        if observed < period_days:
            warnings.append(
                f"PMR data covers {observed} day(s) but the period is {period_days} days, so "
                "usage rates are understated and stock looks over-covered"
            )

    pooled = len(u) > 0 and bool((u["branch"] == UNKNOWN_BRANCH).all())
    u["unattributed"] = False
    if pooled:
        u["group"] = POOLED_BRANCH
        warnings.append("PMR has no branch, so stock and usage are pooled across all branches")
    else:
        u["group"] = u["branch"]
        u["unattributed"] = u["branch"] == UNKNOWN_BRANCH
        if u["unattributed"].any():
            warnings.append(
                f"{int(u['unattributed'].sum())} PMR row(s) have no branch and were left out"
            )
    meta = {
        "period_start": start,
        "period_end": end if pd.notna(end) else pd.NaT,
        "observed_days": observed,
        "pooled_across_branches": pooled,
        "date_basis": "month" if monthly else "day",
        "rate_days": rate_days,  # days the usage rate is divided by (the window's real length)
    }
    return u, meta


def _usage_totals(u: pd.DataFrame) -> pd.DataFrame:
    """Per (group, pmr_key): quantity in the window, line count, excluded-row count, name."""
    usable = u[(u["pmr_key"] != "") & (~u["unattributed"])]
    window = usable[usable["in_window"]]
    totals = window.groupby(["group", "pmr_key"]).agg(
        dispensed=("dispensed_qty", "sum"), lines=("dispensed_qty", "size")
    )
    names = usable.groupby(["group", "pmr_key"])["item_name"].agg(_top).rename("name")
    excluded = usable[usable["issue"]].groupby(["group", "pmr_key"]).size().rename("excluded")
    table = pd.concat([names, totals, excluded], axis=1).reset_index()
    for column in ("dispensed", "lines", "excluded"):
        table[column] = table[column].fillna(0)
    return table


def _stock_items(
    matches: pd.DataFrame, level: str, pooled: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per item at the chosen level, stock rows awaiting review)."""
    awaiting = matches[matches["match_status"] == REVIEW]
    decided = matches[matches["match_status"].isin([MATCHED, NO_MATCH])].copy()
    decided["group"] = POOLED_BRANCH if pooled else decided["branch"]
    decided["item_id"] = decided["pmr_key"].where(
        decided["match_status"] == MATCHED, decided["stock_key"]
    )
    decided["bad"] = decided["stock_qty"].isna() | (decided["stock_qty"] < 0)
    keys = ["group", "item_id"] + (["robot_id"] if level == PER_ROBOT else [])
    if decided.empty:
        return pd.DataFrame(), awaiting
    items = decided.groupby(keys, sort=True).agg(
        item_name=("item_name", "first"),
        name_norm=("name_norm", "first"),
        strength_norm=("strength_norm", "first"),
        stock_qty=("stock_qty", "sum"),
        bad=("bad", "any"),
        earliest_expiry=("expiry_date", "min"),
        unit=("unit", "first"),
        match_level=("match_level", "min"),
        confirmed=("confirmed", "any"),
        robots=("robot_id", lambda s: ", ".join(sorted(set(s)))),
    )
    items = items.reset_index()
    if level == COMBINED_PER_BRANCH:
        items["robot_id"] = items["robots"]
    sharing = decided.groupby(["group", "item_id"])["robot_id"].nunique()
    items["robots_sharing"] = [sharing[(g, i)] for g, i in zip(items["group"], items["item_id"])]
    return items.drop(columns="robots"), awaiting


def _classify(items: pd.DataFrame, over_days: int, under_days: int) -> pd.DataFrame:
    stock, rate = items["stock_qty"], items["usage_rate_per_day"]
    with np.errstate(divide="ignore", invalid="ignore"):
        cover = np.where(rate > 0, stock / rate, np.nan)
    items["days_of_cover"] = cover
    conditions = [
        items["bad"],
        (stock == 0) & (rate <= 0),
        rate <= 0,
        cover > over_days,
        cover < under_days,
    ]
    labels = [UNCLASSIFIED, UNCLASSIFIED, DEAD, OVER, UNDER]
    reasons = [
        "missing or negative stock quantity",
        "no stock and no usage in the period",
        "stock on hand and no usage in the period",
        f"days of cover above {over_days}",
        f"days of cover below {under_days}",
    ]
    items["classification"] = np.select(conditions, labels, default=HEALTHY)
    items["reason"] = np.select(conditions, reasons, default="within the limits")
    return items


def analyse(
    result: MatchResult,
    settings: Settings = DEFAULT_SETTINGS,
    as_of: str | None = None,
    include_latest_month: bool = False,
) -> AnalysisResult:
    """Classify every matched stock item. `as_of` fixes the last day of the usage window.

    By default the window ends on the latest valid PMR date, so a stale export is measured
    against its own last day.
    """
    problems = settings.problems()
    if problems:
        raise ValueError("; ".join(problems))
    values = settings.values()
    period, over_days, under_days = (
        values["period_days"],
        values["overstock_days"],
        values["understock_days"],
    )
    level = values["classification_level"]

    warnings: list[str] = []
    u, window_meta = _prepare_usage(result.usage, period, as_of, warnings, include_latest_month)
    rate_days = window_meta["rate_days"]
    totals = _usage_totals(u)
    items, awaiting = _stock_items(result.matches, level, window_meta["pooled_across_branches"])

    if items.empty:
        items = pd.DataFrame(columns=list(ITEM_COLUMNS) + ["bad"])
    else:
        merged = items.merge(
            totals, how="left", left_on=["group", "item_id"], right_on=["group", "pmr_key"]
        )
        for column in ("dispensed", "lines", "excluded"):
            merged[column] = merged[column].fillna(0)
        shares = merged["robots_sharing"] if level == PER_ROBOT else 1
        merged["dispensed_in_period"] = merged["dispensed"] / shares
        merged["usage_rate_per_day"] = merged["dispensed_in_period"] / rate_days
        merged["usage_rows_excluded"] = merged["excluded"].astype(int)
        merged["branch"] = merged["group"]
        items = _classify(merged, over_days, under_days)
        if level == PER_ROBOT and (items["robots_sharing"] > 1).any():
            warnings.append(
                "Some items sit in more than one robot in the same branch; the PMR cannot "
                "separate robots, so their branch usage is split equally between them"
            )

    items = items.reindex(columns=list(ITEM_COLUMNS) + ["bad"])
    classified = items[items["classification"].isin(CLASSES)].copy()
    classified["class_order"] = classified["classification"].map(
        {name: position for position, name in enumerate(CLASSES)}
    )
    classified = classified.sort_values(["branch", "class_order", "item_name"]).drop(
        columns="class_order"
    )
    unclassified = items[items["classification"] == UNCLASSIFIED]

    stocked = set(zip(items["branch"], items["item_id"]))
    unstocked = pd.Series(
        [(g, k) not in stocked for g, k in zip(totals["group"], totals["pmr_key"])],
        index=totals.index,
        dtype=bool,
    )
    orphans = totals[(totals["dispensed"] > 0) & unstocked]
    usage_no_stock = pd.DataFrame(
        {
            "branch": orphans["group"],
            "item_name": orphans["name"],
            "item_id": orphans["pmr_key"],
            "dispensed_in_period": orphans["dispensed"],
            "usage_rate_per_day": orphans["dispensed"] / rate_days,
            "dispensing_lines": orphans["lines"].astype(int),
        }
    ).reset_index(drop=True)

    summary = {name: int((classified["classification"] == name).sum()) for name in CLASSES}
    summary[UNCLASSIFIED] = len(unclassified)
    summary["awaiting_review"] = len(awaiting)
    summary["usage_no_stock"] = len(usage_no_stock)

    meta = {
        **window_meta,
        "period_days": period,
        "settings": values,
        "setting_labels": settings.labels(),
        "match_rate": result.match_rate,
        "usage_basis": sorted(result.usage["qty_basis"].dropna().unique().tolist()),
    }
    return AnalysisResult(
        items=classified.drop(columns="bad").reset_index(drop=True),
        unclassified=unclassified.drop(columns="bad").reset_index(drop=True),
        awaiting_review=awaiting.reset_index(drop=True),
        usage_no_stock=usage_no_stock,
        summary=summary,
        meta=meta,
        warnings=warnings,
    )
