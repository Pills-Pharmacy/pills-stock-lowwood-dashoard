"""Hand-built standard tables for matching and analysis tests."""

import pandas as pd

import clean
from schema import STANDARD_STOCK_COLUMNS, STANDARD_USAGE_COLUMNS


def stock_row(name, code="", robot="robot_1", branch="Branch A", qty=10.0):
    return {
        "robot_id": robot,
        "branch": branch,
        "item_code": code,
        "item_name": name,
        "name_norm": clean.normalise_name(name),
        "strength_norm": clean.parse_strength(name),
        "strength_source": "name",
        "pack_size": 28.0,
        "unit": "tablet",
        "stock_qty": qty,
        "expiry_date": pd.NaT,
        "source_row": 2,
    }


def usage_row(name, code="", qty=1.0, date="2026-10-01", branch="Branch A"):
    return {
        "branch": branch,
        "item_code": code,
        "item_name": name,
        "generic_name": "",
        "name_norm": clean.normalise_name(name),
        "strength_norm": clean.parse_strength(name),
        "strength_source": "name",
        "pack_size": float("nan"),
        "unit": "",
        "dispensed_qty": qty,
        "qty_basis": "reported",
        "dispense_date": pd.Timestamp(date),
        "date_basis": "day",
        "source_row": 2,
    }


def stock_of(*rows):
    return pd.DataFrame(list(rows)).reindex(columns=list(STANDARD_STOCK_COLUMNS))


def usage_of(*rows):
    return pd.DataFrame(list(rows)).reindex(columns=list(STANDARD_USAGE_COLUMNS))
