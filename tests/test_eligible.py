"""Tests for the robot catalogue loader (synthetic data only)."""

import pytest

from clean import normalise_name, parse_strength
from eligible import load_catalogue
from match import make_key


def _write(tmp_path, rows):
    path = tmp_path / "catalogue.csv"
    path.write_text(
        "﻿trade_name,pack_size\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )
    return path


def test_keys_match_the_pmr_key_for_the_same_name(tmp_path):
    name = "AMLODIPINE 5MG TABLETS"
    catalogue = load_catalogue(_write(tmp_path, [f"{name},28"]))
    assert make_key(normalise_name(name), parse_strength(name)) in catalogue


def test_duplicates_collapse_and_blank_names_are_counted(tmp_path):
    rows = ["AMLODIPINE 5MG TABLETS,28", "AMLODIPINE 5MG TABLETS,28", ",1"]
    catalogue = load_catalogue(_write(tmp_path, rows))
    assert (catalogue.rows, len(catalogue.keys), catalogue.skipped) == (3, 1, 1)


def test_missing_name_column_is_a_clear_error(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("name,size\nX,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="trade_name"):
        load_catalogue(path)
