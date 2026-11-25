"""Excel report and static HTML fallback, both built from one `Report`.

`build_report` turns the analysis and match results into display tables once. `to_excel_bytes`
and `to_html` only lay those same tables out, so the two outputs cannot disagree. Nothing is
written to disk here; callers get bytes or text.

No patient data can reach either output: every table is checked against the patient-field
patterns before it is built, and the load warning about dropped patient columns is reduced to
a plain sentence that names no column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from io import BytesIO

import pandas as pd
from jinja2 import Environment

from analyse import CLASSES, AnalysisResult
from config import PATIENT_FIELD_PATTERNS
from load import Issue
from match import MatchResult
from schema import normalise_header

# One place for what each result table shows and how its columns are labelled.
ITEM_COLUMNS = {
    "branch": "Branch",
    "robot_id": "Robot",
    "item_name": "Item",
    "strength_norm": "Strength",
    "stock_qty": "Stock qty",
    "unit": "Unit",
    "dispensed_in_period": "Dispensed in period",
    "usage_rate_per_day": "Usage per day",
    "days_of_cover": "Days of cover",
    "reason": "Why",
    "earliest_expiry": "Earliest expiry",
    "match_level": "Match level",
    "confirmed": "Confirmed match",
}
USAGE_NO_STOCK_COLUMNS = {
    "branch": "Branch",
    "item_name": "Item",
    "dispensed_in_period": "Dispensed in period",
    "usage_rate_per_day": "Usage per day",
    "dispensing_lines": "Dispensing lines",
}
REVIEW_COLUMNS = {
    "side": "Side",
    "robot_id": "Robot",
    "branch": "Branch",
    "item_name": "Item",
    "strength_norm": "Strength",
    "match_level": "Match level",
    "reason": "Why",
    "candidate_name": "Closest PMR item",
    "candidate_score": "Score",
    "candidates": "All candidates",
    "ambiguous": "Ambiguous",
    "source_row": "File line",
}
CLASS_TITLES = {
    "dead_stock": "Dead stock",
    "overstocked": "Overstocked",
    "understocked": "Understocked",
    "healthy": "Healthy",
}
# Table key -> (sheet / section title, what it holds). Order is the order in both outputs.
TABLES = {
    **{key: (title, "") for key, title in CLASS_TITLES.items()},
    "review": ("Review", "Items a person must check before they can be relied on."),
    "unclassified": ("Unclassified", "Stock rows that could not be classified honestly."),
    "usage_no_stock": ("Usage no stock", "Dispensed in the PMR but not held in that branch."),
}
# Robot activity statistics: shown only when activity files were uploaded.
ACTIVITY_SUMMARY_COLUMNS = {
    "system_name": "System",
    "branch": "Branch",
    "first_day": "First day",
    "last_day": "Last day",
    "days_with_data": "Days with data",
    "missing_days": "Missing days",
    "active_days": "Active days",
    "quiet_days": "Quiet days",
    "total_dispensed": "Total dispensed",
    "total_stocked": "Total stocked",
    "total_rearranged": "Total rearranged",
    "avg_dispensed_per_day": "Average dispensed per day",
    "avg_dispensed_per_active_day": "Average per active day",
    "busiest_day": "Busiest day",
    "busiest_day_dispensed": "Busiest day dispensed",
}
ACTIVITY_DAILY_COLUMNS = {
    "system_name": "System",
    "activity_date": "Date",
    "dispensed": "Dispensed",
    "stocked": "Stocked",
    "rearranged": "Rearranged",
    "dispensed_7d_avg": "Dispensed 7-day average",
}
ACTIVITY_CHANGES_COLUMNS = {
    "system_name": "System",
    "activity_date": "Date",
    "field": "Count",
    "earlier_value": "Earlier pull",
    "kept_value": "Kept (latest pull)",
}
ACTIVITY_PMR_COLUMNS = {
    "activity_date": "Date",
    "robot_dispensed": "Robot dispensed",
    "pmr_total": "PMR total",
    "ratio": "PMR / robot",
}
# Usage analysis from the PMR alone: shown in the full report and as the whole report when no
# stock file was uploaded.
USAGE_PRODUCT_COLUMNS = {
    "product": "Product",
    "scripts": "Scripts",
    "quantity": "Quantity",
    "rate_per_day": "Quantity per day",
    "share_of_scripts": "Share of scripts",
    "previous_quantity": "Previous period quantity",
    "change": "Change",
    "trend": "Trend",
    "last_dispensed": "Last dispensed",
}
USAGE_STOPPED_COLUMNS = {
    "product": "Product",
    "last_dispensed": "Last dispensed",
    "months_since": "Months since",
    "scripts_before": "Scripts before",
    "quantity_before": "Quantity before",
}
USAGE_MONTHLY_COLUMNS = {
    "month": "Month",
    "scripts": "Scripts",
    "quantity": "Quantity",
    "products": "Products",
}
USAGE_ROBOT_COLUMNS = {
    "month": "Month",
    "weekdays_covered": "Robot weekdays covered",
    "robot_per_weekday": "Robot per weekday",
    "pmr_scripts_per_weekday": "PMR scripts per weekday",
    "ratio": "Robot / PMR",
}
USAGE_TABLES = {
    "usage_products": (
        "Usage products",
        "Products dispensed in the usage window, busiest first. Ranked by scripts because "
        "units differ between products; a trend is only called with enough scripts.",
    ),
    "usage_stopped": (
        "Usage stopped",
        "Dispensed earlier in the PMR but not at all in the window, most regularly dispensed first.",
    ),
    "usage_monthly": ("Usage monthly", "Scripts, quantity and distinct products for every month."),
    "usage_robot": (
        "Usage vs robot",
        "Robot dispensed per weekday against PMR scripts per weekday. Both count about one item "
        "per script, so they should be of the same order. A sanity check, not a reconciliation.",
    ),
}
PERCENT_LABELS = ("Share of scripts", "Change", "Cumulative share")
ACTIVITY_TABLES = {
    "activity_summary": ("Activity summary", "Robot activity over the usage window, per system."),
    "activity_daily": ("Activity daily", "Counts per day, with a 7-day average of dispensed."),
    "activity_changes": ("Activity changes", "Days whose counts changed between pulls."),
    "activity_pmr": (
        "Activity vs PMR",
        "Robot dispensed against the PMR total per day. A sanity check, not a reconciliation.",
    ),
}
CLASS_EXPLANATION = {
    "dead_stock": "In the robot with no usage in the period.",
    "overstocked": "Days of cover above the upper limit.",
    "understocked": "Days of cover below the lower limit.",
    "healthy": "Within both limits.",
}
NUMBER_FORMATS = {
    "Days of cover": "0.0",
    "Usage per day": "0.00",
    "Dispensed in period": "#,##0.##",
    "Stock qty": "#,##0.##",
    "Score": "0",
    "Earliest expiry": "yyyy-mm-dd",
    "Date": "yyyy-mm-dd",
    "First day": "yyyy-mm-dd",
    "Last day": "yyyy-mm-dd",
    "Busiest day": "yyyy-mm-dd",
    "Dispensed 7-day average": "0.0",
    "Average dispensed per day": "0.0",
    "Average per active day": "0.0",
    "PMR / robot": "0.00",
    "Month": "yyyy-mm",
    "Last dispensed": "yyyy-mm",
    "Quantity per day": "0.0",
    "Share of scripts": "0.0%",
    "Change": "0%",
    "Previous period quantity": "#,##0.##",
    "Robot per weekday": "0.0",
    "PMR scripts per weekday": "0.0",
    "Robot / PMR": "0.00",
}


@dataclass
class Report:
    tables: dict[str, pd.DataFrame]  # display tables keyed as in TABLES
    summary: pd.DataFrame  # Measure | Value, shown at the top of both outputs
    settings: pd.DataFrame  # Setting | Value | Status, enough to reproduce the run
    validation: pd.DataFrame  # Source | Check | Severity | Message | Rows
    match_rate: float
    matched: int
    total_items: int
    generated_at: str
    branches: list[str] = field(default_factory=list)
    pharmacy: str = ""  # the pharmacy tab this report belongs to, if any
    has_stock: bool = True  # False for a usage-only report (no stock file was uploaded)

    @property
    def counts(self) -> dict[str, int]:
        return {key: len(table) for key, table in self.tables.items()}


def branch_view(tables: dict[str, pd.DataFrame], branch: str | None) -> dict[str, pd.DataFrame]:
    """The tables for one branch. Rows with no branch (PMR-side review rows) always stay."""
    if not branch:
        return dict(tables)
    view = {}
    for key, table in tables.items():
        if "Branch" in table:
            own = table["Branch"].fillna("")
            table = table[(own == branch) | (own == "")]
        view[key] = table.reset_index(drop=True)
    return view


def check_no_patient_columns(tables: dict[str, pd.DataFrame]) -> None:
    """Refuse to build a report if any column header looks like a patient field."""
    for key, table in tables.items():
        for header in table.columns:
            text = normalise_header(header)
            if any(pattern in text for pattern in PATIENT_FIELD_PATTERNS):
                raise ValueError(f"Table '{key}' has a patient-like column: {header}")


def _display(table: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    shown = table.reindex(columns=list(columns)).rename(columns=columns)
    for label in ("Confirmed match", "Ambiguous"):
        if label in shown:
            shown[label] = shown[label].map(lambda value: "Yes" if value is True else "")
    for label in ("Match level", "File line"):
        if label in shown:
            shown[label] = shown[label].astype("Int64")
    return shown.reset_index(drop=True)


def _validation_table(issues: list[Issue], warnings: list[str]) -> pd.DataFrame:
    rows = []
    for issue in issues:
        message = issue.message
        if issue.check == "patient_fields":  # name no columns in the output
            message = "Patient-level column(s) were found in the upload and dropped before analysis"
        rows.append([issue.source, issue.check, issue.severity, message, len(issue.rows)])
    rows += [["analysis", "warning", "warning", text, 0] for text in warnings]
    return pd.DataFrame(rows, columns=["Source", "Check", "Severity", "Message", "Rows"])


def _fmt_date(value) -> str:
    return "" if pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def activity_tables(activity, comparison=None) -> dict[str, pd.DataFrame]:
    """Display tables for robot activity, shared by the report and the app."""
    tables = {
        "activity_summary": _display(activity.summary, ACTIVITY_SUMMARY_COLUMNS),
        "activity_daily": _display(activity.daily, ACTIVITY_DAILY_COLUMNS),
        "activity_changes": _display(activity.conflicts, ACTIVITY_CHANGES_COLUMNS),
    }
    if comparison is not None:
        tables["activity_pmr"] = _display(comparison[0], ACTIVITY_PMR_COLUMNS)
    return tables


def _as_month(column: pd.Series) -> pd.Series:
    return column.map(lambda v: "" if pd.isna(v) else pd.Timestamp(v).strftime("%Y-%m"))


SUGGEST_TABLES = {
    "suggest_abc": (
        "Sales groups",
        "Every dispensed product ranked by scripts, with its group: A fast movers (first 80% of "
        "scripts), B medium (to 95%), C slow movers. With a robot catalogue loaded, every table "
        "on the suggestions sheets only covers products in it.",
    ),
    "suggest_add": (
        "Add to robot",
        "A and B products the robot does not hold, fastest first. A suggestion for the people who arrange the robot: "
        "there is no slot data, so how many fit is their call.",
    ),
    "suggest_remove": (
        "Review for removal",
        "Robot items that are dead stock or slow movers, slowest first.",
    ),
    "suggest_increase": (
        "Stock up",
        "Fast movers (A) the robot holds but that are understocked.",
    ),
    "suggest_swaps": (
        "Swap pairs",
        "The slowest robot item beside the fastest missing product: a starting point, not an "
        "instruction. 0 scripts means the robot holds the item but none was dispensed in the "
        "window; the stock quantity shows how much is there.",
    ),
}
SUGGEST_LABELS = {
    "rank": "Rank",
    "product": "Product",
    "group": "Group",
    "scripts": "Scripts",
    "quantity": "Quantity",
    "share": "Share of scripts",
    "cumulative_share": "Cumulative share",
    "in_robot": "In robot",
    "trend": "Trend",
    "classification": "Stock class",
    "days_of_cover": "Days of cover",
    "why": "Why",
    "stock_qty": "Stock qty",
    "pair": "Pair",
    "move_out": "Move out of robot",
    "out_stock_qty": "In robot now (units)",
    "out_scripts": "Scripts dispensed (out)",
    "move_in": "Move into robot",
    "in_scripts": "Scripts (in)",
}


def suggest_tables(suggestions) -> dict[str, pd.DataFrame]:
    """Display tables for the robot suggestions, shared by the reports and the app."""
    sources = {
        "suggest_abc": suggestions.abc.drop(columns="pmr_key"),
        "suggest_add": suggestions.add,
        "suggest_remove": suggestions.remove,
        "suggest_increase": suggestions.increase,
        "suggest_swaps": suggestions.swaps,
    }
    return {
        key: _display(frame, {c: SUGGEST_LABELS[c] for c in frame.columns})
        for key, frame in sources.items()
    }


def usage_tables(usage) -> dict[str, pd.DataFrame]:
    """Display tables for the PMR usage analysis, shared by the reports and the app."""
    monthly_dates = usage.meta.get("date_basis") == "month"
    products = _display(usage.products, USAGE_PRODUCT_COLUMNS)
    stopped = _display(usage.stopped, USAGE_STOPPED_COLUMNS)
    for table in (products, stopped):
        if "Last dispensed" in table:  # a month-only PMR has no day, so show only the month
            table["Last dispensed"] = (
                _as_month(table["Last dispensed"])
                if monthly_dates
                else table["Last dispensed"].map(_fmt_date)
            )
    monthly = _display(usage.monthly, USAGE_MONTHLY_COLUMNS)
    monthly["Month"] = _as_month(monthly["Month"])
    robot = _display(usage.comparison, USAGE_ROBOT_COLUMNS)
    robot["Month"] = _as_month(robot["Month"])
    return {
        "usage_products": products,
        "usage_stopped": stopped,
        "usage_monthly": monthly,
        "usage_robot": robot,
    }


def _usage_summary_rows(usage) -> list[list]:
    meta = usage.meta
    return [
        ["Scripts in the usage window", meta["scripts"]],
        ["Products dispensed in the window", meta["products"]],
        ["Products that stopped being dispensed", meta["products_stopped"]],
    ]


def build_report(
    analysis: AnalysisResult,
    matching: MatchResult,
    validation: list[Issue] | None = None,
    generated_at: str | None = None,
    activity=None,
    comparison=None,
    pharmacy: str | None = None,
    usage=None,
    suggestions=None,
) -> Report:
    """Collect every result table into one Report. `generated_at` can be fixed for tests."""
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    tables = {
        **{cls: _display(analysis.by_class(cls), ITEM_COLUMNS) for cls in CLASSES},
        "review": _display(matching.review, REVIEW_COLUMNS),
        "unclassified": _display(analysis.unclassified, ITEM_COLUMNS),
        "usage_no_stock": _display(analysis.usage_no_stock, USAGE_NO_STOCK_COLUMNS),
    }
    if usage is not None:
        tables.update(usage_tables(usage))
    if suggestions is not None:
        tables.update(suggest_tables(suggestions))
    if activity is not None:
        tables.update(activity_tables(activity, comparison))
    check_no_patient_columns(tables)

    meta = analysis.meta
    matched = int(matching.status_counts.get("matched", 0))
    total = len(matching.matches)
    summary_rows = [
        *([["Pharmacy", pharmacy]] if pharmacy else []),
        ["Match rate", f"{matching.match_rate:.1%} ({matched} of {total} robot items)"],
        *[[CLASS_TITLES[cls], len(tables[cls])] for cls in CLASSES],
        ["Review list", len(tables["review"])],
        ["Unclassified", len(tables["unclassified"])],
        ["Usage with no stock", len(tables["usage_no_stock"])],
        *(_usage_summary_rows(usage) if usage is not None else []),
    ]
    if activity is not None and len(activity.daily):
        days = activity.daily["activity_date"]  # the days the data covers, not the nominal window
        summary_rows.append(
            [
                "Robot activity",
                f"{days.nunique()} day(s), {_fmt_date(days.min())} to {_fmt_date(days.max())}",
            ]
        )
    settings_rows = [
        [name, meta["settings"][name], meta["setting_labels"][name]] for name in meta["settings"]
    ] + [
        ["usage_window_start", _fmt_date(meta["period_start"]), "Derived from the PMR data"],
        ["usage_window_end", _fmt_date(meta["period_end"]), "Derived from the PMR data"],
        ["pmr_days_observed", meta["observed_days"], "Derived from the PMR data"],
        ["usage_date_basis", meta["date_basis"], "day, or month when the PMR gives only a month"],
        ["usage_rate_days", meta["rate_days"], "Days the usage rate is divided by"],
        ["pooled_across_branches", str(meta["pooled_across_branches"]), "Derived from the PMR"],
        ["usage_quantity_basis", ", ".join(meta["usage_basis"]), "reported = real quantities"],
        ["match_rate", f"{matching.match_rate:.4f}", "Derived"],
        ["generated_at", generated, ""],
    ]
    branches = sorted(
        {b for table in tables.values() if "Branch" in table for b in table["Branch"] if b}
    )
    return Report(
        tables=tables,
        summary=pd.DataFrame(summary_rows, columns=["Measure", "Value"]),
        settings=pd.DataFrame(settings_rows, columns=["Setting", "Value", "Status"]),
        validation=_validation_table(
            validation or [],
            list(
                dict.fromkeys(
                    [
                        *analysis.warnings,
                        *(usage.warnings if usage else []),
                        *(activity.warnings if activity else []),
                    ]
                )
            ),
        ),
        match_rate=matching.match_rate,
        matched=matched,
        total_items=total,
        generated_at=generated,
        branches=branches,
        pharmacy=pharmacy or "",
    )


def build_usage_report(
    usage,
    validation: list[Issue] | None = None,
    generated_at: str | None = None,
    activity=None,
    comparison=None,
    pharmacy: str | None = None,
    suggestions=None,
) -> Report:
    """A report with no stock classes, for a pharmacy that uploaded a PMR but no stock file.

    It holds the PMR usage analysis and, if given, the robot activity. The match rate is not
    shown because nothing was matched.
    """
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    tables = usage_tables(usage)
    if suggestions is not None:
        tables.update(suggest_tables(suggestions))
    if activity is not None:
        tables.update(activity_tables(activity, comparison))
    check_no_patient_columns(tables)

    meta = usage.meta
    summary_rows = [
        *([["Pharmacy", pharmacy]] if pharmacy else []),
        ["Stock", "Not classified: no stock file was uploaded"],
        *_usage_summary_rows(usage),
    ]
    if activity is not None and len(activity.daily):
        days = activity.daily["activity_date"]
        summary_rows.append(
            [
                "Robot activity",
                f"{days.nunique()} day(s), {_fmt_date(days.min())} to {_fmt_date(days.max())}",
            ]
        )
    settings_rows = [
        [name, meta["settings"][name], meta["setting_labels"][name]] for name in meta["settings"]
    ] + [
        ["usage_window_start", _fmt_date(meta["period_start"]), "Derived from the PMR data"],
        ["usage_window_end", _fmt_date(meta["period_end"]), "Derived from the PMR data"],
        ["pmr_days_observed", meta["observed_days"], "Derived from the PMR data"],
        ["usage_date_basis", meta["date_basis"], "day, or month when the PMR gives only a month"],
        ["usage_rate_days", meta["rate_days"], "Days the usage rate is divided by"],
        ["usage_quantity_basis", ", ".join(meta["qty_basis"]), "reported = real quantities"],
        ["trend_threshold", meta["trend_threshold"], "Assumption, to be confirmed"],
        ["trend_min_scripts", meta["trend_min_scripts"], "Assumption, to be confirmed"],
        ["generated_at", generated, ""],
    ]
    warnings = list(dict.fromkeys([*usage.warnings, *(activity.warnings if activity else [])]))
    return Report(
        tables=tables,
        summary=pd.DataFrame(summary_rows, columns=["Measure", "Value"]),
        settings=pd.DataFrame(settings_rows, columns=["Setting", "Value", "Status"]),
        validation=_validation_table(validation or [], warnings),
        match_rate=0.0,
        matched=0,
        total_items=0,
        generated_at=generated,
        branches=[],
        pharmacy=pharmacy or "",
        has_stock=False,
    )


# ---- Excel -------------------------------------------------------------------------------


def _style_sheet(sheet, frame: pd.DataFrame) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    if len(frame.columns):
        sheet.auto_filter.ref = sheet.dimensions
    for position, label in enumerate(frame.columns, start=1):
        letter = get_column_letter(position)
        widest = max([len(str(label))] + [len(str(v)) for v in frame.iloc[:, position - 1]])
        sheet.column_dimensions[letter].width = min(max(widest + 2, 10), 60)
        if label in NUMBER_FORMATS:
            for row in range(2, sheet.max_row + 1):
                sheet[f"{letter}{row}"].number_format = NUMBER_FORMATS[label]


def present_tables(report: Report) -> list[tuple[str, str, str]]:
    """(key, title, note) for every table this report holds, in output order."""
    return [
        (key, title, note)
        for key, (title, note) in {
            **TABLES,
            **USAGE_TABLES,
            **SUGGEST_TABLES,
            **ACTIVITY_TABLES,
        }.items()
        if key in report.tables
    ]


def to_excel_bytes(report: Report) -> bytes:
    """One sheet per class, then Review and the other result tables, Validation and Settings."""
    sheets: dict[str, pd.DataFrame] = {"Summary": report.summary}
    sheets.update({title: report.tables[key] for key, title, _ in present_tables(report)})
    sheets["Validation"] = report.validation
    sheets["Settings"] = report.settings
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl", datetime_format="yyyy-mm-dd") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            _style_sheet(writer.sheets[name], frame)
    return buffer.getvalue()


# ---- HTML --------------------------------------------------------------------------------

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robot stock audit{% if pharmacy %}: {{ pharmacy }}{% endif %}</title>
<style>
:root { --bg:#fff; --fg:#1b1f24; --muted:#5b6570; --line:#d6dbe1; --head:#eef1f4; --accent:#1f5fa8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14181d; --fg:#e6e9ed; --muted:#98a2ad; --line:#2d353e; --head:#1d242c; --accent:#6aa7e8; }
}
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.45 system-ui, sans-serif; }
main { max-width:1200px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:17px; margin:28px 0 4px; }
.muted { color:var(--muted); }
.rate { font-size:18px; margin:12px 0; }
.rate strong { color:var(--accent); }
.tiles { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }
.tile { border:1px solid var(--line); border-radius:6px; padding:8px 12px; min-width:110px; }
.tile b { display:block; font-size:20px; }
.filter { margin:12px 0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }
th { background:var(--head); position:sticky; top:0; white-space:nowrap; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.warn { margin:4px 0; }
.empty { color:var(--muted); font-style:italic; }
</style>
</head>
<body>
<main>
<h1>Robot stock audit{% if pharmacy %}: {{ pharmacy }}{% endif %}</h1>
<div class="muted">Static report generated {{ generated_at }}. It shows one finished run and
cannot re-run an audit.</div>
<div class="rate">{% if has_stock %}Match rate: <strong>{{ "%.1f"|format(match_rate * 100) }}%</strong>
({{ matched }} of {{ total_items }} robot items matched){% else %}Usage analysis only: no stock
file was uploaded, so stock is not classified.{% endif %}</div>

<div class="filter">
<label for="branch">Branch: </label>
<select id="branch">
<option value="">All branches</option>
{% for b in branches %}<option value="{{ b }}">{{ b }}</option>
{% endfor %}</select>
</div>

<div class="tiles">
{% for s in sections %}<div class="tile">{{ s.title }}<b data-count-for="tbl-{{ s.key }}">{{ s.rows|length }}</b></div>
{% endfor %}</div>

{% if report.validation_rows %}<h2>Validation messages</h2>
{% for v in report.validation_rows %}<div class="warn"><b>{{ v.severity }}</b> ({{ v.source }}): {{ v.message }}</div>
{% endfor %}{% endif %}

{% for s in sections %}<h2>{{ s.title }}</h2>
<div class="muted">{{ s.note }}</div>
<div class="scroll"><table class="data" id="tbl-{{ s.key }}">
<thead><tr>{% for h in s.headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
<tbody>
{% for r in s.rows %}<tr data-branch="{{ r.branch }}">{% for c in r.cells %}<td{% if c.num %} class="num"{% endif %}>{{ c.text }}</td>{% endfor %}</tr>
{% endfor %}</tbody></table></div>
{% if not s.rows %}<div class="empty">None.</div>{% endif %}
{% endfor %}

<h2>Settings used</h2>
<div class="muted">Enough to reproduce this report.</div>
<div class="scroll"><table class="data" id="tbl-settings">
<thead><tr>{% for h in settings.headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
<tbody>
{% for r in settings.rows %}<tr data-branch="">{% for c in r.cells %}<td>{{ c.text }}</td>{% endfor %}</tr>
{% endfor %}</tbody></table></div>
</main>
<script>
(function () {
  var select = document.getElementById("branch");
  function apply() {
    var wanted = select.value;
    document.querySelectorAll("table.data").forEach(function (table) {
      var shown = 0;
      table.querySelectorAll("tbody tr").forEach(function (row) {
        var own = row.getAttribute("data-branch");
        var visible = !wanted || !own || own === wanted;
        row.hidden = !visible;
        if (visible) { shown += 1; }
      });
      var counter = document.querySelector('[data-count-for="' + table.id + '"]');
      if (counter) { counter.textContent = shown; }
    });
  }
  select.addEventListener("change", apply);
})();
</script>
</body>
</html>
"""


def _cell(label: str, value) -> dict:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return {"text": "", "num": False}
    if isinstance(value, pd.Timestamp):
        return {"text": value.strftime("%Y-%m-%d"), "num": False}
    if isinstance(value, float | int) and not isinstance(value, bool):
        if label in PERCENT_LABELS:
            return {"text": f"{value:.1%}", "num": True}
        decimals = {
            "Days of cover": 1,
            "Usage per day": 2,
            "Score": 0,
            "Dispensed 7-day average": 1,
            "Average dispensed per day": 1,
            "Average per active day": 1,
            "PMR / robot": 2,
            "Quantity per day": 1,
            "Robot per weekday": 1,
            "PMR scripts per weekday": 1,
            "Robot / PMR": 2,
        }.get(label)
        if decimals is not None:
            return {"text": f"{value:,.{decimals}f}", "num": True}
        text = f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value):,}"
        return {"text": text, "num": True}
    return {"text": str(value), "num": False}


def _rows(frame: pd.DataFrame) -> list[dict]:
    return [
        {
            "branch": row.get("Branch", "") or "",
            "cells": [_cell(label, row[label]) for label in frame.columns],
        }
        for row in frame.to_dict("records")
    ]


def to_html(report: Report) -> str:
    """A single self-contained HTML file: inline CSS and JS, no external resources."""
    sections = []
    for key, title, note in present_tables(report):
        frame = report.tables[key]
        sections.append(
            {
                "key": key,
                "title": title,
                "note": note or CLASS_EXPLANATION.get(key, ""),
                "headers": list(frame.columns),
                "rows": _rows(frame),
            }
        )
    settings = {"headers": list(report.settings.columns), "rows": _rows(report.settings)}
    validation_rows = [
        {"severity": r["Severity"], "source": r["Source"] or "all", "message": r["Message"]}
        for r in report.validation.to_dict("records")
        if r["Severity"] != "info"
    ]
    environment = Environment(autoescape=True, trim_blocks=False, lstrip_blocks=False)
    return environment.from_string(_TEMPLATE).render(
        generated_at=report.generated_at,
        pharmacy=report.pharmacy,
        has_stock=report.has_stock,
        match_rate=report.match_rate,
        matched=report.matched,
        total_items=report.total_items,
        branches=report.branches,
        sections=sections,
        settings=settings,
        report={"validation_rows": validation_rows},
    )


_PRINT_CSS = (
    "body{font:12px/1.4 system-ui,sans-serif;color:#111;background:#fff;margin:12px}"
    "h1{font-size:18px;margin:0}h2{font-size:14px;margin:18px 0 2px}.note{margin:0 0 6px;color:#444}"
    "table{border-collapse:collapse;width:100%}th,td{border:1px solid #999;padding:3px 6px;"
    "text-align:left}th{background:#eee}td.n{text-align:right}thead{display:table-header-group}"
    "tr{page-break-inside:avoid}button{font:inherit;padding:6px 14px;margin:8px 0}"
    "@media print{button{display:none}}"
)


def printable_html(title: str, subtitle: str, sections: list[tuple[str, str, pd.DataFrame]]) -> str:
    """A print-friendly page: a Print button (hidden on paper) and the chosen tables.

    `sections` are (heading, note, table). Every value is escaped, because the page is shown
    in an iframe that can run scripts and the cells come from uploaded files.
    """
    check_no_patient_columns({heading: table for heading, _, table in sections})
    parts = []
    for heading, note, table in sections:
        parts.append(f"<h2>{escape(heading)} ({len(table):,})</h2><p class=note>{escape(note)}</p>")
        if table.empty:
            parts.append("<p>None.</p>")
            continue
        head = "".join(f"<th>{escape(str(label))}</th>" for label in table.columns)
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td{' class=n' if c['num'] else ''}>{escape(c['text'])}</td>" for c in r["cells"]
            )
            + "</tr>"
            for r in _rows(table)
        )
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    return (
        f"<!doctype html><meta charset=utf-8><style>{_PRINT_CSS}</style>"
        f"<h1>{escape(title)}</h1><p class=note>{escape(subtitle)}</p>"
        '<button onclick="window.print()">Print</button>' + "".join(parts)
    )
