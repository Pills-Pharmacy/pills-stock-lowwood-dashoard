"""Upload check: what a file gives the analysis, what it lacks, and what to do about it.

Runs the normal loader on one upload and turns the result into a checklist a person can act on:
every column the analysis wants, whether it was found (and as which header), why it matters when
missing, and, for a missing column, the unrecognised headers that look closest ("did you mean").
No cell values are ever put into the output, only header names and counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

import load
from config import DEFAULT_SETTINGS, Settings, column_guide, load_column_map, resolve_headers
from pipeline import merge_column_map
from schema import OPTIONAL, REQUIRED, normalise_header

SUGGESTION_THRESHOLD = 62
MAX_SUGGESTIONS = 3

# Extra words that signal a column even when its header shares no letters with the known headers
# (e.g. "Qty Out" for the quantity). Matched fuzzily, together with the headers in column_map.yaml.
HINT_WORDS: dict[str, tuple[str, ...]] = {
    "item_name": ("product", "drug", "description", "item", "medicine", "name"),
    "dispense_date": ("date", "dispensed", "dispensing", "supplied", "month"),
    "dispensed_qty": ("quantity", "qty", "units", "amount", "supplied", "issued", "out"),
    "stock_qty": ("quantity", "qty", "stock", "on hand", "count", "level", "balance"),
    "pack_size": ("pack", "size", "pack size"),
    "unit": ("unit", "uom", "form"),
    "item_code": ("code", "barcode", "sku", "id", "ean"),
    "generic_name": ("generic", "written", "prescribed"),
    "expiry_date": ("expiry", "expires", "exp"),
    "activity_date": ("date", "day"),
    "dispensed": ("dispensed", "picked", "out"),
}


@dataclass(frozen=True)
class ColumnStatus:
    internal: str
    required: bool
    found_as: str  # the file header it maps to; "" when not found
    guide: str  # plain-language description of the column and what it is for
    suggestions: tuple[str, ...] = ()  # unrecognised headers that look like it

    @property
    def found(self) -> bool:
        return bool(self.found_as)


@dataclass
class UploadCheck:
    source: str
    readable: bool = True
    rows_read: int = 0
    rows_used: int = 0
    columns: list[ColumnStatus] = field(default_factory=list)
    removed: dict[str, int] = field(default_factory=dict)  # why rows were left out -> count
    dropped_patient: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # headers with no mapping
    blockers: list[str] = field(default_factory=list)  # reasons the file cannot be analysed
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.readable and not self.blockers

    @property
    def missing_required(self) -> list[ColumnStatus]:
        return [c for c in self.columns if c.required and not c.found]

    @property
    def missing_optional(self) -> list[ColumnStatus]:
        return [c for c in self.columns if not c.required and not c.found]


def suggest_headers(internal: str, candidates: list[str], aliases: list[str]) -> tuple[str, ...]:
    """Unrecognised headers that look like column `internal`, best first (at most three)."""
    words = [normalise_header(a) for a in (*aliases, *HINT_WORDS.get(internal, ()))]
    scored = []
    for header in candidates:
        key = normalise_header(header)
        score = max((fuzz.WRatio(key, w) for w in words), default=0)
        if score >= SUGGESTION_THRESHOLD:
            scored.append((score, header))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(h.strip() for _, h in scored[:MAX_SUGGESTIONS])


_REMOVED_LABELS = {
    "marked_deleted": "deleted in the PMR",
    "not_dispensed": "never dispensed",
}


def check_upload(
    source: str,
    content: bytes,
    settings: Settings = DEFAULT_SETTINGS,
    column_map: dict | None = None,
    overrides: dict | None = None,
    pharmacy: str | None = None,
) -> UploadCheck:
    """Check one upload. `overrides` are the user's chosen headers ({source: {internal: header}})."""
    check = UploadCheck(source=source)
    base = column_map if column_map is not None else load_column_map(pharmacy=pharmacy)
    merged = merge_column_map(base, overrides)
    result = load.load_source(source, content, settings, merged, branch_supplied=True)

    check.dropped_patient = [h.strip() for h in result.dropped_patient_columns]
    for issue in result.issues:
        if issue.check == "unreadable_file":
            check.readable = False
            check.blockers.append(issue.message)
        elif issue.check == "inactive_rows":
            continue  # counted from the data below
        elif issue.severity == "error" and issue.check != "required_columns":
            check.blockers.append(issue.message)
        elif issue.severity == "warning" and issue.check != "patient_fields":
            check.warnings.append(issue.message)
    if not check.readable:
        return check

    mapped, unmapped = resolve_headers(source, result.raw_headers, merged)
    found_as = {}
    for header, internal in mapped.items():
        found_as.setdefault(internal, header.strip())
    for internal in (*REQUIRED[source], *OPTIONAL[source]):
        found = found_as.get(internal, "")
        aliases = merged[source].get(internal, [])
        check.columns.append(
            ColumnStatus(
                internal=internal,
                required=internal in REQUIRED[source],
                found_as=found,
                guide=column_guide(source, internal),
                suggestions=() if found else suggest_headers(internal, unmapped, aliases),
            )
        )
    check.ignored = [h.strip() for h in unmapped]

    if result.data is not None:
        check.rows_used = len(result.data)
        check.rows_read = check.rows_used + sum(
            int(i.message.split()[0]) for i in result.issues if i.check == "inactive_rows"
        )
        for issue in result.issues:
            if issue.check == "inactive_rows":
                flag = next((k for k in _REMOVED_LABELS if k in issue.message), "")
                check.removed[_REMOVED_LABELS.get(flag, "flagged")] = int(issue.message.split()[0])
    elif check.missing_required:
        names = ", ".join(c.internal for c in check.missing_required)
        check.blockers.append(f"Required column(s) not found: {names}")
    return check
