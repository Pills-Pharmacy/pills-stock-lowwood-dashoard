from pathlib import Path

import pandas as pd
import pytest

import clean
import load
import make_fixtures
import schema

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"
SOURCE_OF = {
    "omnicell_robot_1": R1,
    "omnicell_robot_2": R2,
    "pmr_usage": PMR,
    "omnicell_activity": "omnicell_activity",
}


def load_fixture(name: str) -> load.LoadResult:
    source = SOURCE_OF[name.split("__")[0].removesuffix(".csv")]
    return load.load_source(source, FIXTURES / name)


def cleaned(name: str, **kwargs) -> pd.DataFrame:
    return clean.clean(load_fixture(name), **kwargs)


# ---- text, strength, name, unit --------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("500mg", "500mg"),
        ("0.5g", "500mg"),
        ("500 MG", "500mg"),
        ("0.4g", "400mg"),
        ("100µg", "0.1mg"),
        ("100mcg", "0.1mg"),
        ("1g", "1000mg"),
        ("10mg/5ml", "10mg/5ml"),
        ("Morphine 10 mg / 5 ml Oral Solution 100ml", "10mg/5ml"),
        ("CO-CODAMOL 30MG/500MG TABLETS", "30mg/500mg"),
        ("Clobetasol 0.05% Ointment", "0.05%"),
        ("Insulin 100 units/ml", "100units/ml"),
        ("Salbutamol 100mcg Inhaler", "0.1mg"),
        ("No strength here", ""),
        ("", ""),
    ],
)
def test_parse_strength_gives_one_canonical_form(text, expected):
    assert clean.parse_strength(text) == expected


def test_strength_is_not_taken_from_inside_words_or_pack_counts():
    assert clean.parse_strength("Pack of 28 tablets") == ""
    assert clean.parse_strength("Drug x28 gel") == ""
    assert clean.parse_strength("Amlodipine 5 mg Tablets") == "5mg"
    assert clean.parse_strength("FREESTYLE/L 3+ CONT GLUCOSE MONITOR") == ""
    assert clean.parse_strength("Drug 5mg/ml") == "5mg/ml"


@pytest.mark.parametrize(
    "a, b",
    [
        ("Paracetamol 500mg Tablets", "Paracetamol 0.5g Tabs"),
        ("Amoxicillin 500mg Capsules", "Amox 500mg Caps"),
        ("Metformin 500mg Tablets", "Metformin Hydrochloride 500 mg Tablets"),
        ("Omeprazole 20mg Capsules", "Omeprazole 20mg Gastro-resistant Caps"),
        ("Morphine Sulf 10mg/5ml Oral Soln", "Morphine Sulfate 10mg/5ml Oral Solution 100ml"),
        ("Ceftriaxone 1g Inj", "Ceftriaxone Sodium 1g Powder for Injection"),
        ("IBUPROFEN 400MG TABS", "Ibuprofen 400mg Tablets"),
    ],
)
def test_name_variants_normalise_to_the_same_key(a, b):
    assert clean.normalise_name(a) == clean.normalise_name(b)


def test_different_drugs_do_not_collide():
    assert clean.normalise_name("Atorvastatin 20mg Tablets") != clean.normalise_name(
        "Amlodipine 5mg Tablets"
    )
    assert clean.normalise_name("Omeprazole 20mg Capsules") != clean.normalise_name(
        "Omeprazole 20mg Tablets"
    )


def test_clean_text_trims_collapses_and_handles_missing():
    assert clean.clean_text("  Drug \t X  ") == "Drug X"
    assert clean.clean_text(None) == "" and clean.clean_text(float("nan")) == ""
    assert clean.clean_text("100µg") == "100μg"  # NFKC


@pytest.mark.parametrize(
    "unit, expected",
    [("Tabs", "tablet"), (" CAPS ", "capsule"), ("Bottle", "bottle"), ("Box", "box"), ("", "")],
)
def test_standard_unit(unit, expected):
    assert clean.standard_unit(unit) == expected


# ---- the 12 fixture items must line up across robot and PMR ----------------------------


def test_every_fixture_item_gets_the_same_matching_key_in_robot_and_pmr():
    stock, usage = clean.clean_all(
        {
            R1: load_fixture("omnicell_robot_1.csv"),
            R2: load_fixture("omnicell_robot_2.csv"),
            PMR: load_fixture("pmr_usage.csv"),
        }
    )
    assert len(stock) == 12
    pmr_keys = {
        (r.name_norm, r.strength_norm) for r in usage.drop_duplicates("item_name").itertuples()
    }
    robot_keys = {(r.name_norm, r.strength_norm) for r in stock.itertuples()}
    assert robot_keys == pmr_keys and len(robot_keys) == 12


# ---- standard tables -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["omnicell_robot_1.csv", "omnicell_robot_2.csv"])
def test_stock_table_shape_branch_and_types(name):
    table = cleaned(name)
    assert tuple(table.columns) == schema.STANDARD_STOCK_COLUMNS
    assert len(table) == 6 and table["robot_id"].nunique() == 1
    assert table["branch"].iloc[0] in {"Branch A", "Branch B"}
    assert pd.api.types.is_float_dtype(table["stock_qty"])
    assert not table["item_name"].str.contains(r"^\s|\s$").any()  # stray spaces gone


def test_branch_comes_from_the_registry_per_robot():
    assert set(cleaned("omnicell_robot_1.csv")["branch"]) == {"Branch A"}
    assert set(cleaned("omnicell_robot_2.csv")["branch"]) == {"Branch B"}
    assert set(cleaned("omnicell_robot_2.csv")["robot_id"]) == {"robot_2"}


def test_robot_1_fields_expiry_pack_and_unit():
    table = cleaned("omnicell_robot_1.csv")
    assert pd.api.types.is_datetime64_any_dtype(table["expiry_date"])
    assert table["expiry_date"].notna().all()
    assert (table["expiry_date"] == table["expiry_date"].dt.normalize()).all()
    assert table["pack_size"].tolist() == [100, 21, 84, 28, 28, 28]
    assert set(table["unit"]) == {"tablet", "capsule"}


def test_robot_2_has_no_expiry_column_so_it_is_blank_not_an_error():
    table = cleaned("omnicell_robot_2.csv")
    assert table["expiry_date"].isna().all()


def test_strength_source_is_recorded():
    robot_2 = cleaned("omnicell_robot_2.csv")
    assert set(robot_2["strength_source"]) == {"column"}
    assert robot_2.loc[robot_2["item_code"] == "FAKE-0007", "strength_norm"].iloc[0] == "400mg"
    pmr = cleaned("pmr_usage.csv")  # no strength column: parsed from the product name
    assert set(pmr["strength_source"]) == {"name"}
    assert (pmr["strength_norm"] != "").all()


def test_pmr_table_shape_and_dates():
    table = cleaned("pmr_usage.csv")
    assert tuple(table.columns) == schema.STANDARD_USAGE_COLUMNS
    assert set(table["branch"]) == {"Branch A", "Branch B"}
    assert set(table["qty_basis"]) == {"reported"}
    assert table["dispense_date"].min() == pd.Timestamp("2026-07-22")
    assert table["dispense_date"].max() == pd.Timestamp("2026-10-19")
    assert not any("patient" in c.lower() for c in table.columns)
    assert table["dispensed_qty"].sum() == sum(i.dispensed for i in make_fixtures.ITEMS)


def test_source_row_points_back_to_the_file_line():
    table = cleaned("omnicell_robot_1.csv")
    assert table["source_row"].tolist() == [2, 3, 4, 5, 6, 7]


# ---- no rows lost, odd values kept ------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.csv")), ids=lambda p: p.name)
def test_no_rows_are_lost_in_tidying(path):
    result = load_fixture(path.name)
    if result.stopped:
        pytest.skip("load stopped by design")
    if path.name.startswith("omnicell_activity"):
        pytest.skip("activity files are cleaned by activity.py, not clean.py")
    table = clean.clean(result)
    assert len(table) == len(result.data)
    assert table["source_row"].tolist() == [i + 2 for i in result.data.index]


def test_negative_and_missing_quantities_are_kept_as_numbers_or_nan():
    table = cleaned("omnicell_robot_1__negative_qty.csv")
    assert table["stock_qty"].iloc[1] == -12 and pd.isna(table["stock_qty"].iloc[4])


def test_duplicates_are_not_removed_by_cleaning():
    table = cleaned("omnicell_robot_1__duplicate_rows.csv")
    assert len(table) == 8


def test_pack_unit_survives_for_the_analysis_to_see():
    assert cleaned("omnicell_robot_1__unit_mismatch.csv")["unit"].iloc[0] == "box"


def test_encoding_variants_clean_identically_to_the_baseline():
    base = cleaned("omnicell_robot_1.csv")
    for name in ("omnicell_robot_1__cp1252.csv", "omnicell_robot_1__utf8_bom.csv"):
        pd.testing.assert_frame_equal(cleaned(name), base)
    assert base.loc[5, "strength_norm"] == "0.1mg"


def test_blank_pmr_branch_gets_the_default_or_unknown():
    result = load_fixture("pmr_usage__missing_branch.csv")
    assert (clean.clean(result)["branch"] == "Unknown").sum() == 5
    assert (clean.clean(result, default_branch="Branch A")["branch"] == "Unknown").sum() == 0


# ---- PMR without a quantity column (the pmr_test.csv shape) --------------------------------


def test_missing_quantity_counts_one_line_per_row():
    data = (
        b"Dispensed Date,EPES ID,Generic Name,Script Dispensed As\n"
        b"10/1/2026 9:29,ID-1,ATORVASTATIN,ATORVASTATIN 20MG TABLETS\n"
        b"10/6/2026 16:35,ID-2,,CO-CODAMOL 30MG/500MG TABLETS\n"
    )
    table = clean.clean(load.load_source(PMR, data), default_branch="Branch A")
    assert table["dispensed_qty"].tolist() == [1.0, 1.0]
    assert set(table["qty_basis"]) == {"line_count"}
    assert table["branch"].tolist() == ["Branch A", "Branch A"]
    assert table["name_norm"].tolist() == ["atorvastatin tablets", "co codamol tablets"]
    assert table["strength_norm"].tolist() == ["20mg", "30mg/500mg"]
    assert table["generic_name"].tolist() == ["ATORVASTATIN", ""]
    assert table["dispense_date"].tolist() == [
        pd.Timestamp("2026-10-01"),
        pd.Timestamp("2026-10-06"),
    ]
    assert "ID-1" not in table.to_csv()


# ---- orchestration -------------------------------------------------------------------------


def test_clean_refuses_a_stopped_load():
    with pytest.raises(ValueError):
        clean.clean(load_fixture("omnicell_robot_1__missing_required_column.csv"))


def test_clean_all_skips_stopped_loads_and_handles_missing_pmr():
    stopped = load_fixture("omnicell_robot_1__missing_required_column.csv")
    stock, usage = clean.clean_all({R1: stopped, R2: load_fixture("omnicell_robot_2.csv")})
    assert len(stock) == 6 and usage is None
    empty, none = clean.clean_all({R1: stopped})
    assert empty.empty and tuple(empty.columns) == schema.STANDARD_STOCK_COLUMNS and none is None


def test_item_in_both_robots_keeps_a_row_per_robot():
    stock, _ = clean.clean_all(
        {
            R1: load_fixture("omnicell_robot_1.csv"),
            R2: load_fixture("omnicell_robot_2__item_in_both_robots.csv"),
        }
    )
    levo = stock[stock["name_norm"] == "levothyroxine tablets"]
    assert sorted(levo["robot_id"]) == ["robot_1", "robot_2"]
    assert sorted(levo["branch"]) == ["Branch A", "Branch B"]
