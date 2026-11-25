"""The internal schema, defined once. Everything else imports it.

Internal names are the only column names the code may use. Source headers live in
column_map.yaml, never in Python.
"""

from __future__ import annotations

import re

ROBOT_SOURCES = {
    "omnicell_robot_1": "robot_1",
    "omnicell_robot_2": "robot_2",
}
PMR_SOURCE = "pmr"
ACTIVITY_SOURCE = "omnicell_activity"  # robot activity statistics: daily counts, no item column
SOURCES = (*ROBOT_SOURCES, PMR_SOURCE, ACTIVITY_SOURCE)

# Columns the pipeline adds itself during cleaning (not read from any export).
ADDED_COLUMNS = ("robot_id", "branch")

# Required and optional internal columns per source kind.
ROBOT_REQUIRED = ("item_name", "stock_qty")
ROBOT_OPTIONAL = ("item_code", "strength", "pack_size", "unit", "expiry_date", "branch")
# dispensed_qty is optional because the test PMR export has no quantity column. Without it each
# row counts as one dispensing line (a usage proxy, flagged by load). Confirm with the real PMR.
PMR_REQUIRED = ("item_name", "dispense_date")
PMR_OPTIONAL = (
    "dispensed_qty",
    "generic_name",
    "item_code",
    "strength",
    "pack_size",
    "unit",
    "branch",
    "marked_deleted",  # row removed in the PMR: dropped on load, never counted as usage
    "not_dispensed",  # script line never handed out: dropped on load
)

# Activity statistics carry one row per day (period) per robot system, with counts only.
ACTIVITY_REQUIRED = ("activity_date", "dispensed")
ACTIVITY_OPTIONAL = ("system_name", "period_hours", "stocked", "rearranged")

REQUIRED = {
    **{source: ROBOT_REQUIRED for source in ROBOT_SOURCES},
    PMR_SOURCE: PMR_REQUIRED,
    ACTIVITY_SOURCE: ACTIVITY_REQUIRED,
}
OPTIONAL = {
    **{source: ROBOT_OPTIONAL for source in ROBOT_SOURCES},
    PMR_SOURCE: PMR_OPTIONAL,
    ACTIVITY_SOURCE: ACTIVITY_OPTIONAL,
}

# Every internal column name that any source may carry.
ALL_COLUMNS = tuple(
    dict.fromkeys(col for source in SOURCES for col in (*REQUIRED[source], *OPTIONAL[source]))
)


def normalise_header(header: str) -> str:
    """Case, spacing and separator tolerant form of a header, for matching only."""
    text = str(header).replace("\ufeff", "").lower()
    text = re.sub(r"[\s_\-]+", " ", text)
    return text.strip()


# Standard tables produced by clean.py. Every cleaned row keeps source_row (the file line
# number) so any result can be traced back to the export.
STANDARD_STOCK_COLUMNS = (
    "robot_id",
    "branch",
    "item_code",
    "item_name",
    "name_norm",
    "strength_norm",
    "strength_source",
    "pack_size",
    "unit",
    "stock_qty",
    "expiry_date",
    "source_row",
)
STANDARD_USAGE_COLUMNS = (
    "branch",
    "item_code",
    "item_name",
    "generic_name",
    "name_norm",
    "strength_norm",
    "strength_source",
    "pack_size",
    "unit",
    "dispensed_qty",
    "qty_basis",
    "dispense_date",
    "date_basis",  # "day", or "month" when the PMR gives only a month (dated the 1st)
    "source_row",
)

# Standard table produced by activity.py: one row per robot system per day.
STANDARD_ACTIVITY_COLUMNS = (
    "system_name",
    "robot_id",
    "branch",
    "activity_date",
    "period_hours",
    "dispensed",
    "stocked",
    "rearranged",
    "pull",
    "source_row",
)
