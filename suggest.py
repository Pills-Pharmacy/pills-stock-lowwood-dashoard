"""Robot stocking suggestions from how fast each product sells (an ABC analysis).

The PMR says how much each product sells. Products are ranked by scripts (units differ between
products, so scripts are the fair common measure) and grouped:

    A  fast movers: the products that together make the first ABC_A_SHARE of all scripts
    B  medium movers: up to ABC_B_SHARE
    C  slow movers: the long tail

Against the robot's stock list that gives, for the people who arrange the robot:
    * add to the robot: A and B products the robot does not hold, fastest first
    * review for removal: robot items that are dead stock or group C, slowest first
    * stock up: group A items the robot holds but that are understocked
    * swap pairs: the slowest robot item beside the fastest missing product

These are suggestions, not instructions: the PMR does not say which robot dispensed a line and
there is no slot data, so how many products fit is for the team to decide.

Matching is by product (pmr_key), pharmacy-wide, like the usage analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import ABC_A_SHARE, ABC_B_SHARE

A, B, C = "A", "B", "C"
YES, NO, UNKNOWN = "Yes", "No", "Unknown"
SWAP_PAIRS = 10

ABC_COLUMNS = ("rank", "product", "scripts", "quantity", "share", "cumulative_share", "group",
               "in_robot", "trend")  # fmt: skip
ADD_COLUMNS = ("rank", "product", "group", "scripts", "quantity", "share", "trend")
REMOVE_COLUMNS = ("rank", "product", "group", "stock_qty", "scripts", "classification",
                  "days_of_cover", "why")  # fmt: skip
INCREASE_COLUMNS = ("product", "group", "scripts", "stock_qty", "days_of_cover")
SWAP_COLUMNS = ("pair", "move_out", "out_stock_qty", "out_scripts", "move_in", "in_scripts")


@dataclass
class Suggestions:
    abc: pd.DataFrame  # every dispensed product, fastest first, with its group
    add: pd.DataFrame  # A/B products not in the robot
    remove: pd.DataFrame  # robot items that are dead stock or group C, slowest first
    increase: pd.DataFrame  # group A items in the robot that are understocked
    swaps: pd.DataFrame  # slowest robot item against fastest missing product
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_stock(self) -> bool:
        return self.meta.get("has_stock", False)


def abc_groups(
    products: pd.DataFrame, a_share: float = ABC_A_SHARE, b_share: float = ABC_B_SHARE
) -> pd.DataFrame:
    """Rank products by scripts and add share, cumulative share and the A/B/C group.

    A product is A if the share of scripts held by the products above it is still below
    `a_share` (so the product that crosses the line is A, and the first product always is).
    """
    frame = products.sort_values(["scripts", "quantity"], ascending=False).reset_index(drop=True)
    total = float(frame["scripts"].sum())
    frame["rank"] = np.arange(1, len(frame) + 1)
    frame["share"] = frame["scripts"] / total if total else 0.0
    frame["cumulative_share"] = frame["share"].cumsum()
    before = frame["cumulative_share"] - frame["share"]
    frame["group"] = np.select([before < a_share, before < b_share], [A, B], default=C)
    return frame


def _robot_keys(analysis) -> set[str]:
    """Products the robot holds that are matched to a PMR product."""
    keys: set[str] = set()
    for table in (analysis.items, analysis.unclassified):
        if table is not None and not table.empty:
            keys |= set(table["item_id"])
    return keys


def build(usage, analysis=None, catalogue=None) -> Suggestions | None:
    """Suggestions from a UsageResult and, when a stock file was uploaded, an AnalysisResult.

    With a `catalogue` (eligible.Catalogue) every table and chart is limited to products in it,
    so the A/B/C groups are worked out among the products the robot can hold.
    """
    products = usage.products
    if products is None or products.empty or "pmr_key" not in products:
        return None
    warnings: list[str] = []
    left_out = None
    if catalogue is not None:
        listed = products["pmr_key"].isin(catalogue.keys)
        if listed.any():
            left_out, products = products[~listed], products[listed]
        else:
            warnings.append(
                "None of the PMR products is in the robot catalogue, so it was not applied: "
                "check it is the right file."
            )
            catalogue = None
    frame = abc_groups(products)
    has_stock = analysis is not None
    if has_stock:
        held = _robot_keys(analysis)
        frame["in_robot"] = np.where(frame["pmr_key"].isin(held), YES, NO)
    else:
        frame["in_robot"] = UNKNOWN
        warnings.append(
            "No stock file was uploaded, so the robot's contents are unknown: products are "
            "grouped by how fast they sell, but nothing can be suggested to add or remove."
        )

    counts = frame["group"].value_counts()
    meta = {
        "has_stock": has_stock,
        "a_share": ABC_A_SHARE,
        "b_share": ABC_B_SHARE,
        "products": len(frame),
        "a_products": int(counts.get(A, 0)),
        "b_products": int(counts.get(B, 0)),
        "c_products": int(counts.get(C, 0)),
    }
    if left_out is not None:
        total = float(usage.products["scripts"].sum())
        meta["catalogue_products"] = len(catalogue.keys)
        meta["left_out_products"] = len(left_out)
        meta["left_out_share"] = float(left_out["scripts"].sum()) / total if total else 0.0
    empty_add = frame.iloc[0:0]
    add = frame[(frame["in_robot"] == NO) & frame["group"].isin([A, B])] if has_stock else empty_add
    remove, increase = _robot_side(frame, analysis, catalogue) if has_stock else (None, None)
    if remove is None:
        remove = pd.DataFrame(columns=list(REMOVE_COLUMNS))
        increase = pd.DataFrame(columns=list(INCREASE_COLUMNS))
    swaps = _swap_pairs(add.reset_index(drop=True), remove)
    meta["add_count"] = len(add)
    meta["remove_count"] = len(remove)
    meta["increase_count"] = len(increase)
    return Suggestions(
        abc=frame[[*ABC_COLUMNS, "pmr_key"]],
        add=add[list(ADD_COLUMNS)].reset_index(drop=True),
        remove=remove,
        increase=increase,
        swaps=swaps,
        meta=meta,
        warnings=warnings,
    )


def _robot_side(frame: pd.DataFrame, analysis, catalogue=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Removal candidates and stock-up candidates among the items the robot holds."""
    scripts = frame.set_index("pmr_key")[["product", "scripts", "group"]]
    items = analysis.items
    if items is None or items.empty:
        return (
            pd.DataFrame(columns=list(REMOVE_COLUMNS)),
            pd.DataFrame(columns=list(INCREASE_COLUMNS)),
        )
    if catalogue is not None:
        items = items[items["item_id"].isin(catalogue.keys)]
    joined = items.join(scripts[["scripts", "group"]], on="item_id")
    joined["scripts"] = joined["scripts"].fillna(0).astype(int)
    joined["group"] = joined["group"].fillna("None")  # in the robot, never dispensed
    out = joined[(joined["classification"] == "dead_stock") | (joined["group"] == C)]
    out = out.sort_values(["scripts", "item_name"]).reset_index(drop=True)
    remove = pd.DataFrame(
        {
            "rank": np.arange(1, len(out) + 1),
            "product": out["item_name"],
            "group": out["group"],
            "stock_qty": out["stock_qty"],
            "scripts": out["scripts"],
            "classification": out["classification"],
            "days_of_cover": out["days_of_cover"],
            "why": np.where(
                out["classification"] == "dead_stock",
                "In the robot, but none dispensed in the window",
                "Slow mover: among the last 5% of scripts",
            ),
        }
    )
    up = joined[(joined["classification"] == "understocked") & (joined["group"] == A)]
    up = up.sort_values("scripts", ascending=False)
    increase = pd.DataFrame(
        {
            "product": up["item_name"],
            "group": up["group"],
            "scripts": up["scripts"],
            "stock_qty": up["stock_qty"],
            "days_of_cover": up["days_of_cover"],
        }
    ).reset_index(drop=True)
    return remove, increase


def _swap_pairs(add: pd.DataFrame, remove: pd.DataFrame) -> pd.DataFrame:
    count = min(len(add), len(remove), SWAP_PAIRS)
    rows = [
        [i + 1, remove.loc[i, "product"], remove.loc[i, "stock_qty"], int(remove.loc[i, "scripts"]),
         add.loc[i, "product"], int(add.loc[i, "scripts"])]
        for i in range(count)
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=list(SWAP_COLUMNS))
