"""Drive the Streamlit page headlessly. File uploads are faked at the widget level.

There is one top-level tab per pharmacy (Lowood, McGregor). Results sit in tabs nested inside
each, so tabs are looked up by label.
"""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import streamlit as st
from streamlit.delta_generator import DeltaGenerator
from streamlit.testing.v1 import AppTest

import app
import config

FIXTURES = Path(__file__).parent / "fixtures"
APP_FILE = str(Path(__file__).parent.parent / "app.py")
LOWOOD, MCGREGOR = config.PHARMACIES
BASELINE = {"Lowood stock": "omnicell_robot_1.csv", "Lowood PMR": "pmr_usage.csv"}
RESULT_TABS = [
    "Summary",
    "Dead stock",
    "Overstock",
    "Understock",
    "Healthy",
    "Review",
    "Suggestions",
    "Usage",
    "Print",
]
ACTIVITY_PULLS = ["omnicell_activity.csv", "omnicell_activity__pull_2.csv"]


@pytest.fixture
def uploads(monkeypatch):
    """Fake the file_uploader widget. Keys are "<Pharmacy> stock|PMR|activity"; values are a
    fixture file name, raw bytes, or (activity only) a list of those. None means no upload."""
    chosen: dict[str, object] = dict(BASELINE)

    def upload(name):
        data = name if isinstance(name, bytes) else (FIXTURES / name).read_bytes()
        return SimpleNamespace(name="upload.csv", getvalue=lambda data=data: data)

    def fake(self, label, *args, **kwargs):
        kind = "activity" if "activity" in label else "PMR" if "PMR" in label else "stock"
        picked = chosen.get(f"{label.split()[0]} {kind}")
        if not picked:
            return None
        return [upload(n) for n in picked] if kind == "activity" else upload(picked)

    # st.file_uploader is bound when streamlit is imported, so patch it as well as the class.
    monkeypatch.setattr(DeltaGenerator, "file_uploader", fake)
    monkeypatch.setattr(st, "file_uploader", lambda label, *a, **k: fake(None, label, *a, **k))
    return chosen


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """The page may only write a pharmacy's confirmed-match file; send those to a scratch folder.

    AppTest re-executes app.py, which looks the folder up on the config module, so patch it there.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "CONFIRMED_MATCHES_DIR", tmp_path)
    monkeypatch.setattr(config, "CATALOGUE_PATH", tmp_path / "no_catalogue.csv")  # not the real one


def run_app(view: str = app.ANALYSIS_VIEW) -> AppTest:
    """Open the page (the Upload view stores the faked uploads), then switch to `view`."""
    at = AppTest.from_file(APP_FILE, default_timeout=120)
    at.run()
    if view != app.UPLOAD_VIEW:
        at.session_state["view"] = view
        at.run()
    return at


def switch(at: AppTest, view: str) -> AppTest:
    at.session_state["view"] = view
    return at.run()


def tab(at: AppTest, label: str, nth: int = 0):
    return [t for t in at.tabs if t.label == label][nth]


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def tab_metrics(tab_) -> dict[str, str]:
    return {m.label: m.value for m in tab_.metric}


def items(tab_) -> set[str]:
    return set(tab_.dataframe[0].value["Item"])


def texts(elements) -> list[str]:
    return [e.value for e in elements]


CATALOGUE_ERROR = "robot catalogue was not applied"


def real_errors(at: AppTest) -> list:
    """Errors other than the catalogue banner (the fixtures have no matching catalogue)."""
    return [e for e in at.error if CATALOGUE_ERROR not in e.value]


# ---- structure and empty states ------------------------------------------------------------------


def test_the_page_has_one_tab_per_pharmacy_and_explains_what_to_do(uploads):
    uploads.clear()
    at = run_app()
    assert not at.exception
    assert at.title[0].value == "Robot stock audit"
    assert [t.label for t in at.tabs] == ["Lowood", "McGregor"]
    welcomes = texts(at.info)
    assert "Upload Lowood's PMR usage export" in welcomes[0]
    assert "Upload McGregor's PMR usage export" in welcomes[1]


def test_each_pharmacy_has_its_own_labelled_uploads():
    assert app.slot_label(LOWOOD, "omnicell_robot_1") == "Lowood robot stock export"
    assert app.slot_label(LOWOOD, "pmr") == "Lowood PMR usage export"
    assert app.slot_label(LOWOOD, "omnicell_activity") == "Lowood robot activity statistics"
    assert app.slot_label(MCGREGOR, "omnicell_robot_2") == "McGregor robot stock export"
    assert app.slot_label(MCGREGOR, "pmr") == "McGregor PMR usage export"


def test_criteria_are_in_the_sidebar_shared_and_show_the_assumption_label(uploads):
    at = run_app()
    inputs = {n.label: n.value for n in at.sidebar.number_input}
    assert inputs == {
        "Usage window (days)": 90,
        "Overstocked above (days of cover)": 180,
        "Understocked below (days of cover)": 14,
    }
    assert texts(at.sidebar.caption).count("Assumption, to be confirmed") == 4
    assert at.sidebar.selectbox(key="set_classification_level").value == "combined_per_branch"


def test_a_pharmacy_with_no_files_does_not_block_the_other(uploads):
    at = run_app()
    assert not at.exception
    assert any("Upload McGregor's" in i.value for i in at.info)  # the other tab is untouched
    assert metrics(at)["Match rate"] == "100.0%"


def test_missing_pmr_shows_an_error_and_no_result_tabs(uploads):
    uploads["Lowood PMR"] = None
    at = run_app()
    assert any("No PMR usage file could be read" in e.value for e in at.error)
    assert [t.label for t in at.tabs] == ["Lowood", "McGregor"]


# ---- a Lowood run --------------------------------------------------------------------------------------


def test_a_run_shows_match_rate_first_then_the_counts(uploads):
    at = run_app()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Lowood", *RESULT_TABS, "McGregor"]
    shown = metrics(at)
    assert next(iter(shown)) == "Match rate" and shown["Match rate"] == "100.0%"
    # six robot-1 items: paracetamol, atorvastatin, omeprazole healthy; metformin over;
    # amoxicillin and levothyroxine under. The six robot-2 products have no stock in Lowood.
    assert shown["Overstocked"] == "1" and shown["Understocked"] == "2" and shown["Healthy"] == "3"
    assert shown["Dead stock"] == "0" and shown["Review list"] == "6"
    assert shown["Usage with no stock"] == "6"


def test_the_pharmacy_label_is_the_branch_and_no_branch_column_is_asked_for(uploads):
    at = run_app()
    healthy = tab(at, "Healthy").dataframe[0].value
    assert set(healthy["Branch"]) == {"Lowood"}
    assert not any("no branch column" in w.value for w in at.warning)


def test_summary_shows_validation_messages_and_hides_patient_column_names(uploads):
    at = run_app()
    assert any("Patient-level column(s)" in w.value for w in at.warning)
    assert not any("Patient ID" in w.value for w in at.warning)


def test_tabs_show_the_class_tables_with_hand_checked_rows(uploads):
    at = run_app()
    assert items(tab(at, "Understock")) == {
        "Amoxicillin 500mg Capsules",
        "Levothyroxine 100μg Tablets",
    }
    assert items(tab(at, "Overstock")) == {"Metformin 500mg Tablets"}
    healthy = items(tab(at, "Healthy"))
    assert healthy == {
        "Paracetamol 500mg Tablets",
        "Atorvastatin 20mg Tablets",
        "Omeprazole 20mg Capsules",
    }
    assert tab(at, "Healthy").caption[0].value.endswith("3 item(s).")
    assert tab(at, "Dead stock").caption[0].value.endswith("0 item(s).")


def test_changing_a_criterion_re_runs_the_audit_automatically(uploads):
    at = run_app()
    at.sidebar.number_input(key="set_overstock_days").set_value(100).run()
    assert not at.exception
    assert metrics(at)["Overstocked"] == "2"  # paracetamol, 122 days of cover, is now over


def test_classification_level_can_be_switched(uploads):
    at = run_app()
    at.sidebar.selectbox(key="set_classification_level").select("per_robot").run()
    assert not at.exception and metrics(at)["Overstocked"] == "1"


def test_stock_count_option_is_off_by_default_and_changes_the_stock_when_on(uploads):
    at = run_app()
    assert at.sidebar.checkbox(key="opt_stock_packs").value is False
    before = tab(at, "Healthy").dataframe[0].value
    at.sidebar.checkbox(key="opt_stock_packs").check().run()
    assert not at.exception
    after = tab(at, "Healthy").dataframe[0].value
    assert list(after["Stock qty"]) != list(before["Stock qty"])


def test_downloads_are_offered_for_both_reports_with_the_pharmacy_in_the_key(uploads):
    at = run_app()
    buttons = at.get("download_button")
    assert [b.proto.label for b in buttons] == [
        "Excel report",
        "HTML report (opens without the app)",
    ]
    assert not at.exception


# ---- review, confirming and the bulk decision ------------------------------------------------------------


def test_review_tab_lists_unmatched_items_and_the_match_rate_counts_them(uploads):
    uploads["Lowood stock"] = "omnicell_robot_1__item_not_in_pmr.csv"
    at = run_app()
    assert metrics(at)["Match rate"] == "85.7%"
    review = tab(at, "Review").dataframe[0].value
    assert "Zzz Placeholder Drug 10mg Tablets" in set(review["Item"])
    assert review.loc[review["Item"].str.startswith("Zzz"), "Robot"].iloc[0] == "robot_1"
    assert at.selectbox(key="lowood_confirm_item") is not None


def test_confirming_a_decision_writes_only_that_pharmacys_file_and_re_runs(uploads, tmp_path):
    uploads["Lowood stock"] = "omnicell_robot_1__item_not_in_pmr.csv"
    at = run_app()
    at.selectbox(key="lowood_confirm_choice").select("(not in the PMR)")
    at.button(key="lowood_confirm_save").click().run()
    assert not at.exception
    assert sorted(p.name for p in tmp_path.iterdir()) == ["confirmed_matches_lowood.csv"]
    assert metrics(at)["Dead stock"] == "1" and metrics(at)["Review list"] == "6"


def test_bulk_decision_marks_items_with_no_candidate_as_not_in_the_pmr(uploads, tmp_path):
    uploads["Lowood stock"] = "omnicell_robot_1__item_not_in_pmr.csv"
    at = run_app()
    assert any("1 item(s) have no similar product" in c.value for c in tab(at, "Review").caption)
    button = at.button(key="lowood_bulk_save")
    assert button.disabled is False
    button.click().run()
    assert not at.exception
    assert sorted(p.name for p in tmp_path.iterdir()) == ["confirmed_matches_lowood.csv"]
    assert "bulk: no similar PMR product" in (tmp_path / "confirmed_matches_lowood.csv").read_text()
    assert metrics(at)["Dead stock"] == "1"


def test_bulk_decision_is_switched_off_when_the_pmr_is_shorter_than_the_period(uploads, tmp_path):
    uploads["Lowood stock"] = "omnicell_robot_1__item_not_in_pmr.csv"
    uploads["Lowood PMR"] = "pmr_usage__short_range.csv"
    at = run_app()
    assert at.button(key="lowood_bulk_save").disabled is True
    assert any("bulk marking is switched off" in w.value for w in at.warning)
    assert list(tmp_path.iterdir()) == []


def test_no_bulk_button_when_nothing_is_unmatched(uploads):
    uploads["Lowood PMR"] = "pmr_usage__grouped_report.csv"
    uploads["Lowood stock"] = "omnicell_robot_1.csv"
    at = run_app()
    assert not any(b.key == "lowood_bulk_save" for b in at.button)


# ---- each pharmacy is analysed on its own -------------------------------------------------------------------


def test_two_pharmacies_are_analysed_separately_from_their_own_files(uploads):
    uploads["McGregor stock"] = "omnicell_robot_2.csv"
    uploads["McGregor PMR"] = "pmr_usage.csv"
    at = run_app()
    assert not at.exception
    assert [t.label for t in at.tabs] == [
        "Lowood",
        *RESULT_TABS,
        "McGregor",
        *RESULT_TABS,
    ]
    lowood, mcgregor = tab(at, "Summary", 0), tab(at, "Summary", 1)
    assert tab_metrics(lowood)["Overstocked"] == "1" and tab_metrics(lowood)["Understocked"] == "2"
    # robot 2 holds items 7 to 12: amlodipine over; ceftriaxone under; four healthy
    assert tab_metrics(mcgregor)["Overstocked"] == "1"
    assert tab_metrics(mcgregor)["Understocked"] == "1" and tab_metrics(mcgregor)["Healthy"] == "4"
    assert set(tab(at, "Healthy", 0).dataframe[0].value["Branch"]) == {"Lowood"}
    assert set(tab(at, "Healthy", 1).dataframe[0].value["Branch"]) == {"McGregor"}


def test_decisions_are_kept_per_pharmacy(uploads, tmp_path):
    uploads["Lowood stock"] = "omnicell_robot_1__item_not_in_pmr.csv"
    uploads["McGregor stock"] = "omnicell_robot_2.csv"
    uploads["McGregor PMR"] = "pmr_usage.csv"
    at = run_app()
    at.button(key="lowood_bulk_save").click().run()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["confirmed_matches_lowood.csv"]
    assert not (tmp_path / "confirmed_matches_mcgregor.csv").exists()


def test_a_file_for_one_pharmacy_does_not_leak_into_the_other(uploads):
    uploads["McGregor stock"] = None
    at = run_app()
    assert not any(m.label == "Match rate" for m in tab(at, "McGregor").metric)


# ---- the Lowood PMR report layout (whole months) -------------------------------------------------------------


def test_a_month_only_pmr_report_is_read_and_counted_in_whole_months(uploads):
    uploads["Lowood PMR"] = "pmr_usage__grouped_report.csv"
    at = run_app()
    assert not at.exception and not real_errors(at)
    shown = metrics(at)
    assert shown["Match rate"] == "100.0%" and shown["Healthy"] == "3"
    assert shown["Overstocked"] == "1" and shown["Understocked"] == "2"
    assert any("latest month (Oct 2026) was left out" in w.value for w in at.warning)
    details = [d.value for d in tab(at, "Summary").dataframe if "Message" in d.value.columns]
    assert any(details[0]["Message"].str.contains("flattened"))


def test_the_latest_month_can_be_counted_on_request(uploads):
    uploads["Lowood PMR"] = "pmr_usage__grouped_report.csv"
    at = run_app()
    assert at.sidebar.checkbox(key="opt_latest_month").value is False
    at.sidebar.checkbox(key="opt_latest_month").check().run()
    assert not at.exception
    assert not any("left out because it may be incomplete" in w.value for w in at.warning)
    settings = next(d.value for d in at.get("dataframe") if "Setting" in d.value.columns)
    window_end = settings.loc[settings["Setting"] == "usage_window_end", "Value"].iloc[0]
    assert window_end == "2026-10-31"


# ---- robot activity statistics ---------------------------------------------------------------------------------


def test_activity_on_its_own_shows_only_the_activity_view_for_that_pharmacy(uploads):
    uploads.clear()
    uploads["Lowood activity"] = ACTIVITY_PULLS
    at = run_app()
    assert not at.exception and not real_errors(at)
    assert [t.label for t in at.tabs] == ["Lowood", "McGregor"]
    assert any("Only robot activity statistics were uploaded" in i.value for i in at.info)
    shown = metrics(at)
    assert set(shown) == {"Dispensed", "Stocked", "Rearranged", "Quiet days"}
    assert shown["Quiet days"] == "10"
    assert any("changed between pulls" in w.value for w in at.warning)


def test_activity_adds_a_tab_next_to_the_stock_audit(uploads):
    uploads["Lowood activity"] = ACTIVITY_PULLS
    at = run_app()
    assert not at.exception
    assert [t.label for t in at.tabs] == [
        "Lowood",
        *RESULT_TABS[:-1],
        "Robot activity",
        "Print",
        "McGregor",
    ]
    assert metrics(at)["Match rate"] == "100.0%"  # the stock audit is untouched
    activity = tab(at, "Robot activity")
    assert {m.label for m in activity.metric} == {
        "Dispensed",
        "Stocked",
        "Rearranged",
        "Quiet days",
    }
    assert any("Over the" in c.value and "both cover" in c.value for c in activity.caption)
    assert len(activity.dataframe) >= 3


def test_stock_errors_do_not_hide_the_activity_results(uploads):
    uploads["Lowood PMR"] = None
    uploads["Lowood activity"] = ACTIVITY_PULLS
    at = run_app()
    assert any("No PMR usage file could be read" in e.value for e in at.error)
    assert any(h.value == "Robot activity" for h in at.subheader)
    assert {"Dispensed", "Stocked"} <= set(metrics(at))


def test_a_wrong_file_in_the_activity_slot_is_explained(uploads):
    uploads["Lowood activity"] = ["omnicell_robot_1__bestand_format.csv"]
    at = run_app()
    assert not at.exception
    assert any("No robot activity file could be read" in e.value for e in at.error)
    assert any("stock listing" in e.value for e in tab(at, "Robot activity").error)


def test_activity_for_a_month_only_pmr_is_compared_by_whole_month(uploads):
    uploads["Lowood PMR"] = "pmr_usage__grouped_report.csv"
    sept = "\n".join(
        ["DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED"]
        + [f"{d:02d}.09.26;24;Medimat1;10;0;0" for d in range(30, 0, -1)]
    )
    uploads["Lowood activity"] = [(sept + "\n").encode()]
    at = run_app()
    assert not at.exception
    captions = texts(tab(at, "Robot activity").caption)
    assert any("Over the 1 month(s) both cover" in c for c in captions)


# ---- mapping screens, per pharmacy ----------------------------------------------------------------------------


def test_the_mapping_screen_is_inside_each_pharmacy_tab(uploads):
    uploads["Lowood activity"] = ACTIVITY_PULLS
    at = run_app(app.UPLOAD_VIEW)
    keys = {s.key for s in at.selectbox}
    assert (
        "lowood_map_omnicell_robot_1_stock_qty" in keys and "lowood_map_pmr_dispense_date" in keys
    )
    assert "lowood_map_omnicell_activity_dispensed" in keys
    assert not any(k.startswith("mcgregor_map_") for k in keys)  # nothing uploaded for McGregor
    options = at.selectbox(key="lowood_map_pmr_dispense_date").options
    assert "Patient ID" not in options and "date_dispensed" in options


def test_a_mapping_override_makes_an_unrecognised_file_usable(uploads):
    uploads["Lowood stock"] = b"Thing,Amount\nAtorvastatin 20mg Tablets,100\n"
    at = run_app()
    assert any("No robot stock file could be read" in e.value for e in at.error)
    switch(at, app.UPLOAD_VIEW)
    at.selectbox(key="lowood_map_omnicell_robot_1_item_name").select("Thing")
    at.selectbox(key="lowood_map_omnicell_robot_1_stock_qty").select("Amount").run()
    switch(at, app.ANALYSIS_VIEW)  # the choice carries over to the analysis
    assert not at.exception and not real_errors(at)
    assert metrics(at)["Match rate"] == "100.0%"


def test_mcgregor_can_be_mapped_on_its_own_with_a_different_format(uploads):
    uploads["McGregor stock"] = b"Drug,On hand\nAtorvastatin 20mg Tablets,100\n"
    uploads["McGregor PMR"] = (
        b"Product,Qty Supplied,Date Supplied\nATORVASTATIN 20MG TABLETS,28,03/09/2026\n"
    )
    uploads["Lowood stock"] = uploads["Lowood PMR"] = None
    at = run_app()
    assert any("No robot stock file could be read" in e.value for e in at.error)
    switch(at, app.UPLOAD_VIEW)
    at.selectbox(key="mcgregor_map_omnicell_robot_2_item_name").select("Drug")
    at.selectbox(key="mcgregor_map_omnicell_robot_2_stock_qty").select("On hand")
    at.selectbox(key="mcgregor_map_pmr_item_name").select("Product")
    at.selectbox(key="mcgregor_map_pmr_dispensed_qty").select("Qty Supplied")
    at.selectbox(key="mcgregor_map_pmr_dispense_date").select("Date Supplied").run()
    switch(at, app.ANALYSIS_VIEW)
    assert not at.exception and not real_errors(at)
    assert metrics(at)["Match rate"] == "100.0%"
    # one 28-tablet script over the window leaves 100 in stock: 321 days of cover, overstocked
    assert metrics(at)["Overstocked"] == "1"
    assert tab(at, "Overstock").dataframe[0].value["Branch"].iloc[0] == "McGregor"


# ---- a PMR without a stock file: the usage analysis ----------------------------------------------------------------

GROUPED = "pmr_usage__grouped_report.csv"


def usage_only(uploads, pmr=GROUPED, **more):
    uploads.clear()
    uploads["Lowood PMR"] = pmr
    uploads.update(more)
    return run_app()


def test_a_pmr_alone_gives_a_usage_analysis_not_an_error(uploads):
    at = usage_only(uploads)
    assert not at.exception and not real_errors(at)
    assert [t.label for t in at.tabs] == [
        "Lowood",
        "Summary",
        "Suggestions",
        "Usage",
        "Print",
        "McGregor",
    ]
    assert any("No stock file was uploaded, so stock is not classified" in i.value for i in at.info)


def test_the_usage_only_summary_shows_usage_figures_and_no_stock_classes(uploads):
    at = usage_only(uploads)
    shown = tab_metrics(tab(at, "Summary"))
    assert shown == {
        "Scripts in the window": "72",
        "Products dispensed": "12",
        "Stopped products": "0",
        "Months in the PMR": "7",
    }
    assert "Match rate" not in metrics(at) and "Overstocked" not in metrics(at)
    assert any("Stock was not classified" in c.value for c in tab(at, "Summary").caption)


def test_the_usage_tab_lists_products_trends_and_months(uploads):
    at = usage_only(uploads)
    usage = tab(at, "Usage")
    assert {m.label for m in usage.metric} == {"Rising", "Falling", "Steady", "New", "Few scripts"}
    products = usage.dataframe[0].value
    assert list(products.columns)[:3] == ["Product", "Scripts", "Quantity"]
    assert len(products) == 12 and products["Scripts"].is_monotonic_decreasing
    assert any("Usage window 2026-07-01 to 2026-09-30 (92 days" in c.value for c in usage.caption)
    assert any(h.value == "Scripts per month" for h in usage.subheader)


def test_the_usage_tab_names_the_window_and_warns_about_the_latest_month(uploads):
    at = usage_only(uploads)
    assert any("latest month (Oct 2026) was left out" in w.value for w in at.warning)
    at.sidebar.checkbox(key="opt_latest_month").check().run()
    assert not at.exception
    assert any("Usage window 2026-08-01 to 2026-10-31" in c.value for c in tab(at, "Usage").caption)


def test_usage_only_downloads_are_named_for_the_usage_analysis(uploads):
    at = usage_only(uploads)
    buttons = at.get("download_button")
    assert [b.proto.label for b in buttons] == [
        "Excel report",
        "HTML report (opens without the app)",
    ]
    assert not at.exception


def test_usage_only_with_activity_adds_the_activity_tab_and_the_weekday_check(uploads):
    sept = "\n".join(
        ["DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED"]
        + [f"{d:02d}.09.26;24;Medimat1;200;0;0" for d in range(30, 0, -1)]
    )
    at = usage_only(uploads, **{"Lowood activity": [(sept + "\n").encode()]})
    assert not at.exception and not real_errors(at)
    assert [t.label for t in at.tabs] == [
        "Lowood",
        "Summary",
        "Suggestions",
        "Usage",
        "Robot activity",
        "Print",
        "McGregor",
    ]
    assert any(h.value == "Robot against the PMR, per weekday" for h in tab(at, "Usage").subheader)
    assert {"Dispensed", "Stocked"} <= {m.label for m in tab(at, "Robot activity").metric}


def test_the_full_audit_also_has_a_usage_tab(uploads):
    uploads["Lowood PMR"] = GROUPED
    at = run_app()
    assert [t.label for t in at.tabs] == ["Lowood", *RESULT_TABS, "McGregor"]
    assert tab(at, "Usage").dataframe[0].value["Product"].nunique() == 12
    assert metrics(at)["Match rate"] == "100.0%"


def test_a_stock_file_that_cannot_be_read_is_still_an_error_even_with_a_pmr(uploads):
    uploads["Lowood stock"] = "omnicell_robot_1__missing_required_column.csv"
    at = run_app()
    assert any("No robot stock file could be read" in e.value for e in at.error)
    assert not any("No stock file was uploaded" in i.value for i in at.info)


def test_pharmacies_can_differ_one_usage_only_one_full(uploads):
    uploads.clear()
    uploads["Lowood PMR"] = GROUPED
    uploads["McGregor stock"] = "omnicell_robot_2.csv"
    uploads["McGregor PMR"] = "pmr_usage.csv"
    at = run_app()
    assert not at.exception and not real_errors(at)
    labels = [t.label for t in at.tabs]
    assert labels == [
        "Lowood",
        "Summary",
        "Suggestions",
        "Usage",
        "Print",
        "McGregor",
        *RESULT_TABS,
    ]
    assert "Scripts in the window" in tab_metrics(tab(at, "Summary", 0))
    assert "Match rate" in tab_metrics(tab(at, "Summary", 1))


# ---- upload and analysis are separate views --------------------------------------------------------------


def test_the_upload_view_shows_the_check_and_no_analysis(uploads):
    at = run_app(app.UPLOAD_VIEW)
    assert not at.exception
    assert not at.metric  # no results on the upload step
    assert any("Upload check" in s.value for s in at.subheader)
    assert any("ready" in m.value for m in [*at.success, *at.warning])
    assert at.button(key="lowood_go").disabled is False


def test_the_analysis_view_has_no_upload_boxes_and_keeps_the_files(uploads):
    at = run_app()
    assert not at.exception
    assert metrics(at)["Match rate"] == "100.0%"
    switch(at, app.UPLOAD_VIEW)  # going back: the files are still there
    assert any("Lowood robot stock export" in m.value for m in at.markdown)
    assert at.button(key="lowood_rm_pmr")


def test_a_missing_required_column_blocks_the_go_button_and_names_the_fix(uploads):
    uploads["Lowood PMR"] = b"Foo,Bar\n1,2\n"
    at = run_app(app.UPLOAD_VIEW)
    assert at.button(key="lowood_go").disabled
    assert any("cannot be analysed yet" in e.value for e in at.error)
    assert any("Required column(s) not found" in e.value for e in at.error)


def test_an_unknown_header_gets_a_did_you_mean_button_that_fixes_the_mapping(uploads):
    uploads["Lowood PMR"] = (
        b"Product Name,Date Dispensed,Qty Out\nAtorvastatin 20mg Tablets,2026-09-03,28\n"
    )
    at = run_app(app.UPLOAD_VIEW)
    button = next(b for b in at.button if b.label == "Use 'Qty Out' as dispensed_qty")
    button.click().run()
    assert at.selectbox(key="lowood_map_pmr_dispensed_qty").value == "Qty Out"
    assert not any(b.label.startswith("Use 'Qty Out'") for b in at.button)  # now mapped


def test_catalogue_is_optional_and_read_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CATALOGUE_PATH", tmp_path / "missing.csv")
    assert app.read_catalogue() is None and app.catalogue_stamp() == 0.0
    path = tmp_path / "robot_catalogue.csv"
    path.write_text("trade_name,pack_size\nAMLODIPINE 5MG TABLETS,28\n", encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGUE_PATH", path)
    assert len(app.read_catalogue().keys) == 1 and app.catalogue_stamp() > 0
    path.write_text("name,size\nX,1\n", encoding="utf-8")  # wrong header: ignored, not a crash
    assert app.read_catalogue() is None


@pytest.mark.parametrize(
    "content",
    [None, "trade_name,pack_size\n", "trade_name,pack_size\nUNRELATED PRODUCT 1MG,1\n", "a,b\n1,2\n"],
    ids=["missing", "empty", "no_match", "wrong_columns"],
)
def test_a_bad_catalogue_is_a_visible_error_on_the_suggestions_tab(uploads, tmp_path, monkeypatch, content):
    if content is not None:
        path = tmp_path / "catalogue.csv"
        path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(config, "CATALOGUE_PATH", path)
    at = run_app()
    assert any(CATALOGUE_ERROR in e.value for e in tab(at, "Suggestions").error)
    assert not any(CATALOGUE_ERROR in e.value for e in tab(at, "Summary").error)


def test_a_catalogue_that_matches_shows_no_error(uploads, tmp_path, monkeypatch):
    names = pd.read_csv(FIXTURES / "pmr_usage.csv", dtype=str)["product_name"].dropna().unique()
    path = tmp_path / "catalogue.csv"
    path.write_text("trade_name\n" + "\n".join(names) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGUE_PATH", path)
    at = run_app()
    assert not any(CATALOGUE_ERROR in e.value for e in at.error)


def test_filter_frame_combines_pick_range_and_contains_filters():
    table = pd.DataFrame(
        {
            "Product": ["AMLODIPINE 5MG", "AMLODIPINE 10MG", "CETIRIZINE 10MG", "FREESTYLE"],
            "In robot catalogue": ["Yes", "Yes", "Yes", "No"],
            "Scripts": [50, 5, 70, 20],
        }
    )
    f = app.filter_frame
    assert f(table, {}).equals(table)
    assert f(table, {"In robot catalogue": ("in", ["No"])})["Product"].tolist() == ["FREESTYLE"]
    assert f(table, {"Scripts": ("range", (10, 60))})["Scripts"].tolist() == [50, 20]
    both = {"Product": ("contains", "amlodipine"), "Scripts": ("range", (10, 60))}
    assert f(table, both)["Product"].tolist() == ["AMLODIPINE 5MG"]
    assert f(table, {"Product": ("contains", "5MG")}).shape[0] == 1  # not read as a pattern


def test_the_print_tab_lists_every_table_and_prints_the_chosen_ones(uploads):
    at = run_app()
    print_tab = tab(at, "Print")
    picker = print_tab.multiselect[0]
    labels = list(picker.options)
    for expected in ("Overstocked", "Review", "Usage products", "Add to robot", "Sales groups"):
        assert any(label.startswith(expected) for label in labels), expected
    assert not any(
        label.startswith("Dead stock") for label in labels
    )  # empty tables are not listed
    assert any("No table picked yet" in i.value for i in print_tab.info)
    picker.select(labels[0]).run()
    assert not at.exception
    assert not any("No table picked yet" in i.value for i in tab(at, "Print").info)
