"""Robot activity statistics: clean, merge repeated pulls, summarise, cross-check the PMR.

The activity export is one row per day per robot system with counts only (dispensed, stocked,
rearranged). It has no item column, so it cannot feed the stock audit; it answers how busy the
robot is, and it gives an independent total to compare the PMR against.

The same days are often pulled several times and later pulls can revise earlier days (a
REARRANGED count changed from 0 to 829 for one day between two real pulls). Merging therefore
keeps, for each day, the value from the most recent pull and reports every day that changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import (
    ACTIVITY_SYSTEM_ROBOT,
    DEFAULT_SETTINGS,
    ROBOT_REGISTRY,
    UNKNOWN_BRANCH,
    Settings,
)
from load import LoadResult, parse_dates
from schema import STANDARD_ACTIVITY_COLUMNS

COUNTS = ("dispensed", "stocked", "rearranged")
SUMMARY_COLUMNS = (
    "system_name",
    "robot_id",
    "branch",
    "first_day",
    "last_day",
    "days_with_data",
    "missing_days",
    "active_days",
    "quiet_days",
    "total_dispensed",
    "total_stocked",
    "total_rearranged",
    "avg_dispensed_per_day",
    "avg_dispensed_per_active_day",
    "busiest_day",
    "busiest_day_dispensed",
)
DAILY_COLUMNS = (
    "system_name",
    "activity_date",
    "dispensed",
    "stocked",
    "rearranged",
    "dispensed_7d_avg",
)
WEEKDAY_COLUMNS = ("system_name", "weekday", "days", "avg_dispensed")
CONFLICT_COLUMNS = ("system_name", "activity_date", "field", "earlier_value", "kept_value")
COMPARE_COLUMNS = ("activity_date", "robot_dispensed", "pmr_total", "ratio")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class ActivityResult:
    table: pd.DataFrame  # merged standard table, every day we hold
    summary: pd.DataFrame  # one row per system, for the usage window
    daily: pd.DataFrame  # per day in the window, with a 7-day average
    weekday: pd.DataFrame  # average dispensed by weekday
    conflicts: pd.DataFrame  # days whose counts changed between pulls
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def clean_activity(result: LoadResult, pull: int = 0, branch: str | None = None) -> pd.DataFrame:
    """Standard activity table for one pull. Raises ValueError if the load stopped.

    `branch` (a pharmacy label) is written on every row; without it the branch comes from the
    robot registry via the system name.
    """
    if result.stopped:
        raise ValueError("The activity file did not load; fix the errors before cleaning")
    frame = result.data
    system = (
        frame["system_name"].str.strip()
        if "system_name" in frame
        else pd.Series("", index=frame.index)
    )
    robot = system.str.lower().map(ACTIVITY_SYSTEM_ROBOT).fillna("")
    from_registry = robot.map(
        lambda r: ROBOT_REGISTRY[r].branch if r in ROBOT_REGISTRY else UNKNOWN_BRANCH
    )
    branch_column = pd.Series(branch, index=frame.index) if branch else from_registry

    def number(column: str) -> pd.Series:
        if column not in frame:
            return pd.Series(float("nan"), index=frame.index)
        return pd.to_numeric(frame[column].str.strip(), errors="coerce").astype(float)

    table = pd.DataFrame(
        {
            "system_name": system,
            "robot_id": robot,
            "branch": branch_column,
            "activity_date": parse_dates(frame["activity_date"])[0].dt.normalize(),
            "period_hours": number("period_hours"),
            "dispensed": number("dispensed"),
            "stocked": number("stocked"),
            "rearranged": number("rearranged"),
            "pull": pull,
            "source_row": frame.index.to_series().astype(int) + 2,
        }
    )
    return table[list(STANDARD_ACTIVITY_COLUMNS)].reset_index(drop=True)


def merge_pulls(tables: list[pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Combine repeated pulls. Returns (one row per system and day, days that changed).

    For a day held by several pulls the most recent pull wins: the one whose newest day is the
    latest, or the later one in the list when that ties.
    """
    columns = list(STANDARD_ACTIVITY_COLUMNS)
    if not tables:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=list(CONFLICT_COLUMNS))
    ranked = sorted(enumerate(tables), key=lambda item: (item[1]["activity_date"].max(), item[0]))
    stacked = pd.concat([table.assign(pull=position) for position, (_, table) in enumerate(ranked)])
    stacked = stacked[stacked["activity_date"].notna()]
    keys = ["system_name", "activity_date"]
    kept = stacked.drop_duplicates(keys, keep="last")
    earlier = stacked[stacked.duplicated(keys, keep="last")]

    rows = []
    kept_by_key = kept.set_index(keys)
    for _, old in earlier.iterrows():
        new = kept_by_key.loc[(old["system_name"], old["activity_date"])]
        for column in COUNTS:
            before, after = old[column], new[column]
            if not (pd.isna(before) and pd.isna(after)) and before != after:
                rows.append([old["system_name"], old["activity_date"], column, before, after])
    conflicts = pd.DataFrame(rows, columns=list(CONFLICT_COLUMNS)).drop_duplicates()
    merged = kept.sort_values(keys).reset_index(drop=True)
    return merged[columns], conflicts.sort_values(
        keys[:1] + ["activity_date", "field"]
    ).reset_index(drop=True)


def _system_summary(rows: pd.DataFrame, system: str) -> dict:
    days = rows["activity_date"]
    first, last = days.min(), days.max()
    expected = pd.date_range(first, last)
    busiest = rows.loc[rows["dispensed"].idxmax()] if rows["dispensed"].notna().any() else None
    dispensed = rows["dispensed"]
    active = dispensed > 0
    return {
        "system_name": system,
        "robot_id": rows["robot_id"].iloc[0],
        "branch": rows["branch"].iloc[0],
        "first_day": first,
        "last_day": last,
        "days_with_data": len(rows),
        "missing_days": len(expected.difference(pd.DatetimeIndex(days))),
        "active_days": int(active.sum()),
        "quiet_days": int((dispensed == 0).sum()),
        "total_dispensed": dispensed.sum(),
        "total_stocked": rows["stocked"].sum(),
        "total_rearranged": rows["rearranged"].sum(),
        "avg_dispensed_per_day": dispensed.mean(),
        "avg_dispensed_per_active_day": dispensed[active].mean() if active.any() else float("nan"),
        "busiest_day": busiest["activity_date"] if busiest is not None else pd.NaT,
        "busiest_day_dispensed": busiest["dispensed"] if busiest is not None else float("nan"),
    }


def analyse_activity(
    table: pd.DataFrame,
    settings: Settings = DEFAULT_SETTINGS,
    conflicts: pd.DataFrame | None = None,
    as_of: str | None = None,
) -> ActivityResult:
    """Summarise the usage window (period_days ending on the latest day, or `as_of`)."""
    problems = settings.problems()
    if problems:
        raise ValueError("; ".join(problems))
    period = settings.period_days.value
    warnings: list[str] = []
    conflicts = conflicts if conflicts is not None else pd.DataFrame(columns=list(CONFLICT_COLUMNS))

    dated = table[table["activity_date"].notna()]
    end = pd.Timestamp(as_of) if as_of is not None else dated["activity_date"].max()
    if pd.isna(end):
        empty = pd.DataFrame(columns=list(SUMMARY_COLUMNS))
        return ActivityResult(
            table, empty, pd.DataFrame(columns=list(DAILY_COLUMNS)),
            pd.DataFrame(columns=list(WEEKDAY_COLUMNS)), conflicts,
            ["No readable activity rows"], {},
        )  # fmt: skip
    start = end - pd.Timedelta(days=period - 1)
    window = dated[dated["activity_date"].between(start, end)].sort_values(
        ["system_name", "activity_date"]
    )

    observed = (
        int((window["activity_date"].max() - window["activity_date"].min()).days + 1)
        if len(window)
        else 0
    )
    if observed < period:
        warnings.append(
            f"Activity data covers {observed} day(s) but the period is {period} days, so totals "
            "cover only part of it"
        )
    if len(conflicts):
        days = conflicts[["system_name", "activity_date"]].drop_duplicates().shape[0]
        warnings.append(
            f"{days} day(s) changed between pulls; the most recent pull's counts were kept"
        )
    unknown = sorted(window.loc[window["robot_id"] == "", "system_name"].unique())
    if unknown:
        warnings.append(
            f"System(s) not in the robot registry, branch unknown: {', '.join(unknown)}"
        )
    if window[list(COUNTS)].isna().any().any():
        warnings.append("Some counts are missing or unreadable and were left out of the totals")

    summary = pd.DataFrame(
        [_system_summary(rows, system) for system, rows in window.groupby("system_name")],
        columns=list(SUMMARY_COLUMNS),
    )
    daily = window[["system_name", "activity_date", *COUNTS]].copy()
    daily["dispensed_7d_avg"] = daily.groupby("system_name")["dispensed"].transform(
        lambda s: s.rolling(7, min_periods=1).mean()
    )
    names = window["activity_date"].dt.day_name()
    weekday = (
        window.assign(weekday=names)
        .groupby(["system_name", "weekday"])["dispensed"]
        .agg(days="size", avg_dispensed="mean")
        .reset_index()
    )
    weekday["order"] = weekday["weekday"].map({d: i for i, d in enumerate(WEEKDAYS)})
    weekday = weekday.sort_values(["system_name", "order"]).drop(columns="order")

    meta = {
        "period_start": start,
        "period_end": end,
        "period_days": period,
        "observed_days": observed,
        "systems": sorted(window["system_name"].unique().tolist()),
    }
    return ActivityResult(
        table=table,
        summary=summary,
        daily=daily[list(DAILY_COLUMNS)].reset_index(drop=True),
        weekday=weekday[list(WEEKDAY_COLUMNS)].reset_index(drop=True),
        conflicts=conflicts,
        warnings=warnings,
        meta=meta,
    )


def compare_with_pmr(daily: pd.DataFrame, usage: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Per day both sources cover: robot DISPENSED against the PMR total for that day.

    When the PMR gives only months, whole months are compared instead, and only months the
    robot data covers on every day.

    The PMR total is the sum of dispensed quantities, or a count of lines when the PMR has no
    quantity column. The two measure different things, so the ratio is a sanity check and not
    a reconciliation. It also cannot split robot dispensing from other dispensing (open question).
    """
    usable = usage[usage["dispensed_qty"].notna() & (usage["dispensed_qty"] >= 0)]
    monthly = (
        "date_basis" in usage
        and len(usage) > 0
        and bool((usage["date_basis"].fillna("day") == "month").all())
    )
    if (
        monthly
    ):  # the PMR has only months: compare month totals, for months the robot data fully covers
        months = daily["activity_date"].dt.to_period("M").dt.to_timestamp()
        held = (
            daily.assign(month=months)
            .groupby("month")
            .agg(dispensed=("dispensed", "sum"), days=("activity_date", "nunique"))
        )
        complete = held["days"] == held.index.days_in_month
        robot = held.loc[complete, "dispensed"]
    else:
        robot = daily.groupby("activity_date")["dispensed"].sum()
    pmr = usable.groupby("dispense_date")["dispensed_qty"].sum()
    days = robot.index.intersection(pmr.index)
    table = pd.DataFrame(
        {
            "activity_date": days,
            "robot_dispensed": robot.loc[days].to_numpy(),
            "pmr_total": pmr.loc[days].to_numpy(),
        }
    )
    table["ratio"] = table["pmr_total"] / table["robot_dispensed"].where(
        table["robot_dispensed"] > 0
    )
    basis = sorted(usage["qty_basis"].dropna().unique().tolist())
    robot_total, pmr_total = table["robot_dispensed"].sum(), table["pmr_total"].sum()
    summary = {
        "overlap_days": len(table),
        "robot_total": robot_total,
        "pmr_total": pmr_total,
        "ratio": pmr_total / robot_total if robot_total else float("nan"),
        "pmr_basis": ", ".join(basis),
        "granularity": "month" if monthly else "day",
    }
    return table[list(COMPARE_COLUMNS)].reset_index(drop=True), summary


def run_activity(
    loads: list[LoadResult],
    settings: Settings = DEFAULT_SETTINGS,
    as_of: str | None = None,
    branch: str | None = None,
) -> ActivityResult | None:
    """Clean, merge and analyse every pull that loaded. None if none did."""
    tables = [clean_activity(r, pull, branch) for pull, r in enumerate(loads) if not r.stopped]
    if not tables:
        return None
    merged, conflicts = merge_pulls(tables)
    return analyse_activity(merged, settings, conflicts, as_of)
