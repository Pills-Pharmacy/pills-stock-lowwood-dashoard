from pathlib import Path

import pandas as pd
import pytest

import clean
import load
import match
from frames import stock_of, stock_row, usage_of, usage_row

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"


def run(stock_names, usage_names, **kwargs):
    stock = stock_of(*[stock_row(n) for n in stock_names])
    usage = usage_of(*[usage_row(n) for n in usage_names])
    return match.match_items(stock, usage, **kwargs)


# ---- level 2: normalised key ---------------------------------------------------------------


@pytest.mark.parametrize(
    "robot_name, pmr_name",
    [
        ("Paracetamol 500mg Tablets", "Paracetamol 0.5g Tabs"),  # 500mg vs 0.5g, Tabs
        ("Amoxicillin 500mg Capsules", "AMOX 500MG CAPS"),  # abbreviation
        ("Metformin 500mg Tablets", "Metformin Hydrochloride 500 mg Tablets"),  # salt, spacing
        ("Levothyroxine 100µg Tablets", "Levothyroxine 0.1mg Tablets"),  # micrograms
    ],
)
def test_naming_variants_match_at_level_2(robot_name, pmr_name):
    result = run([robot_name], [pmr_name])
    [row] = result.matches.to_dict("records")
    assert row["match_level"] == match.LEVEL_KEY and row["match_status"] == match.MATCHED
    assert result.match_rate == 1.0 and result.review.empty


def test_all_fixture_items_match_at_level_2():
    results = {
        R1: load.load_source(R1, FIXTURES / "omnicell_robot_1.csv"),
        R2: load.load_source(R2, FIXTURES / "omnicell_robot_2.csv"),
        PMR: load.load_source(PMR, FIXTURES / "pmr_usage.csv"),
    }
    stock, usage = clean.clean_all(results)
    result = match.match_items(stock, usage)
    assert result.match_rate == 1.0
    assert result.level_counts == {2: 12} and result.status_counts == {"matched": 12}
    assert result.review.empty
    assert result.usage["matched_to_robot"].all()


def test_item_in_both_robots_matches_the_same_pmr_product():
    stock = stock_of(
        stock_row("Levothyroxine 100mcg Tablets", robot="robot_1", branch="Branch A"),
        stock_row("Levothyroxine 100mcg Tablets", robot="robot_2", branch="Branch B"),
    )
    result = match.match_items(stock, usage_of(usage_row("Levothyroxine 0.1mg Tablets")))
    assert result.matches["pmr_key"].nunique() == 1
    assert set(result.matches["robot_id"]) == {"robot_1", "robot_2"}
    assert result.match_rate == 1.0


# ---- level 1: exact code (off by default) -----------------------------------------------------


def test_code_match_is_off_by_default_and_works_when_switched_on():
    stock = stock_of(stock_row("Cardura 4mg Tablets", code="C-1"))
    usage = usage_of(usage_row("Doxazosin 4mg Tablets", code="C-1"))
    default = match.match_items(stock, usage)
    assert default.matches.loc[0, "match_status"] == match.REVIEW
    assert default.matches.loc[0, "match_level"] != match.LEVEL_CODE
    on = match.match_items(stock, usage, use_code_match=True)
    assert on.matches.loc[0, "match_level"] == match.LEVEL_CODE
    assert on.matches.loc[0, "match_status"] == match.MATCHED
    assert on.matches.loc[0, "pmr_key"] == "doxazosin tablets|4mg"


def test_code_match_ignores_blank_and_unknown_codes():
    stock = stock_of(stock_row("Cardura 4mg Tablets", code=""), stock_row("X 1mg", code="NOPE"))
    usage = usage_of(usage_row("Doxazosin 4mg Tablets", code="C-1"))
    result = match.match_items(stock, usage, use_code_match=True)
    assert (result.matches["match_level"] != match.LEVEL_CODE).all()


def test_exact_code_beats_a_different_name_key():
    stock = stock_of(stock_row("Metformin 500mg Tablets", code="M-1"))
    usage = usage_of(
        usage_row("Metformin 500mg Tablets", code="M-2"),
        usage_row("Glucophage 500mg Tablets", code="M-1"),
    )
    result = match.match_items(stock, usage, use_code_match=True)
    assert result.matches.loc[0, "pmr_key"] == "glucophage tablets|500mg"
    assert result.matches.loc[0, "match_level"] == match.LEVEL_CODE


# ---- level 3: fuzzy, review only -------------------------------------------------------------


def test_fuzzy_match_is_only_a_proposal_for_review():
    result = run(["Atorvastatin 20mg Tablets"], ["Atorvastin 20mg Tablets"])
    [row] = result.matches.to_dict("records")
    assert row["match_level"] == match.LEVEL_FUZZY
    assert row["match_status"] == match.REVIEW  # never accepted automatically
    assert row["candidate_name"] == "Atorvastin 20mg Tablets" and row["candidate_score"] >= 85
    assert result.match_rate == 0.0
    [review] = result.review[result.review["side"] == "robot"].to_dict("records")
    assert review["robot_id"] == "robot_1" and review["candidate_name"] == row["candidate_name"]


def test_fuzzy_needs_the_same_strength():
    result = run(["Atorvastatin 20mg Tablets"], ["Atorvastatin 40mg Tablets"])
    assert result.matches.loc[0, "match_level"] == match.LEVEL_NONE
    assert result.matches.loc[0, "candidate_name"] == ""


def test_no_fuzzy_when_strength_is_unknown():
    result = run(["Sharps Bin Yellow"], ["Sharps Bin Yelow"])
    row = result.matches.iloc[0]
    assert row["match_level"] == match.LEVEL_NONE and "no strength" in row["reason"]


def test_ambiguous_near_duplicates_list_every_candidate():
    result = run(
        ["Sertraline Tabs 50mg"],
        ["Sertraline Tablets Film 50mg", "Sertraline Tablets Coated 50mg"],
        threshold=60,
    )
    row = result.matches.iloc[0]
    assert row["match_status"] == match.REVIEW and row["match_level"] == match.LEVEL_FUZZY
    assert bool(row["ambiguous"]) is True
    assert "Film" in row["candidates"] and "Coated" in row["candidates"]


def test_a_clear_best_candidate_is_not_flagged_ambiguous():
    result = run(
        ["Sertraline Tabs 50mg"],
        ["Sertralin Tablets 50mg", "Sertraline Dispersible Chewable Tablets 50mg"],
        threshold=60,
    )
    assert bool(result.matches.iloc[0]["ambiguous"]) is False


def test_threshold_is_respected():
    result = run(["Atorvastatin 20mg Tablets"], ["Atorvastin 20mg Tablets"], threshold=99)
    assert result.matches.loc[0, "match_level"] == match.LEVEL_NONE


# ---- level 4 and the review list -----------------------------------------------------------


def test_brand_versus_generic_cannot_be_matched_automatically():
    result = run(["Lipitor 20mg Tablets"], ["Atorvastatin 20mg Tablets"])
    row = result.matches.iloc[0]
    assert row["match_level"] == match.LEVEL_NONE and row["match_status"] == match.REVIEW


def test_review_list_holds_both_sides_and_carries_robot_id():
    result = run(
        ["Aspirin 75mg Tablets", "Zzz Placeholder 10mg Tablets"],
        ["Aspirin 75mg Tablets", "Warfarin 3mg Tablets"],
    )
    review = result.review
    assert sorted(review["side"]) == ["pmr", "robot"]
    robot = review[review["side"] == "robot"].iloc[0]
    assert robot["robot_id"] == "robot_1" and robot["item_name"] == "Zzz Placeholder 10mg Tablets"
    pmr = review[review["side"] == "pmr"].iloc[0]
    assert pmr["item_name"] == "Warfarin 3mg Tablets" and pmr["robot_id"] == ""
    assert result.match_rate == 0.5
    assert list(review.columns) == list(match.REVIEW_COLUMNS)


def test_blank_names_are_reviewed_not_dropped():
    stock = stock_of(stock_row(""))
    usage = usage_of(usage_row(""), usage_row("Aspirin 75mg Tablets"))
    result = match.match_items(stock, usage)
    assert result.matches.loc[0, "reason"] == "blank item name"
    assert len(result.matches) == 1
    assert result.review["reason"].str.contains("blank product name").any()
    assert (result.usage["pmr_key"] == "").sum() == 1


def test_every_stock_row_comes_back_with_a_level():
    result = run(["A 1mg Tablets", "B 2mg Tablets", "C 3mg Tablets"], ["A 1mg Tablets"])
    assert len(result.matches) == 3
    assert result.matches["match_level"].isin([1, 2, 3, 4]).all()
    assert result.level_counts == {2: 1, 4: 2}


def test_empty_stock_gives_zero_match_rate_without_dividing_by_zero():
    result = match.match_items(stock_of(), usage_of(usage_row("Aspirin 75mg Tablets")))
    assert result.match_rate == 0.0 and result.matches.empty
    assert len(result.review) == 1  # the PMR product nobody stocks


def test_empty_usage_sends_everything_to_review():
    result = match.match_items(stock_of(stock_row("Aspirin 75mg Tablets")), usage_of())
    assert result.match_rate == 0.0
    assert result.matches.loc[0, "match_level"] == match.LEVEL_NONE


def test_planted_problem_files_through_the_pipeline():
    base_stock = load.load_source(R1, FIXTURES / "omnicell_robot_1.csv")
    robot = load.load_source(R1, FIXTURES / "omnicell_robot_1__item_not_in_pmr.csv")
    pmr = load.load_source(PMR, FIXTURES / "pmr_usage.csv")
    stock, usage = clean.clean_all({R1: robot, PMR: pmr})
    assert len(stock) == len(base_stock.data) + 1
    result = match.match_items(stock, usage)
    review_names = set(result.review.loc[result.review["side"] == "robot", "item_name"])
    assert review_names == {"Zzz Placeholder Drug 10mg Tablets"}
    # The six robot-2 products have no robot here, so they show up on the PMR side.
    assert (result.review["side"] == "pmr").sum() == 6

    extra = load.load_source(PMR, FIXTURES / "pmr_usage__item_not_in_robots.csv")
    stock, usage = clean.clean_all({R1: base_stock, PMR: extra})
    review = match.match_items(stock, usage).review
    assert "Warfarin 3mg Tablets" in set(review.loc[review["side"] == "pmr", "item_name"])


# ---- confirmed-match lookup -----------------------------------------------------------------


def test_confirmed_match_settles_a_review_item_next_time():
    stock = stock_of(stock_row("Lipitor 20mg Tablets"))
    usage = usage_of(usage_row("Atorvastatin 20mg Tablets"))
    first = match.match_items(stock, usage)
    assert first.matches.loc[0, "match_status"] == match.REVIEW
    confirmed = match.confirm(
        match.empty_confirmed(),
        first.matches.loc[0, "stock_key"],
        "atorvastatin tablets|20mg",
        note="brand of atorvastatin",
        confirmed_on="2026-10-29",
    )
    second = match.match_items(stock, usage, confirmed=confirmed)
    row = second.matches.iloc[0]
    assert row["match_status"] == match.MATCHED and bool(row["confirmed"]) is True
    assert row["pmr_key"] == "atorvastatin tablets|20mg"
    assert row["match_level"] == match.LEVEL_NONE  # the automatic level is kept for the record
    assert second.match_rate == 1.0 and second.review.empty


def test_confirmed_no_match_removes_the_item_from_review_but_not_from_the_rate():
    stock = stock_of(stock_row("Zzz Placeholder 10mg Tablets"))
    usage = usage_of(usage_row("Aspirin 75mg Tablets"))
    key = match.match_items(stock, usage).matches.loc[0, "stock_key"]
    confirmed = match.confirm(match.empty_confirmed(), key, "", decision="no_match")
    result = match.match_items(stock, usage, confirmed=confirmed)
    assert result.matches.loc[0, "match_status"] == match.NO_MATCH
    assert (result.review["side"] == "robot").sum() == 0
    assert result.match_rate == 0.0


def test_automatic_matches_are_never_overridden_by_the_lookup():
    stock = stock_of(stock_row("Aspirin 75mg Tablets"))
    usage = usage_of(usage_row("Aspirin 75mg Tablets"), usage_row("Other 5mg Tablets"))
    key = match.make_key("aspirin tablets", "75mg")
    confirmed = match.confirm(match.empty_confirmed(), key, "other tablets|5mg")
    result = match.match_items(stock, usage, confirmed=confirmed)
    assert result.matches.loc[0, "pmr_key"] == "aspirin tablets|75mg"
    assert bool(result.matches.loc[0, "confirmed"]) is False


def test_stale_confirmation_goes_back_to_review_with_a_reason():
    stock = stock_of(stock_row("Lipitor 20mg Tablets"))
    usage = usage_of(usage_row("Simvastatin 20mg Tablets"))
    key = match.make_key("lipitor tablets", "20mg")
    confirmed = match.confirm(match.empty_confirmed(), key, "atorvastatin tablets|20mg")
    row = match.match_items(stock, usage, confirmed=confirmed).matches.iloc[0]
    assert row["match_status"] == match.REVIEW and "no longer" in row["reason"]


def test_confirm_replaces_an_earlier_decision_and_validates_input():
    table = match.confirm(match.empty_confirmed(), "k", "a|1mg")
    table = match.confirm(table, "k", "b|1mg")
    assert table["pmr_key"].tolist() == ["b|1mg"]
    table = match.confirm(table, "k", "ignored", decision="no_match")
    assert table.iloc[0]["pmr_key"] == "" and len(table) == 1
    with pytest.raises(ValueError):
        match.confirm(table, "k", "x", decision="maybe")
    with pytest.raises(ValueError):
        match.confirm(table, "k", "", decision="match")


def test_lookup_file_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "confirmed_matches.csv"
    assert match.read_confirmed(path).empty
    table = match.confirm(match.empty_confirmed(), "a|1mg", "b|1mg", note="same drug")
    match.write_confirmed(table, path)
    pd.testing.assert_frame_equal(match.read_confirmed(path), table)
    path.write_text("stock_key,pmr_key\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        match.read_confirmed(path)


def test_lookup_holds_no_patient_data_by_construction():
    assert set(match.CONFIRMED_COLUMNS) == {
        "stock_key",
        "pmr_key",
        "decision",
        "note",
        "confirmed_on",
    }


# ---- bulk decisions ------------------------------------------------------------------------------------


def bulk_scenario():
    stock = stock_of(
        stock_row("Zzz Placeholder 10mg Tablets"),  # nothing like it in the PMR
        stock_row("Atorvastatin 20mg Tablets"),  # a typo'd look-alike exists: level 3
        stock_row("Aspirin 75mg Tablets"),  # exact: matched
        stock_row(""),  # blank name
    )
    usage = usage_of(usage_row("Atorvastin 20mg Tablets"), usage_row("Aspirin 75mg Tablets"))
    return match.match_items(stock, usage)


def test_bulk_candidates_exclude_look_alikes_blank_names_and_matched_items():
    candidates = match.bulk_not_in_pmr_candidates(bulk_scenario())
    assert list(candidates["item_name"]) == ["Zzz Placeholder 10mg Tablets"]
    assert list(candidates.columns) == ["robot_id", "branch", "item_name", "stock_key", "reason"]


def test_bulk_candidates_are_distinct_per_stock_key():
    stock = stock_of(
        stock_row("Zzz 10mg Tablets", robot="robot_1"),
        stock_row("Zzz 10mg Tablets", robot="robot_2", branch="Branch B"),
    )
    result = match.match_items(stock, usage_of(usage_row("Aspirin 75mg Tablets")))
    assert len(match.bulk_not_in_pmr_candidates(result)) == 1


def test_bulk_candidates_skip_stale_earlier_confirmations():
    stock = stock_of(stock_row("Lipitor 20mg Tablets"))
    usage = usage_of(usage_row("Simvastatin 20mg Tablets"))
    key = match.make_key("lipitor tablets", "20mg")
    stale = match.confirm(match.empty_confirmed(), key, "atorvastatin tablets|20mg")
    result = match.match_items(stock, usage, confirmed=stale)
    assert match.bulk_not_in_pmr_candidates(result).empty


def test_confirm_all_not_in_pmr_records_decisions_and_settles_the_items():
    result = bulk_scenario()
    candidates = match.bulk_not_in_pmr_candidates(result)
    table = match.confirm_all_not_in_pmr(
        match.empty_confirmed(), candidates, confirmed_on="2026-10-30"
    )
    assert list(table["decision"]) == ["no_match"] and table.iloc[0]["pmr_key"] == ""
    assert table.iloc[0]["note"] == "bulk: no similar PMR product"
    stock = stock_of(stock_row("Zzz Placeholder 10mg Tablets"))
    again = match.match_items(stock, usage_of(usage_row("Aspirin 75mg Tablets")), confirmed=table)
    assert again.matches.loc[0, "match_status"] == match.NO_MATCH


def test_bulk_replaces_an_earlier_decision_for_the_same_item():
    result = bulk_scenario()
    candidates = match.bulk_not_in_pmr_candidates(result)
    key = candidates.iloc[0]["stock_key"]
    earlier = match.confirm(match.empty_confirmed(), key, "aspirin tablets|75mg")
    table = match.confirm_all_not_in_pmr(earlier, candidates)
    assert len(table) == 1 and table.iloc[0]["decision"] == "no_match"
