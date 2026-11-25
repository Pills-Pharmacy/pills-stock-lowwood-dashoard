"""Robot suggestions: ABC groups from how fast products sell, and what to add, remove or stock up."""

import pandas as pd

import config
import pipeline
import suggest
from tests.test_pipeline import files


def products(scripts):
    return pd.DataFrame(
        {
            "product": [f"P{i}" for i in range(len(scripts))],
            "pmr_key": [f"k{i}" for i in range(len(scripts))],
            "scripts": scripts,
            "quantity": [float(s) * 10 for s in scripts],
            "trend": "steady",
        }
    )


def test_abc_groups_follow_the_cumulative_share_of_scripts():
    # 100 scripts: P0 80, P1 15, P2 3, P3 2 -> A is P0 only, B is P1, C is P2 and P3
    frame = suggest.abc_groups(products([2, 80, 15, 3]))
    assert list(frame["product"]) == ["P1", "P2", "P3", "P0"]
    assert list(frame["group"]) == ["A", "B", "C", "C"]
    assert abs(frame["cumulative_share"].iloc[-1] - 1.0) < 1e-9
    assert list(frame["rank"]) == [1, 2, 3, 4]


def test_the_product_that_crosses_the_line_is_in_the_faster_group():
    frame = suggest.abc_groups(products([50, 50]))
    assert list(frame["group"]) == ["A", "A"]  # the second starts at 50%, still below 80%


def test_the_limits_are_the_documented_defaults():
    assert (config.ABC_A_SHARE, config.ABC_B_SHARE) == (0.80, 0.95)


def test_with_stock_it_lists_what_to_add_and_pairs_it_with_what_to_remove():
    result = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    sg = result.suggestions
    assert sg.has_stock and not sg.warnings
    assert set(sg.abc["in_robot"]) == {"Yes", "No"}
    assert set(sg.add["group"]) <= {"A", "B"}
    assert len(sg.add) == sg.meta["add_count"] == 6
    # none of the added products is held by the robot
    held = set(sg.abc.loc[sg.abc["in_robot"] == "Yes", "product"])
    assert not held & set(sg.add["product"])
    assert {"suggest_abc", "suggest_add", "suggest_swaps"} <= set(result.report.tables)


def test_a_pmr_alone_groups_products_but_suggests_nothing():
    result = pipeline.run_pipeline({"pmr": files()["pmr"]})
    sg = result.suggestions
    assert not sg.has_stock and sg.meta["add_count"] == 0
    assert set(sg.abc["in_robot"]) == {"Unknown"}
    assert any("No stock file" in w for w in sg.warnings)
    assert result.report.tables["suggest_add"].empty


def test_dead_stock_and_slow_movers_are_listed_for_removal_slowest_first():
    result = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    analysis = result.analysis
    # make one held item look dead and another slow, then rebuild the suggestions
    items = analysis.items.copy()
    items.loc[items.index[0], "classification"] = "dead_stock"
    analysis.items = items
    sg = suggest.build(result.usage, analysis)
    assert len(sg.remove) >= 1
    assert sg.remove["scripts"].is_monotonic_increasing
    assert (sg.remove["why"] == "In the robot, but none dispensed in the window").any()


def test_suggestion_tables_carry_no_patient_columns():
    result = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    for key, table in result.report.tables.items():
        if key.startswith("suggest"):
            assert not any("patient" in c.lower() for c in table.columns)


def test_every_table_is_limited_to_catalogue_products():
    from eligible import Catalogue

    plain = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    p = plain.suggestions
    keys = set(p.abc["pmr_key"].head(8))  # eight of the dispensed products are in the catalogue
    sg = pipeline.run_pipeline(
        files(robot_2=None),
        generated_at="2026-10-29 12:00",
        catalogue=Catalogue(keys=keys, rows=8, skipped=0),
    ).suggestions
    assert set(sg.abc["pmr_key"]) == keys and sg.meta["products"] == 8
    assert sg.meta["left_out_products"] == len(p.abc) - 8 and 0 < sg.meta["left_out_share"] < 1
    assert set(sg.add["product"]) <= set(p.add["product"]) and len(sg.add) < len(p.add)
    assert len(sg.remove) <= len(p.remove) and len(sg.increase) <= len(p.increase)


def test_a_catalogue_matching_nothing_warns():
    from eligible import Catalogue

    result = pipeline.run_pipeline(
        files(robot_2=None), catalogue=Catalogue(keys={"nope|"}, rows=1, skipped=0)
    )
    sg = result.suggestions
    assert any("right file" in w for w in sg.warnings)
    assert "left_out_products" not in sg.meta and len(sg.abc) > 0  # not applied


def test_swap_pairs_show_how_much_the_robot_holds_of_the_item_moved_out():
    result = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    sg = result.suggestions
    assert list(sg.swaps.columns)[:3] == ["pair", "move_out", "out_stock_qty"]
    assert sg.swaps["out_stock_qty"].notna().all() and "stock_qty" in sg.remove
    assert sg.swaps["out_stock_qty"].tolist() == sg.remove["stock_qty"].head(len(sg.swaps)).tolist()


def test_swap_pairs_only_use_catalogue_products_on_both_sides():
    from eligible import Catalogue

    result = pipeline.run_pipeline(files(robot_2=None), generated_at="2026-10-29 12:00")
    items = result.analysis.items.copy()
    items.loc[items.index[:3], "classification"] = "dead_stock"  # three items to move out
    result.analysis.items = items
    plain = suggest.build(result.usage, result.analysis)
    assert len(plain.swaps) == 3  # no catalogue: every added product can be paired
    moved_in = set(
        plain.abc.loc[plain.abc["product"].isin(plain.add["product"].head(2)), "pmr_key"]
    )
    keys = moved_in | set(items["item_id"].head(3))  # the items moving out are in it too
    sg = suggest.build(result.usage, result.analysis, Catalogue(keys=keys, rows=5, skipped=0))
    assert sg.swaps["move_in"].tolist() == plain.add["product"].head(2).tolist()
    assert set(sg.swaps["move_out"]) <= set(plain.swaps["move_out"])
    outside = suggest.build(
        result.usage, result.analysis, Catalogue(keys=moved_in, rows=2, skipped=0)
    )
    assert outside.swaps.empty  # no robot item in the catalogue to move out
