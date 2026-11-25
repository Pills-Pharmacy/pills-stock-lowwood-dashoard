"""Link robot stock items to PMR products and build the review list.

Levels, in order of reliability. Every result carries one:
  1  exact product code (built, off by default until a shared code is confirmed)
  2  normalised key: same name and strength after cleaning (accepted automatically)
  3  fuzzy name with the same strength (review list only, never accepted automatically)
  4  no match (review list)

Pack size is not part of the key: a robot pack and a dispensing pack of the same medicine
usually differ (see OPEN_QUESTIONS.md). A person's confirmed decisions are kept in a lookup
file so the same item is not asked about twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from config import AMBIGUITY_MARGIN, CONFIRMED_MATCHES_PATH, FUZZY_THRESHOLD, MAX_CANDIDATES

LEVEL_CODE, LEVEL_KEY, LEVEL_FUZZY, LEVEL_NONE = 1, 2, 3, 4
MATCHED, REVIEW, NO_MATCH = "matched", "review", "no_match"
DECISIONS = ("match", "no_match")
CONFIRMED_COLUMNS = ("stock_key", "pmr_key", "decision", "note", "confirmed_on")

MATCH_COLUMNS = (
    "robot_id",
    "branch",
    "item_code",
    "item_name",
    "name_norm",
    "strength_norm",
    "pack_size",
    "unit",
    "stock_qty",
    "expiry_date",
    "stock_key",
    "match_level",
    "match_status",
    "pmr_key",
    "candidate_name",
    "candidate_score",
    "candidates",
    "ambiguous",
    "confirmed",
    "reason",
    "source_row",
)
REVIEW_COLUMNS = (
    "side",
    "robot_id",
    "branch",
    "item_name",
    "name_norm",
    "strength_norm",
    "stock_key",
    "match_level",
    "reason",
    "candidate_name",
    "candidate_score",
    "candidates",
    "ambiguous",
    "source_row",
)


@dataclass
class MatchResult:
    matches: pd.DataFrame  # one row per stock row, in MATCH_COLUMNS
    review: pd.DataFrame  # stock rows needing a person, plus PMR products no robot item matched
    usage: pd.DataFrame  # usage table plus pmr_key and matched_to_robot
    match_rate: float  # matched stock rows / all stock rows (0.0 when there is no stock)
    level_counts: dict[int, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)


def make_key(name_norm: str, strength_norm: str) -> str:
    return f"{name_norm}|{strength_norm}"


# ---- confirmed-match lookup --------------------------------------------------------------


def empty_confirmed() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in CONFIRMED_COLUMNS})


def read_confirmed(path: Path | str = CONFIRMED_MATCHES_PATH) -> pd.DataFrame:
    """Load confirmed decisions. A missing file means none yet."""
    path = Path(path)
    if not path.exists():
        return empty_confirmed()
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(CONFIRMED_COLUMNS) - set(table.columns)
    if missing:
        raise ValueError(f"{path.name} is missing column(s): {', '.join(sorted(missing))}")
    return table[list(CONFIRMED_COLUMNS)].astype(object)


def write_confirmed(table: pd.DataFrame, path: Path | str = CONFIRMED_MATCHES_PATH) -> None:
    table[list(CONFIRMED_COLUMNS)].to_csv(path, index=False, encoding="utf-8")


def confirm(
    confirmed: pd.DataFrame,
    stock_key: str,
    pmr_key: str,
    decision: str = "match",
    note: str = "",
    confirmed_on: str = "",
) -> pd.DataFrame:
    """Return the lookup with one decision added or replaced (latest decision wins)."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision == "match" and not pmr_key:
        raise ValueError("a 'match' decision needs a pmr_key")
    row = pd.DataFrame(
        [[stock_key, pmr_key if decision == "match" else "", decision, note, confirmed_on]],
        columns=list(CONFIRMED_COLUMNS),
    )
    kept = confirmed[confirmed["stock_key"] != stock_key]
    return pd.concat([kept, row], ignore_index=True)


# ---- matching ----------------------------------------------------------------------------


def _add_pmr_key(usage: pd.DataFrame) -> pd.DataFrame:
    keyed = usage.copy()
    keys = [make_key(n, s) for n, s in zip(keyed["name_norm"], keyed["strength_norm"])]
    keyed["pmr_key"] = pd.Series(keys, index=keyed.index).where(keyed["name_norm"] != "", "")
    return keyed


def _products(keyed: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct PMR product, indexed by pmr_key."""
    named = keyed[keyed["pmr_key"] != ""]
    if named.empty:
        empty = pd.DataFrame(columns=["name_norm", "strength_norm", "item_name", "lines"])
        empty.index.name = "pmr_key"
        return empty
    return named.groupby("pmr_key").agg(
        name_norm=("name_norm", "first"),
        strength_norm=("strength_norm", "first"),
        item_name=("item_name", lambda s: s.value_counts().index[0]),
        lines=("pmr_key", "size"),
    )


def _code_index(keyed: pd.DataFrame) -> dict[str, str]:
    """Item code -> the pmr_key it is most often used with."""
    coded = keyed[(keyed["item_code"] != "") & (keyed["pmr_key"] != "")]
    if coded.empty:
        return {}
    counts = coded.groupby(["item_code", "pmr_key"]).size().reset_index(name="n")
    best = counts.sort_values(["item_code", "n", "pmr_key"], ascending=[True, False, True])
    return dict(best.drop_duplicates("item_code")[["item_code", "pmr_key"]].to_numpy())


def _fuzzy_candidates(
    row: pd.Series, products: pd.DataFrame, threshold: int
) -> list[tuple[str, float]]:
    """(pmr_key, score) for products with the same strength and a similar name, best first."""
    if not row["strength_norm"]:
        return []
    same = products[products["strength_norm"] == row["strength_norm"]]
    if same.empty:
        return []
    hits = process.extract(
        row["name_norm"],
        dict(zip(same.index, same["name_norm"])),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold,
        limit=MAX_CANDIDATES,
    )
    return [(key, float(score)) for _, score, key in hits]


def _match_one(
    row: pd.Series,
    products: pd.DataFrame,
    code_index: dict[str, str],
    use_code_match: bool,
    threshold: int,
) -> dict:
    out = {
        "match_level": LEVEL_NONE,
        "match_status": REVIEW,
        "pmr_key": "",
        "candidate_name": "",
        "candidate_score": float("nan"),
        "candidates": "",
        "ambiguous": False,
        "reason": "",
    }
    if not row["name_norm"]:
        out["reason"] = "blank item name"
        return out
    if use_code_match and row["item_code"] and row["item_code"] in code_index:
        out.update(
            match_level=LEVEL_CODE, match_status=MATCHED, pmr_key=code_index[row["item_code"]]
        )
        return out
    if row["stock_key"] in products.index:
        out.update(match_level=LEVEL_KEY, match_status=MATCHED, pmr_key=row["stock_key"])
        return out
    candidates = _fuzzy_candidates(row, products, threshold)
    if candidates:
        best_key, best_score = candidates[0]
        out.update(
            match_level=LEVEL_FUZZY,
            pmr_key=best_key,  # a proposal only: the status stays 'review'
            candidate_name=products.loc[best_key, "item_name"],
            candidate_score=best_score,
            candidates="; ".join(
                f"{products.loc[k, 'item_name']} ({s:.0f})" for k, s in candidates
            ),
            ambiguous=len(candidates) > 1 and best_score - candidates[1][1] <= AMBIGUITY_MARGIN,
            reason="similar name, same strength: needs a person to confirm",
        )
        return out
    out["reason"] = (
        "no PMR product with this strength and a similar name"
        if row["strength_norm"]
        else "no strength to compare, and no exact name match"
    )
    return out


def _apply_confirmed(
    matches: pd.DataFrame, confirmed: pd.DataFrame, products: pd.DataFrame
) -> pd.DataFrame:
    """Apply earlier human decisions to rows the automatic levels could not settle.

    Automatic matches (levels 1 and 2) are never overridden by the lookup.
    """
    if confirmed.empty:
        return matches
    decisions = confirmed.drop_duplicates("stock_key", keep="last").set_index("stock_key")
    for index in matches.index[matches["match_status"] == REVIEW]:
        key = matches.at[index, "stock_key"]
        if key not in decisions.index:
            continue
        decision = decisions.loc[key]
        if decision["decision"] == "no_match":
            status, pmr_key, reason = NO_MATCH, "", "confirmed: not in PMR"
        elif decision["pmr_key"] in products.index:
            status, pmr_key, reason = MATCHED, decision["pmr_key"], "confirmed by a person"
        else:
            matches.at[index, "reason"] = "earlier confirmation no longer matches any PMR product"
            continue
        matches.at[index, "match_status"] = status
        matches.at[index, "pmr_key"] = pmr_key
        matches.at[index, "reason"] = reason
        matches.at[index, "confirmed"] = True
    return matches


def match_items(
    stock: pd.DataFrame,
    usage: pd.DataFrame,
    use_code_match: bool = False,
    confirmed: pd.DataFrame | None = None,
    threshold: int = FUZZY_THRESHOLD,
) -> MatchResult:
    """Match every stock row to a PMR product. Nothing is dropped: unsettled rows go to review."""
    keyed = _add_pmr_key(usage)
    products = _products(keyed)
    code_index = _code_index(keyed) if use_code_match else {}

    table = stock.copy()
    table["stock_key"] = [
        make_key(n, s) for n, s in zip(table["name_norm"], table["strength_norm"])
    ]
    cache: dict[tuple[str, str], dict] = {}
    results = []
    for _, row in table.iterrows():
        cache_key = (row["stock_key"], row["item_code"] if use_code_match else "")
        if cache_key not in cache:
            cache[cache_key] = _match_one(row, products, code_index, use_code_match, threshold)
        results.append(cache[cache_key])
    outcome_columns = ["match_level", "match_status", "pmr_key", "candidate_name",
                       "candidate_score", "candidates", "ambiguous", "reason"]  # fmt: skip
    outcome = pd.DataFrame(results, index=table.index, columns=outcome_columns)
    matches = pd.concat([table, outcome], axis=1)
    matches["confirmed"] = False
    matches = _apply_confirmed(
        matches, confirmed if confirmed is not None else empty_confirmed(), products
    )
    matches = matches[list(MATCH_COLUMNS)].reset_index(drop=True)

    matched = matches["match_status"] == MATCHED
    keyed["matched_to_robot"] = keyed["pmr_key"].isin(set(matches.loc[matched, "pmr_key"]))
    return MatchResult(
        matches=matches,
        review=_review_list(matches, keyed, products),
        usage=keyed,
        match_rate=float(matched.mean()) if len(matches) else 0.0,
        level_counts={int(k): int(v) for k, v in matches["match_level"].value_counts().items()},
        status_counts={str(k): int(v) for k, v in matches["match_status"].value_counts().items()},
    )


def _pmr_review_row(item_name: str, name_norm: str, strength_norm: str, reason: str) -> dict:
    return {
        "side": "pmr",
        "robot_id": "",
        "branch": "",
        "item_name": item_name,
        "name_norm": name_norm,
        "strength_norm": strength_norm,
        "stock_key": "",
        "match_level": LEVEL_NONE,
        "reason": reason,
        "candidate_name": "",
        "candidate_score": float("nan"),
        "candidates": "",
        "ambiguous": False,
        "source_row": pd.NA,
    }


def _review_list(matches: pd.DataFrame, keyed: pd.DataFrame, products: pd.DataFrame):
    """Robot rows awaiting a decision, then PMR products (and blank rows) no robot item claimed."""
    robot_side = matches[matches["match_status"] == REVIEW].assign(side="robot")
    claimed = set(matches.loc[matches["match_status"] == MATCHED, "pmr_key"])
    rows = [
        _pmr_review_row(
            p.item_name,
            p.name_norm,
            p.strength_norm,
            "dispensed in PMR but not matched to any robot item",
        )
        for p in products[~products.index.isin(claimed)].itertuples()
    ]
    blank = int((keyed["pmr_key"] == "").sum())
    if blank:
        rows.append(_pmr_review_row("", "", "", f"{blank} PMR row(s) have a blank product name"))
    pmr_side = pd.DataFrame(rows, columns=list(REVIEW_COLUMNS))
    review = pd.concat([robot_side, pmr_side], ignore_index=True)
    return review[list(REVIEW_COLUMNS)]


# ---- bulk decisions ----------------------------------------------------------------------


def bulk_not_in_pmr_candidates(matching: MatchResult) -> pd.DataFrame:
    """Stock items still awaiting review that have no PMR candidate at all.

    Items with a close candidate (level 3, the look-alike risk), blank names and stale earlier
    confirmations are left out: those need a person to look at them one by one.
    """
    rows = matching.matches
    pending = rows[
        (rows["match_status"] == REVIEW)
        & (rows["match_level"] == LEVEL_NONE)
        & (rows["name_norm"] != "")
        & ~rows["reason"].str.startswith("earlier confirmation")
    ]
    columns = ["robot_id", "branch", "item_name", "stock_key", "reason"]
    return pending.drop_duplicates("stock_key")[columns].reset_index(drop=True)


def confirm_all_not_in_pmr(
    confirmed: pd.DataFrame, candidates: pd.DataFrame, confirmed_on: str = ""
) -> pd.DataFrame:
    """Record 'not in the PMR' for every candidate. Existing decisions for them are replaced."""
    table = confirmed
    for stock_key in candidates["stock_key"]:
        table = confirm(
            table,
            stock_key,
            "",
            "no_match",
            note="bulk: no similar PMR product",
            confirmed_on=confirmed_on,
        )
    return table
