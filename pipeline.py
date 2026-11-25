"""Run the whole audit in memory: load, clean, match, analyse, report.

This is the one place the stages are chained, so the Streamlit page stays a thin layer over
it and the chain can be tested without a browser. Nothing is written to disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

import activity
import analyse
import clean
import export
import load
import match
import suggest
import usage as usage_analysis
from config import (
    DEFAULT_SETTINGS,
    Settings,
    load_column_map,
    pharmacy_by_key,
    resolve_headers,
)
from schema import ACTIVITY_SOURCE, OPTIONAL, REQUIRED, ROBOT_SOURCES, normalise_header

log = logging.getLogger(__name__)

PMR = "pmr"


@dataclass
class PipelineResult:
    loads: dict[str, load.LoadResult] = field(default_factory=dict)
    issues: list[load.Issue] = field(default_factory=list)  # every load and cross-file issue
    errors: list[str] = field(default_factory=list)  # reasons the analysis could not run
    matching: match.MatchResult | None = None
    analysis: analyse.AnalysisResult | None = None
    report: export.Report | None = None
    activity_loads: list[load.LoadResult] = field(default_factory=list)
    activity_errors: list[str] = field(default_factory=list)
    activity: activity.ActivityResult | None = None
    comparison: tuple[pd.DataFrame, dict] | None = None  # robot activity vs PMR, by day
    usage: usage_analysis.UsageResult | None = None  # what the PMR says was dispensed
    usage_errors: list[str] = field(default_factory=list)
    suggestions: suggest.Suggestions | None = None  # robot stocking suggestions (ABC)

    @property
    def ran(self) -> bool:
        """The full stock audit ran (a report with stock classes exists)."""
        return self.report is not None and self.report.has_stock

    @property
    def activity_ran(self) -> bool:
        return self.activity is not None

    @property
    def usage_ran(self) -> bool:
        return self.usage is not None

    @property
    def usage_only(self) -> bool:
        """A PMR was analysed but no stock file was uploaded, so stock could not be classified."""
        return self.report is not None and not self.report.has_stock and not self.errors


def merge_column_map(base: dict, overrides: dict | None) -> dict:
    """Add user-chosen headers to the column map. A chosen header wins over any older mapping."""
    merged = {source: {k: list(v) for k, v in block.items()} for source, block in base.items()}
    for source, chosen in (overrides or {}).items():
        block = merged[source]
        for internal, header in chosen.items():
            key = normalise_header(header)
            for other, aliases in block.items():
                block[other] = [a for a in aliases if normalise_header(a) != key]
            block.setdefault(internal, []).insert(0, header)
    return merged


def file_headers(content: bytes) -> list[str]:
    """Headers of an upload, minus any that look like patient fields (never offered for mapping)."""
    try:
        frame, _, _ = load.read_csv(content)
    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return []
    patient = set(load.detect_patient_columns(list(frame.columns)))
    return [str(h) for h in frame.columns if h not in patient]


def mapping_status(source: str, content: bytes, column_map: dict | None = None) -> dict[str, str]:
    """{internal column: the header it currently maps to, or ''} for the mapping screen."""
    headers = file_headers(content)
    mapped, _ = resolve_headers(source, headers, column_map)
    by_internal = {internal: header for header, internal in mapped.items()}
    return {
        internal: by_internal.get(internal, "")
        for internal in (*REQUIRED[source], *OPTIONAL[source])
    }


def run_pipeline(
    files: dict[str, bytes],
    settings: Settings = DEFAULT_SETTINGS,
    overrides: dict | None = None,
    use_code_match: bool = False,
    confirmed: pd.DataFrame | None = None,
    default_branch: str | None = None,
    generated_at: str | None = None,
    stock_is_packs: bool | None = None,
    activity_files: list[bytes] | None = None,
    pharmacy: str | None = None,
    include_latest_month: bool = False,
    catalogue=None,
) -> PipelineResult:
    """Analyse uploaded files (keyed by source name). Problems are returned, not raised.

    `activity_files` are robot activity-statistics pulls (several allowed). They are analysed on
    their own, so they work without stock or PMR files and never block the stock audit.

    `pharmacy` is a key from config.PHARMACIES. It supplies that pharmacy's own headers
    (column_map.yaml) and its label becomes the branch on every row, so no branch column is
    needed in any file.
    """
    result = PipelineResult()
    problems = settings.problems()
    if problems:
        result.errors += [f"Criteria: {p}" for p in problems]
        return result

    owner = pharmacy_by_key(pharmacy) if pharmacy else None
    branch = owner.label if owner else None
    column_map = merge_column_map(load_column_map(pharmacy=pharmacy), overrides)
    for source, content in files.items():
        result.loads[source] = load.load_source(
            source, content, settings, column_map, branch_supplied=branch is not None
        )
        result.issues += result.loads[source].issues
    result.issues += load.validate_across(result.loads)

    if activity_files:
        _run_activity(result, activity_files, settings, column_map, branch)
        if not files:  # activity-only run: nothing to complain about on the stock side
            return result

    pmr_ok = PMR in result.loads and not result.loads[PMR].stopped
    stock_uploaded = any(source in result.loads for source in ROBOT_SOURCES)
    if pmr_ok and not stock_uploaded:
        # No stock file at all: stock cannot be classified, but what the PMR says was dispensed
        # can still be shown. This is a result, not an error.
        try:
            table = clean.clean(result.loads[PMR], default_branch, branch=branch)
            _run_usage(result, table, settings, include_latest_month)
            if result.activity is not None:
                result.comparison = activity.compare_with_pmr(result.activity.daily, table)
            result.suggestions = suggest.build(result.usage)
            result.report = export.build_usage_report(
                result.usage,
                result.issues,
                generated_at,
                result.activity,
                result.comparison,
                branch,
                result.suggestions,
            )
        except Exception as exc:
            log.exception("Usage analysis failed")
            result.usage = result.report = None
            result.errors.append(f"The usage analysis failed unexpectedly ({type(exc).__name__}).")
        return result

    robots_ok = [s for s in ROBOT_SOURCES if s in result.loads and not result.loads[s].stopped]
    if not robots_ok:
        result.errors.append("No robot stock file could be read. Upload one and fix any errors.")
    if not pmr_ok:
        result.errors.append("No PMR usage file could be read. Upload one and fix any errors.")
    if result.errors:
        return result

    try:
        stock, usage = clean.clean_all(result.loads, default_branch, stock_is_packs, branch)
        result.matching = match.match_items(stock, usage, use_code_match, confirmed)
        _run_usage(result, usage, settings, include_latest_month)
        result.analysis = analyse.analyse(
            result.matching, settings, include_latest_month=include_latest_month
        )
        if result.activity is not None:
            result.comparison = activity.compare_with_pmr(
                result.activity.daily, result.matching.usage
            )
        result.suggestions = suggest.build(result.usage, result.analysis, catalogue)
        result.report = export.build_report(
            result.analysis,
            result.matching,
            result.issues,
            generated_at,
            result.activity,
            result.comparison,
            branch,
            result.usage,
            result.suggestions,
        )
    except Exception as exc:
        log.exception("Pipeline failed")
        result.matching = result.analysis = result.usage = None
        result.errors.append(f"The analysis failed unexpectedly ({type(exc).__name__}).")
    return result


def _run_usage(
    result: PipelineResult, table: pd.DataFrame, settings, include_latest_month: bool
) -> None:
    """Summarise the cleaned PMR into `result.usage`."""
    daily = result.activity.daily if result.activity is not None else None
    result.usage = usage_analysis.analyse_usage(
        table, settings, activity_daily=daily, include_latest_month=include_latest_month
    )


def _run_activity(
    result: PipelineResult, contents: list[bytes], settings, column_map, branch: str | None = None
) -> None:
    """Load, merge and analyse activity pulls into `result`. Problems are recorded, not raised."""
    seen = set()
    for content in contents:
        loaded = load.load_source(ACTIVITY_SOURCE, content, settings, column_map)
        result.activity_loads.append(loaded)
        for issue in loaded.issues:
            # Each pull covers its own few days, so the merged analysis reports coverage once.
            key = (issue.check, issue.severity, issue.message)
            if issue.check != "activity_range" and key not in seen:
                seen.add(key)
                result.issues.append(issue)
    try:
        result.activity = activity.run_activity(result.activity_loads, settings, branch=branch)
    except Exception as exc:
        log.exception("Activity analysis failed")
        result.activity_errors.append(
            f"The activity analysis failed unexpectedly ({type(exc).__name__})."
        )
        return
    if result.activity is None:
        result.activity_errors.append(
            "No robot activity file could be read. Upload one and fix any errors."
        )


def branch_match_rate(matching: match.MatchResult, branch: str | None) -> tuple[float, int, int]:
    """(rate, matched, total) for one branch, or for everything when branch is empty."""
    rows = matching.matches
    if branch:
        rows = rows[rows["branch"] == branch]
    matched = int((rows["match_status"] == match.MATCHED).sum())
    return (matched / len(rows) if len(rows) else 0.0), matched, len(rows)


def bulk_blocked_reason(analysis: analyse.AnalysisResult) -> str | None:
    """Why a bulk 'not in the PMR' decision is unsafe right now, or None when it is allowed.

    An item missing from a PMR that is shorter than the usage period may simply not have been
    dispensed in the days we can see, so it must not be marked as unused in bulk.
    """
    meta = analysis.meta
    if meta["observed_days"] < meta["period_days"]:
        return (
            f"The PMR covers {meta['observed_days']} day(s) but the usage period is "
            f"{meta['period_days']} days. An item missing from a short PMR is not necessarily "
            "unused, so bulk marking is switched off. Upload a PMR that covers the whole "
            "period, or set the usage window to the days the PMR covers."
        )
    return None
