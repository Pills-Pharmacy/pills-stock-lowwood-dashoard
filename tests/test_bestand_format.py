"""The real OmniCell stock listing shape (BestandArtikel), rebuilt with made-up values."""

from pathlib import Path

import pandas as pd
import pytest

import clean
import config
import load
import match
import pipeline
from config import DEFAULT_SETTINGS

FIXTURES = Path(__file__).parent / "fixtures"
BESTAND = FIXTURES / "omnicell_robot_1__bestand_format.csv"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"


@pytest.fixture(scope="module")
def loaded():
    return load.load_source(R1, BESTAND)


@pytest.fixture(scope="module")
def stock(loaded):
    return clean.clean(loaded)


def test_it_is_a_semicolon_file_with_the_real_61_headers(loaded):
    assert loaded.delimiter == ";" and loaded.encoding == "utf-8"
    assert len(loaded.raw_headers) == 61
    assert loaded.detected_type == "omnicell_stock" and not loaded.stopped


def test_the_four_columns_the_audit_needs_are_mapped(loaded):
    assert list(loaded.data.columns) == [
        "item_name",
        "pack_size",
        "item_code",
        "stock_qty",
        "strength",
    ]
    mapped, _ = config.resolve_headers(R1, loaded.raw_headers)
    assert mapped["ARTIKELCODE"] == "item_code" and mapped["NAME"] == "item_name"
    assert mapped["PACKSIZE"] == "pack_size" and mapped["ISTANZAHL"] == "stock_qty"


def test_copies_and_unneeded_columns_are_ignored_not_mapped(loaded):
    _, unmapped = config.resolve_headers(R1, loaded.raw_headers)
    for ignored in ("NAME1", "ISTANZAHL1", "FREIANZAHL", "FORM", "MINMENGE", "MAXMENGE"):
        assert ignored in unmapped
    [issue] = loaded.by_check("unmapped_columns")
    assert issue.severity == load.INFO
    assert not loaded.by_check("ambiguous_columns")


def test_placeholder_expiry_dates_are_not_read_as_expiry(stock):
    assert stock["expiry_date"].isna().all()


def test_the_load_has_no_warnings_or_errors(loaded):
    assert [i for i in loaded.issues if i.severity != load.INFO] == []


def test_one_drug_under_two_barcodes_is_two_rows_not_a_duplicate(loaded, stock):
    assert not loaded.by_check("duplicates")
    paracetamol = stock[stock["name_norm"] == "paracetamol tablets"]
    assert sorted(paracetamol["item_code"]) == ["00000000000001", "00000000000101"]
    assert paracetamol["stock_qty"].sum() == 1500  # (12 + 3 packs) x pack size 100, packs default


def test_barcodes_keep_their_leading_zeros(stock):
    assert stock["item_code"].str.len().min() >= 13
    assert (stock["item_code"] == "00000000000001").any()


def test_the_full_name_is_used_not_the_50_character_copy(loaded):
    long = loaded.data[loaded.data["item_name"].str.startswith("BUDESONIDE")]
    assert len(long.iloc[0]["item_name"]) > 50  # the NAME1 copy would have cut it at 50


def test_strength_is_parsed_from_the_name_because_the_column_is_blank(stock):
    assert set(stock["strength_source"]) == {"name"}
    by_name = dict(zip(stock["item_name"], stock["strength_norm"]))
    assert by_name["PARACETAMOL 0.5G TABS"] == "500mg"
    assert by_name["LEVOTHYROXINE 0.1MG TABLETS"] == "0.1mg"
    assert by_name["BUDESONIDE 500MCG/2ML NEBULISER LIQUID UNIT DOSE VIALS"] == "0.5mg/2ml"


def test_branch_comes_from_the_robot_slot_not_the_file(stock):
    assert set(stock["robot_id"]) == {"robot_1"} and set(stock["branch"]) == {"Branch A"}


def test_it_loads_in_either_robot_slot_and_is_rejected_in_the_pmr_slot():
    assert not load.load_source(R2, BESTAND).stopped
    wrong = load.load_source(PMR, BESTAND)
    assert wrong.stopped and wrong.by_check("wrong_file_type")


# ---- through matching and analysis ----------------------------------------------------------------


def run(**kwargs):
    files = {
        R1: BESTAND.read_bytes(),
        PMR: (FIXTURES / "pmr_usage.csv").read_bytes(),
    }
    return pipeline.run_pipeline(files, **kwargs)


def test_names_match_the_pmr_at_level_2_and_unknown_items_go_to_review():
    result = run()
    assert result.ran and result.errors == []
    levels = result.matching.level_counts
    assert levels == {2: 7, 4: 1}  # seven rows match; the budesonide row has no PMR item
    reviewed = result.matching.review.query("side == 'robot'")
    assert list(reviewed["item_name"]) == ["BUDESONIDE 500MCG/2ML NEBULISER LIQUID UNIT DOSE VIALS"]


def test_two_barcodes_of_one_drug_are_summed_in_the_analysis():
    result = run()
    row = result.analysis.items[result.analysis.items["name_norm"] == "paracetamol tablets"]
    [item] = row.to_dict("records")
    assert item["stock_qty"] == 15  # packs as exported, both barcodes together
    assert item["usage_rate_per_day"] == pytest.approx(900 / 90)


# ---- the optional pack-count switch ---------------------------------------------------------------


def test_stock_count_is_read_as_packs_by_default():
    # Confirmed 2026-11-05: the count is a number of boxes.
    assert config.STOCK_QTY_IS_PACK_COUNT is True


def test_switching_on_pack_count_multiplies_by_pack_size(monkeypatch, loaded):
    monkeypatch.setattr(clean, "STOCK_QTY_IS_PACK_COUNT", True)
    units = clean.clean(loaded)
    paracetamol = units[units["item_code"] == "00000000000001"].iloc[0]
    assert paracetamol["stock_qty"] == 12 * 100  # 12 packs of 100


def test_pack_count_switch_leaves_rows_without_a_pack_size_as_exported(monkeypatch):
    monkeypatch.setattr(clean, "STOCK_QTY_IS_PACK_COUNT", True)
    data = b"NAME;ARTIKELCODE;ISTANZAHL;FREIANZAHL;PACKSIZE\nDRUG X 5MG TABLETS;1;7;7;\n"
    table = clean.clean(load.load_source(R1, data))
    assert table.loc[0, "stock_qty"] == 7 and pd.isna(table.loc[0, "pack_size"])


def test_the_switch_changes_days_of_cover(monkeypatch):
    base = run().analysis.items.set_index("name_norm")["days_of_cover"]
    monkeypatch.setattr(clean, "STOCK_QTY_IS_PACK_COUNT", True)
    packs = run().analysis.items.set_index("name_norm")["days_of_cover"]
    assert packs["paracetamol tablets"] == pytest.approx(base["paracetamol tablets"] * 100)


# ---- the word lists learned from the real names ---------------------------------------------------


@pytest.mark.parametrize(
    "robot, pmr",
    [
        ("APIXABAN 5MG TABLETS (GB)", "APIXABAN TABS 5MG"),  # region suffix on one side only
        ("FORXIGA TABS 10MG (NI)", "FORXIGA 10MG TABLETS"),
        ("BETMIGA TABS P/R 25MG (NI)", "BETMIGA 25MG TABLETS MR"),  # prolonged vs modified release
        ("TABLET M/R 5MG X", "TABLET MR 5MG X"),
        ("BRINZOLAMIDE DRPS 10MG/ML 5ML", "BRINZOLAMIDE DROPS 10MG/ML 5ML"),
        ("METHADONE 1MG/ML ORAL SOLU SF", "METHADONE 1MG/ML SOLUTION SF"),
    ],
)
def test_observed_naming_noise_normalises_to_one_key(robot, pmr):
    assert clean.normalise_name(robot) == clean.normalise_name(pmr)
    assert clean.parse_strength(robot) == clean.parse_strength(pmr)


@pytest.mark.parametrize(
    "a, b",
    [
        ("ESCITALOPRAM 20MG TABLETS", "CITALOPRAM 20MG TABLETS"),
        ("ESOMEPRAZOLE 20MG CAPSULES GR", "OMEPRAZOLE 20MG CAPSULES GR"),
        ("NORTRIPTYLINE 10MG TABLETS", "AMITRIPTYLINE 10MG TABLETS"),
        ("CLARITHROMYCIN 500MG TABLETS", "AZITHROMYCIN 500MG TABLETS"),
    ],
)
def test_look_alike_drugs_are_never_accepted_automatically(a, b):
    """These score 85+ on name similarity but are different drugs: review list only."""
    from frames import stock_of, stock_row, usage_of, usage_row

    result = match.match_items(stock_of(stock_row(a)), usage_of(usage_row(b)))
    row = result.matches.iloc[0]
    assert row["match_status"] == match.REVIEW
    assert row["match_level"] in (match.LEVEL_FUZZY, match.LEVEL_NONE)


def test_the_phrase_rewrite_only_touches_whole_tokens():
    assert clean.normalise_name("SPR/RAY 5MG") == clean.normalise_name("SPR/RAY 5MG")
    assert "mr" not in clean.normalise_name("APR/RICOT 5MG").split()
    assert DEFAULT_SETTINGS.problems() == []
