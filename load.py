"""Read an export, drop patient fields, map headers and run the validation checks.

Every check returns an `Issue` (check id, severity, message, affected rows). Row numbers are
file line numbers (header = line 1), so they can be quoted back to whoever owns the export.
Nothing here writes to disk, and no cell values are put into messages.
"""

from __future__ import annotations

import codecs
import collections
import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from config import (
    ACTIVITY_SYSTEM_ROBOT,
    DEFAULT_SETTINGS,
    EXPECTED_FILE_TYPE,
    FILE_TYPE_LABELS,
    FILE_TYPE_SIGNATURES,
    PACK_UNITS,
    PATIENT_FIELD_PATTERNS,
    ROBOT_REGISTRY,
    Settings,
    column_guide,
    load_column_map,
    resolve_headers,
)
from schema import (
    ACTIVITY_SOURCE,
    OPTIONAL,
    PMR_SOURCE,
    REQUIRED,
    ROBOT_SOURCES,
    normalise_header,
)

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass(frozen=True)
class Issue:
    check: str
    severity: str
    message: str
    rows: tuple[int, ...] = ()
    source: str = ""


@dataclass
class LoadResult:
    source: str
    encoding: str = ""
    delimiter: str = ","
    data: pd.DataFrame | None = None  # internal column names; None when loading had to stop
    issues: list[Issue] = field(default_factory=list)
    dropped_patient_columns: list[str] = field(default_factory=list)
    raw_headers: list[str] = field(default_factory=list)  # as read, patient columns excluded
    detected_type: str | None = None  # key of FILE_TYPE_SIGNATURES, None if unrecognised
    date_order: str = ""  # PMR only: "iso", "day-first", "month-first" or "year-month"
    layout: str = "table"  # "grouped" when a multi-line report layout was flattened

    @property
    def stopped(self) -> bool:
        return self.data is None

    def by_check(self, check: str) -> list[Issue]:
        return [i for i in self.issues if i.check == check]


DELIMITERS = (",", ";", "\t", "|")


def xlsx_to_csv_bytes(content: bytes) -> bytes:
    """Turn the first sheet of an .xlsx workbook into CSV bytes the normal loader can read.

    Values are written as text (dates as ISO, so no day/month ambiguity). Long numeric codes that
    Excel already stored as numbers cannot be restored: that damage is in the workbook itself.
    """
    import datetime as dt

    import openpyxl

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # corrupt or not a real workbook
        raise ValueError(f"not a readable Excel workbook ({type(exc).__name__})") from exc
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow([_xlsx_cell(v, dt) for v in row])
    finally:
        workbook.close()
    return buffer.getvalue().encode("utf-8")


def _xlsx_cell(value: object, dt) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def detect_delimiter(text: str) -> str:
    """Pick the delimiter that splits the header line into the most fields (comma on a tie).

    Only the first non-empty line is inspected, and quoted delimiters are not counted.
    """
    header = next((line for line in text.splitlines() if line.strip()), "")
    best, best_fields = ",", 1
    for delimiter in DELIMITERS:
        fields = len(next(csv.reader([header], delimiter=delimiter), []))
        if fields > best_fields:
            best, best_fields = delimiter, fields
    return best


def flatten_grouped(text: str, delimiter: str) -> pd.DataFrame | None:
    """Flatten a grouped report into one row per record, or None if the text is a plain table.

    Some report generators print each record over several lines: a line holding the first
    fields, optional lines carrying the rest of a wrapped text field, then a line that starts
    with a blank field and holds the remaining fields. The header still lists every column,
    so such a file read as a table is misaligned. It is recognised by its shape alone (two
    line lengths p and c, in equal numbers, with p + c - 1 equal to the header length and the
    second kind starting blank), so no header names are involved. The result carries the layout
    in `frame.attrs`: {"layout": "grouped", "unfit_lines": n}.
    """
    records = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter) if r]
    if len(records) < 3:
        return None
    header, body = records[0], records[1:]
    width = len(header)
    lengths = collections.Counter(len(r) for r in body)
    if width < 4 or lengths[width] >= 0.9 * len(body):
        return None  # an ordinary table
    top = [length for length, _ in lengths.most_common(2)]
    if len(top) < 2:
        return None
    parent_len, child_len = max(top), min(top)
    near_equal = abs(lengths[parent_len] - lengths[child_len]) <= 0.01 * len(body)
    if parent_len + child_len - 1 != width or child_len < 2 or not near_equal:
        return None
    children = [r for r in body if len(r) == child_len]
    if sum(1 for r in children if not r[0].strip()) < 0.99 * len(children):
        return None

    rows, parent, unfit = [], None, 0
    for record in body:
        if len(record) == parent_len:
            unfit += parent is not None  # a script that never got its closing line
            parent = list(record)
        elif len(record) == child_len and parent is not None:
            rows.append(parent + record[1:])
            parent = None
        elif len(record) == 1 and parent is not None:  # wrapped text of the last field
            parent[-1] = f"{parent[-1]} {record[0]}".strip()
        else:
            unfit += 1
    unfit += parent is not None  # the last script never got its closing line
    frame = pd.DataFrame(rows, columns=header, dtype=str)
    frame.attrs = {"layout": "grouped", "unfit_lines": unfit}
    return frame


def read_csv(content: bytes | str | Path) -> tuple[pd.DataFrame, str, str]:
    """Read a delimited export as text columns.

    Encoding: UTF-8 (BOM tolerated) first, then Windows-1252. Delimiter: detected from the
    header line. A grouped report layout is flattened first (see `flatten_grouped`). Returns
    (frame, encoding used, delimiter). Raises ValueError if neither encoding decodes.
    """
    raw = content if isinstance(content, bytes) else Path(content).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        text, encoding = raw[3:].decode("utf-8"), "utf-8 (with BOM)"
    else:
        try:
            text, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            try:
                text, encoding = raw.decode("cp1252"), "windows-1252"
            except UnicodeDecodeError as exc:
                raise ValueError("File is neither UTF-8 nor Windows-1252 text") from exc
    delimiter = detect_delimiter(text)
    grouped = flatten_grouped(text, delimiter)
    if grouped is not None:
        return grouped, encoding, delimiter
    frame = pd.read_csv(io.StringIO(text), sep=delimiter, dtype=str, keep_default_na=False)
    return frame, encoding, delimiter


def detect_patient_columns(
    headers: list[str], patterns: tuple[str, ...] | list[str] = PATIENT_FIELD_PATTERNS
) -> list[str]:
    """Headers whose normalised name contains any patient-field pattern."""
    return [h for h in headers if any(p in normalise_header(h) for p in patterns)]


def detect_file_type(headers: list[str]) -> str | None:
    """Which known file type these headers belong to, or None if none matches."""
    present = {normalise_header(h) for h in headers}
    for file_type, signature in FILE_TYPE_SIGNATURES.items():
        alternatives = (signature,) if isinstance(signature, frozenset) else signature
        if any(alt <= present for alt in alternatives):
            return file_type
    return None


def _rows(mask: pd.Series) -> tuple[int, ...]:
    return tuple(int(i) + 2 for i in mask[mask].index)


def _check_quantities(frame: pd.DataFrame, column: str, source: str) -> list[Issue]:
    text = frame[column].str.strip()
    number = pd.to_numeric(text, errors="coerce")
    missing, negative = number.isna(), number < 0
    issues = []
    if missing.any():
        issues.append(
            Issue(
                "negative_or_missing_qty",
                WARNING,
                f"{int(missing.sum())} row(s) have a blank or non-numeric {column}",
                _rows(missing),
                source,
            )
        )
    if negative.any():
        issues.append(
            Issue(
                "negative_or_missing_qty",
                WARNING,
                f"{int(negative.sum())} row(s) have a negative {column}",
                _rows(negative),
                source,
            )
        )
    return issues


def _item_key(frame: pd.DataFrame) -> pd.Series:
    names = frame["item_name"].str.strip().map(normalise_header)
    if "item_code" not in frame:
        return names
    codes = frame["item_code"].str.strip()
    return codes.where(codes != "", names)


def _check_duplicates(frame: pd.DataFrame, source: str) -> list[Issue]:
    """Robot exports should hold one row per (robot_id, item). robot_id is fixed per file."""
    key = _item_key(frame)
    repeated = key.duplicated(keep=False)
    if not repeated.any():
        return []
    check_cols = [c for c in ("stock_qty",) if c in frame]
    groups = frame[repeated].groupby(key[repeated])
    conflicting = sum(1 for _, g in groups if len(g[check_cols].drop_duplicates()) > 1)
    keys = key[repeated].nunique()
    note = f", {conflicting} with different quantities" if conflicting else ""
    return [
        Issue(
            "duplicates",
            ERROR if conflicting else WARNING,
            f"{keys} item(s) appear more than once{note}",
            _rows(repeated),
            source,
        )
    ]


def _check_branch(frame: pd.DataFrame, source: str) -> list[Issue]:
    if "branch" in frame:
        blank = frame["branch"].str.strip() == ""
        if blank.any():
            return [
                Issue(
                    "branch_present",
                    ERROR,
                    f"{int(blank.sum())} row(s) have no branch",
                    _rows(blank),
                    source,
                )
            ]
        return []
    if source == PMR_SOURCE:
        return [
            Issue(
                "branch_present",
                WARNING,
                "PMR export has no branch column, so usage cannot be split by branch",
                (),
                source,
            )
        ]
    robot_id = ROBOT_SOURCES[source]
    if robot_id not in ROBOT_REGISTRY:
        message = f"{robot_id} is not in the robot registry"
        return [Issue("branch_present", ERROR, message, (), source)]
    return []  # branch comes from the registry


_NUMERIC_DATE = r"^\s*(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})"
# A whole month with no day: "2026 04", "2026-04", "04/2026".
_MONTH_YEAR = r"^\s*(\d{4})\s*[ /.\-]\s*(\d{1,2})\s*$"
_YEAR_MONTH_REVERSED = r"^\s*(\d{1,2})\s*[ /.\-]\s*(\d{4})\s*$"


def parse_dates(values: pd.Series) -> tuple[pd.Series, str, str]:
    """Parse dates whose day/month order is not known in advance.

    Returns (dates, order, basis). order is "iso", "day-first", "month-first", or "year-month"
    for values that are whole months with no day (basis "month", dated the 1st). The order is
    certain when some value has a first or second part above 12. Otherwise it is guessed:
    the position with more distinct values is the day (a month rarely varies across a short
    export). A tie falls back to day-first. basis is "certain", "guessed" or "iso".
    """
    text = values.str.strip()
    filled = text[text != ""]
    for pattern, year_col, month_col in ((_MONTH_YEAR, 0, 1), (_YEAR_MONTH_REVERSED, 1, 0)):
        found = filled.str.extract(pattern)
        if len(filled) and found.notna().all().all():
            numbers = found.apply(pd.to_numeric)
            months = pd.to_datetime(
                {"year": numbers[year_col], "month": numbers[month_col], "day": 1},
                errors="coerce",
            ).reindex(text.index)
            return months, "year-month", "month"
    parts = text.str.extract(_NUMERIC_DATE).apply(pd.to_numeric, errors="coerce")
    if parts[0].notna().any() and (parts[0].dropna() > 31).all():
        return pd.to_datetime(text, format="mixed", errors="coerce"), "iso", "iso"
    first, second = parts[0].dropna(), parts[1].dropna()
    if (first > 12).any() and not (second > 12).any():
        order, basis = "day-first", "certain"
    elif (second > 12).any() and not (first > 12).any():
        order, basis = "month-first", "certain"
    elif first.nunique() > second.nunique():
        order, basis = "day-first", "guessed"
    elif second.nunique() > first.nunique():
        order, basis = "month-first", "guessed"
    else:
        order, basis = "day-first", "guessed"
    dates = pd.to_datetime(text, format="mixed", dayfirst=order == "day-first", errors="coerce")
    return dates, order, basis


def _check_dates(
    frame: pd.DataFrame, settings: Settings, source: str, result: LoadResult | None = None
) -> list[Issue]:
    dates, order, basis = parse_dates(frame["dispense_date"])
    issues = []
    if result is not None:
        result.date_order = order
    if order == "year-month":
        issues.append(
            Issue(
                "date_format",
                INFO,
                "Dates are whole months with no day; usage is counted by month",
                (),
                source,
            )
        )
    elif order != "iso":
        guessed = basis == "guessed"
        issues.append(
            Issue(
                "date_format",
                WARNING if guessed else INFO,
                f"Dates read as {order} ("
                + (
                    "guessed from how the values vary; confirm with the PMR owner)"
                    if guessed
                    else "a value above 12 settles it)"
                ),
                (),
                source,
            )
        )
    bad = dates.isna()
    if bad.any():
        issues.append(
            Issue(
                "pmr_date_range",
                ERROR,
                f"{int(bad.sum())} row(s) have a blank or unreadable dispense date",
                _rows(bad),
                source,
            )
        )
    if dates.notna().any():
        first, last = dates.min().normalize(), dates.max().normalize()
        if order == "year-month":  # the last month runs to its end
            last = last + pd.offsets.MonthEnd(0)
        span = (last - first).days + 1
        wanted = settings.period_days.value
        if span < wanted:
            issues.append(
                Issue(
                    "pmr_date_range",
                    WARNING,
                    f"PMR data covers {span} days ({first:%Y-%m-%d} to {last:%Y-%m-%d}); "
                    f"the period is {wanted} days, so usage rates will be understated",
                    (),
                    source,
                )
            )
    return issues


def _check_units(frame: pd.DataFrame, source: str) -> list[Issue]:
    if "unit" not in frame:
        return []
    packed = frame["unit"].str.strip().str.lower().isin(PACK_UNITS)
    if not packed.any():
        return []
    return [
        Issue(
            "unit_mismatch",
            WARNING,
            f"{int(packed.sum())} row(s) are counted in packs, not single units, "
            "which will not line up with PMR dispensing",
            _rows(packed),
            source,
        )
    ]


ACTIVITY_COUNTS = ("dispensed", "stocked", "rearranged")


def _check_activity(
    frame: pd.DataFrame, settings: Settings, source: str, result: LoadResult
) -> list[Issue]:
    """Checks for a robot activity-statistics file (one row per day per system)."""
    issues = []
    for column in ACTIVITY_COUNTS:
        if column in frame:
            issues += _check_quantities(frame, column, source)

    dates, order, basis = parse_dates(frame["activity_date"])
    result.date_order = order
    if order != "iso":
        guessed = basis == "guessed"
        how = (
            "guessed from how the values vary; confirm"
            if guessed
            else "a value above 12 settles it"
        )
        message = f"Dates read as {order} ({how})"
        issues.append(Issue("date_format", WARNING if guessed else INFO, message, (), source))
    unreadable = dates.isna()
    if unreadable.any():
        issues.append(
            Issue(
                "activity_dates",
                ERROR,
                f"{int(unreadable.sum())} row(s) have a blank or unreadable date",
                _rows(unreadable),
                source,
            )
        )

    day = dates.dt.normalize()
    system = (
        frame["system_name"].str.strip()
        if "system_name" in frame
        else pd.Series("", index=frame.index)
    )
    keyed = frame.assign(_system=system, _day=day)
    repeated = keyed.duplicated(["_system", "_day"], keep=False) & day.notna()
    if repeated.any():
        counts = [c for c in ACTIVITY_COUNTS if c in frame]
        groups = keyed[repeated].groupby(["_system", "_day"])
        conflicting = int((groups[counts].nunique() > 1).any(axis=1).sum())
        note = f", {conflicting} with different counts" if conflicting else ""
        issues.append(
            Issue(
                "duplicates",
                ERROR if conflicting else WARNING,
                f"{groups.ngroups} day(s) appear more than once{note}",
                _rows(repeated),
                source,
            )
        )

    for name, days in keyed[day.notna()].groupby("_system")["_day"]:
        first, last = days.min(), days.max()
        missing = len(pd.date_range(first, last).difference(pd.DatetimeIndex(days.unique())))
        if missing:
            label = name or "file"
            issues.append(
                Issue(
                    "missing_days",
                    WARNING,
                    f"{label}: {missing} day(s) missing between "
                    f"{first:%Y-%m-%d} and {last:%Y-%m-%d}",
                    (),
                    source,
                )
            )

    if "period_hours" in frame:
        hours = pd.to_numeric(frame["period_hours"].str.strip(), errors="coerce")
        odd = hours.isna() | (hours != 24)
        if odd.any():
            issues.append(
                Issue(
                    "period_hours",
                    WARNING,
                    f"{int(odd.sum())} row(s) cover a period other than 24 hours; daily figures "
                    "assume 24",
                    _rows(odd),
                    source,
                )
            )

    if day.notna().any():
        span = int((day.max() - day.min()).days + 1)
        wanted = settings.period_days.value
        if span < wanted:
            issues.append(
                Issue(
                    "activity_range",
                    WARNING,
                    f"Activity data covers {span} days ({day.min():%Y-%m-%d} to "
                    f"{day.max():%Y-%m-%d}); the period is {wanted} days, so totals cover only "
                    "part of it",
                    (),
                    source,
                )
            )

    if "system_name" in frame:
        unknown = sorted({s for s in system if s and s.lower() not in ACTIVITY_SYSTEM_ROBOT})
        if unknown:
            issues.append(
                Issue(
                    "system_registry",
                    WARNING,
                    f"System name(s) not in the robot registry, so their branch is unknown: "
                    f"{', '.join(unknown)}",
                    (),
                    source,
                )
            )
    return issues


_TRUE_VALUES = {"1", "true", "t", "yes", "y"}


def drop_inactive_pmr_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Remove PMR rows flagged deleted or not dispensed. The index is kept, so source_row still
    points at the right line of the export. Returns (frame, {flag: rows removed})."""
    dropped: dict[str, int] = {}
    keep = pd.Series(True, index=frame.index)
    for column in ("marked_deleted", "not_dispensed"):
        if column in frame:
            flagged = frame[column].astype(str).str.strip().str.lower().isin(_TRUE_VALUES)
            flagged &= keep
            if flagged.any():
                dropped[column] = int(flagged.sum())
            keep &= ~flagged
    return frame[keep], dropped


def load_source(
    source: str,
    content: bytes | str | Path,
    settings: Settings = DEFAULT_SETTINGS,
    column_map: dict | None = None,
    branch_supplied: bool = False,
) -> LoadResult:
    """Read one export and validate it. `result.data` is None if loading had to stop.

    `branch_supplied` says the caller (a pharmacy tab) provides the branch, so the files are not
    expected to carry one and that check is skipped.
    """
    result = LoadResult(source=source)
    try:
        frame, result.encoding, result.delimiter = read_csv(content)
    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        message = f"Cannot read file: {exc}"
        result.issues.append(Issue("unreadable_file", ERROR, message, (), source))
        return result
    shown = {"\t": "tab"}.get(result.delimiter, result.delimiter)
    message = f"Read as {result.encoding}, delimiter '{shown}'"
    result.issues.append(Issue("encoding", INFO, message, (), source))

    result.layout = frame.attrs.get("layout", "table")
    if result.layout == "grouped":
        result.issues.append(
            Issue(
                "layout",
                INFO,
                "Read as a grouped report: each record spans several lines and was flattened",
                (),
                source,
            )
        )
        skipped = int(frame.attrs.get("unfit_lines", 0))
        if skipped:
            result.issues.append(
                Issue(
                    "layout",
                    WARNING,
                    f"{skipped} line(s) did not fit the grouped layout and were skipped",
                    (),
                    source,
                )
            )

    patient = detect_patient_columns(list(frame.columns))
    if patient:  # dropped before anything else touches the data
        frame = frame.drop(columns=patient)
        result.dropped_patient_columns = patient
        result.issues.append(
            Issue(
                "patient_fields",
                WARNING,
                f"Patient column(s) found and dropped: {', '.join(p.strip() for p in patient)}",
                (),
                source,
            )
        )
    result.raw_headers = list(frame.columns)

    result.detected_type = detect_file_type(result.raw_headers)
    expected = EXPECTED_FILE_TYPE[source]
    if result.detected_type and result.detected_type != expected:
        message = (
            f"This looks like an {FILE_TYPE_LABELS[result.detected_type]} file, "
            f"but a {FILE_TYPE_LABELS[expected]} file was expected here. Cannot continue."
        )
        result.issues.append(Issue("wrong_file_type", ERROR, message, (), source))
        return result

    column_map = column_map if column_map is not None else load_column_map()
    mapped, unmapped = resolve_headers(source, result.raw_headers, column_map)
    seen: dict[str, str] = {}
    for raw, internal in list(mapped.items()):
        if internal in seen:
            result.issues.append(
                Issue(
                    "ambiguous_columns",
                    WARNING,
                    f"'{raw.strip()}' and '{seen[internal].strip()}' both map to {internal}; "
                    "using the first",
                    (),
                    source,
                )
            )
            del mapped[raw]
        else:
            seen[internal] = raw
    if unmapped:
        result.issues.append(
            Issue(
                "unmapped_columns",
                INFO,
                f"Ignored column(s) with no mapping: {', '.join(u.strip() for u in unmapped)}",
                (),
                source,
            )
        )

    missing = [c for c in REQUIRED[source] if c not in mapped.values()]
    if missing:
        need = "; ".join(f"{c}: {column_guide(source, c)}" for c in missing)
        accepted = column_map.get(source, {})
        hints = "; ".join(
            f"{c} can be headed {', '.join(repr(a) for a in accepted.get(c, [])[:4])}"
            for c in missing
        )
        result.issues.append(
            Issue(
                "required_columns",
                ERROR,
                f"Required column(s) missing: {', '.join(missing)}. Please re-export with: "
                f"{need}. ({hints}.) Cannot continue.",
                (),
                source,
            )
        )
        return result

    absent = [c for c in OPTIONAL[source] if c not in mapped.values()]
    if absent:  # not an error: tells the user what extra columns would improve the analysis
        advice = "; ".join(f"{c}: {column_guide(source, c)}" for c in absent)
        result.issues.append(
            Issue(
                "optional_columns",
                INFO,
                f"Not found (the analysis still runs, but these would improve it): {advice}",
                (),
                source,
            )
        )

    frame = frame[list(mapped)].rename(columns=mapped)
    if source == PMR_SOURCE:
        frame, dropped = drop_inactive_pmr_rows(frame)
        for flag, count in dropped.items():
            result.issues.append(
                Issue(
                    "inactive_rows",
                    INFO,
                    f"{count} row(s) flagged {flag} were removed (they are not real usage)",
                    (),
                    source,
                )
            )
    if frame.empty:
        result.issues.append(Issue("empty_file", ERROR, "File has no data rows", (), source))
        return result
    result.data = frame

    if source == ACTIVITY_SOURCE:
        result.issues += _check_activity(frame, settings, source, result)
        return result

    qty = "dispensed_qty" if source == PMR_SOURCE else "stock_qty"
    if qty in frame:
        result.issues += _check_quantities(frame, qty, source)
    else:
        result.issues.append(
            Issue(
                "no_quantity",
                WARNING,
                "PMR export has no quantity column; each row will count as one dispensing "
                "line, which understates or overstates units used",
                (),
                source,
            )
        )
    if source != PMR_SOURCE:
        result.issues += _check_duplicates(frame, source)
    if not branch_supplied:
        result.issues += _check_branch(frame, source)
    if source == PMR_SOURCE:
        result.issues += _check_dates(frame, settings, source, result)
    result.issues += _check_units(frame, source)
    return result


def validate_across(results: dict[str, LoadResult]) -> list[Issue]:
    """Checks that need more than one export: header agreement and unit agreement."""
    issues = []
    robots = [r for s, r in results.items() if s in ROBOT_SOURCES and r.raw_headers]
    if len(robots) == 2:
        first, second = ({normalise_header(h) for h in r.raw_headers} for r in robots)
        if first != second:
            only_first = sorted(first - second)
            only_second = sorted(second - first)
            issues.append(
                Issue(
                    "same_headers",
                    WARNING,
                    "Robot exports have different headers "
                    f"(only in {robots[0].source}: {', '.join(only_first) or 'none'}; "
                    f"only in {robots[1].source}: {', '.join(only_second) or 'none'})",
                )
            )

    pmr = results.get(PMR_SOURCE)
    if pmr is not None and not pmr.stopped and "unit" in pmr.data and "item_code" in pmr.data:
        issues += _unit_mismatches_by_code(results, pmr)
    return issues


def _unit_mismatches_by_code(results: dict[str, LoadResult], pmr: LoadResult) -> list[Issue]:
    """Compare units where robot and PMR share an item code. Name matching is M5's job."""
    pmr_units = (
        pmr.data.assign(code=pmr.data["item_code"].str.strip(), unit=pmr.data["unit"].str.strip())
        .query("code != ''")
        .groupby("code")["unit"]
        .agg(lambda s: set(s.str.lower()))
    )
    issues = []
    for source, result in results.items():
        if source not in ROBOT_SOURCES or result.stopped:
            continue
        if not {"item_code", "unit"} <= set(result.data.columns):
            continue
        frame = result.data
        codes, units = frame["item_code"].str.strip(), frame["unit"].str.strip().str.lower()
        mismatch = pd.Series(
            [c in pmr_units.index and u not in pmr_units[c] for c, u in zip(codes, units)],
            index=frame.index,
        )
        if mismatch.any():
            issues.append(
                Issue(
                    "unit_mismatch",
                    WARNING,
                    f"{int(mismatch.sum())} item(s) use a different unit in PMR than in the robot",
                    _rows(mismatch),
                    source,
                )
            )
    return issues
