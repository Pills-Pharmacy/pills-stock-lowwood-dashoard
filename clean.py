"""Turn validated loads into the standard tables.

Works only with internal column names (load.py has already mapped the headers). No row is
dropped: odd values become blanks or NaN and stay traceable through `source_row`.
"""

from __future__ import annotations

import re
import unicodedata

import pandas as pd

from config import (
    NAME_ABBREVIATIONS,
    NAME_IGNORED_WORDS,
    NAME_PHRASES,
    ROBOT_REGISTRY,
    STOCK_QTY_IS_PACK_COUNT,
    UNIT_ALIASES,
    UNKNOWN_BRANCH,
)
from load import LoadResult, parse_dates
from schema import PMR_SOURCE, ROBOT_SOURCES, STANDARD_STOCK_COLUMNS, STANDARD_USAGE_COLUMNS

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = r"(?:mcg|μg|ug|mg|kg|g|ml|l|iu|units?|%)"
_TERM = rf"(?:{_NUM}\s*)?{_UNIT}"
# A strength expression: a number and unit, optionally followed by "/" terms whose number may be
# omitted (10mg/5ml, 30mg/500mg, 5mg/ml). A bare unit on its own ("/L") is never a strength.
_STRENGTH_RE = re.compile(rf"(?<![a-z0-9.,]){_NUM}\s*{_UNIT}(?:\s*/\s*{_TERM})*(?![a-z0-9])")
_TERM_RE = re.compile(rf"({_NUM})?\s*({_UNIT})")
_TO_MG = {"mg": 1.0, "g": 1000.0, "kg": 1_000_000.0, "mcg": 0.001, "μg": 0.001, "ug": 0.001}
_TO_ML = {"ml": 1.0, "l": 1000.0}


def clean_text(value: object) -> str:
    """Trim, collapse whitespace and normalise unicode (NFKC). Missing values become ''."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _fmt(number: float) -> str:
    return f"{round(number, 6):g}"


def _canonical_term(term: str) -> str:
    match = _TERM_RE.fullmatch(term.strip())
    number, unit = match.group(1), match.group(2)
    value = float(number.replace(",", ".")) if number else None
    if unit in _TO_MG:
        unit, scale = "mg", _TO_MG[unit]
    elif unit in _TO_ML:
        unit, scale = "ml", _TO_ML[unit]
    else:
        unit, scale = ("units" if unit.startswith("unit") else unit), 1.0
    return unit if value is None else f"{_fmt(value * scale)}{unit}"


def parse_strength(text: str) -> str:
    """First strength expression in `text` as a canonical string ('' if none).

    Mass is converted to mg and volume to ml, so 0.5g, 500 mg and 500MG all give '500mg', and
    100µg gives '0.1mg'. Compound strengths keep their parts: '10mg/5ml', '30mg/500mg'.
    """
    match = _STRENGTH_RE.search(clean_text(text).lower())
    if not match:
        return ""
    return "/".join(_canonical_term(t) for t in match.group(0).split("/"))


def normalise_name(text: str) -> str:
    """Matching form of a drug name: no strengths, salts or format words, abbreviations expanded."""
    lowered = _STRENGTH_RE.sub(" ", clean_text(text).lower())
    for phrase, replacement in NAME_PHRASES.items():
        lowered = re.sub(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", replacement, lowered)
    tokens = re.findall(r"[a-z0-9]+", lowered)
    words = [NAME_ABBREVIATIONS.get(t, t) for t in tokens]
    return " ".join(w for w in words if w not in NAME_IGNORED_WORDS)


def standard_unit(text: str) -> str:
    unit = clean_text(text).lower()
    return UNIT_ALIASES.get(unit, unit)


def _text(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series("", index=frame.index, dtype=object)
    return frame[column].map(clean_text)


def _number(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(float("nan"), index=frame.index)
    return pd.to_numeric(frame[column].str.strip(), errors="coerce").astype(float)


def _date(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    return parse_dates(frame[column])[0].dt.normalize()


def _item_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Columns shared by both tables: names, strength (column, else name), pack size, unit."""
    name = _text(frame, "item_name")
    from_column = _text(frame, "strength").map(parse_strength)
    from_name = name.map(parse_strength)
    strength = from_column.where(from_column != "", from_name)
    source = pd.Series("none", index=frame.index)
    source[from_name != ""] = "name"
    source[from_column != ""] = "column"
    return pd.DataFrame(
        {
            "item_code": _text(frame, "item_code"),
            "item_name": name,
            "name_norm": name.map(normalise_name),
            "strength_norm": strength,
            "strength_source": source,
            "pack_size": _number(frame, "pack_size"),
            "unit": _text(frame, "unit").map(standard_unit),
            # File line number, so results trace back to the export (header is line 1).
            "source_row": frame.index.to_series().astype(int) + 2,
        }
    )


def clean_stock(
    result: LoadResult, stock_is_packs: bool | None = None, branch: str | None = None
) -> pd.DataFrame:
    """Standard stock table for one robot export.

    `stock_is_packs` says the count is a number of packs, to be multiplied by pack size; None
    uses the config default. `branch` (a pharmacy label) overrides the branch of every row.
    """
    if stock_is_packs is None:
        stock_is_packs = STOCK_QTY_IS_PACK_COUNT
    robot_id = ROBOT_SOURCES[result.source]
    frame = result.data
    table = _item_columns(frame)
    table["robot_id"] = robot_id
    if branch:
        table["branch"] = branch
    elif "branch" in frame and (frame["branch"].str.strip() != "").all():
        table["branch"] = _text(frame, "branch")
    else:
        table["branch"] = ROBOT_REGISTRY[robot_id].branch
    table["stock_qty"] = _number(frame, "stock_qty")
    if stock_is_packs:  # packs -> units; rows with no usable pack size stay as exported
        packs = table["pack_size"].where(table["pack_size"] > 0, 1.0).fillna(1.0)
        table["stock_qty"] = table["stock_qty"] * packs
    table["expiry_date"] = _date(frame, "expiry_date")
    return table[list(STANDARD_STOCK_COLUMNS)].reset_index(drop=True)


def clean_usage(
    result: LoadResult, default_branch: str | None = None, branch: str | None = None
) -> pd.DataFrame:
    """Standard usage table for the PMR export. `branch` overrides the branch of every row.

    With no quantity column each row counts as one dispensing line (qty_basis 'line_count').
    """
    frame = result.data
    table = _item_columns(frame)
    table["generic_name"] = _text(frame, "generic_name")
    file_branch = _text(frame, "branch")
    table["branch"] = (
        branch if branch else file_branch.where(file_branch != "", default_branch or UNKNOWN_BRANCH)
    )
    if "dispensed_qty" in frame:
        table["dispensed_qty"] = _number(frame, "dispensed_qty")
        table["qty_basis"] = "reported"
    else:
        table["dispensed_qty"] = 1.0
        table["qty_basis"] = "line_count"
    dates, order, _ = parse_dates(frame["dispense_date"])
    table["dispense_date"] = dates.dt.normalize()
    table["date_basis"] = "month" if order == "year-month" else "day"
    return table[list(STANDARD_USAGE_COLUMNS)].reset_index(drop=True)


def clean(
    result: LoadResult,
    default_branch: str | None = None,
    stock_is_packs: bool | None = None,
    branch: str | None = None,
) -> pd.DataFrame:
    """Clean one load result into its standard table. Raises ValueError if the load stopped.

    `branch` (a pharmacy label) is written on every row, whatever the file says.
    """
    if result.stopped:
        raise ValueError(f"{result.source} did not load; fix the errors before cleaning")
    if result.source == PMR_SOURCE:
        return clean_usage(result, default_branch, branch)
    return clean_stock(result, stock_is_packs, branch)


def clean_all(
    results: dict[str, LoadResult],
    default_branch: str | None = None,
    stock_is_packs: bool | None = None,
    branch: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """(stock table for every robot that loaded, usage table or None). Stopped loads are skipped."""
    stock = [
        clean(r, stock_is_packs=stock_is_packs, branch=branch)
        for s, r in results.items()
        if s in ROBOT_SOURCES and not r.stopped
    ]
    pmr = results.get(PMR_SOURCE)
    usage = (
        clean(pmr, default_branch, branch=branch) if pmr is not None and not pmr.stopped else None
    )
    stock_table = (
        pd.concat(stock, ignore_index=True)
        if stock
        else pd.DataFrame(columns=list(STANDARD_STOCK_COLUMNS))
    )
    return stock_table, usage
