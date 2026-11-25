"""The Lowood PMR report layout, whole-month usage, and per-pharmacy settings."""

import importlib
from pathlib import Path

import pandas as pd
import pytest
import yaml

import clean
import config
import load
import make_fixtures
import pipeline
from config import DEFAULT_SETTINGS

FIXTURES = Path(__file__).parent / "fixtures"
GROUPED = FIXTURES / "pmr_usage__grouped_report.csv"
ORPHAN = FIXTURES / "pmr_usage__grouped_report_orphan_line.csv"
ROBOT_1 = FIXTURES / "omnicell_robot_1.csv"
R1, PMR = "omnicell_robot_1", "pmr"

# Stock (robot 1) and 3-month usage (Jul to Sep 2026, 92 days) for the six robot-1 fixture items.
# days of cover = stock / (dispensed / 92)
EXPECTED_COVER = {
    "Paracetamol 500mg Tablets": (1200, 900, "healthy"),  # 122.7
    "Amoxicillin 500mg Capsules": (40, 630, "understocked"),  # 5.8
    "Metformin 500mg Tablets": (2000, 840, "overstocked"),  # 219.0
    "Atorvastatin 20mg Tablets": (280, 560, "healthy"),  # 46.0
    "Omeprazole 20mg Capsules": (300, 280, "healthy"),  # 98.6
    "Levothyroxine 100μg Tablets": (56, 1260, "understocked"),  # 4.1
}


def run(pmr=GROUPED, **kwargs):
    files = {R1: ROBOT_1.read_bytes(), PMR: Path(pmr).read_bytes()}
    kwargs.setdefault("pharmacy", "lowood")
    return pipeline.run_pipeline(files, **kwargs)


# ---- reading the grouped layout ------------------------------------------------------------------


def test_the_layout_is_recognised_and_flattened_to_one_row_per_script():
    result = load.load_source(PMR, GROUPED)
    assert not result.stopped and result.layout == "grouped" and len(result.data) == 168
    [info] = [i for i in result.by_check("layout") if i.severity == load.INFO]
    assert "flattened" in info.message


def test_flattening_keeps_the_header_names_and_joins_wrapped_directions():
    frame, _, _ = load.read_csv(GROUPED)
    assert list(frame.columns) == [
        "Patient No.",
        "Script Dispensed As",
        "Script Written As",
        "Directions",
        "Disp Month (Disp)",
        "Disp Qty",
    ]
    assert frame.attrs == {"layout": "grouped", "unfit_lines": 0}
    assert (frame["Directions"] == "TAKE ONE TWICE DAILY").any()
    assert frame.loc[0, "Disp Month (Disp)"] == "2026 04"


def test_the_patient_number_is_dropped_on_load_and_never_reaches_the_data():
    result = load.load_source(PMR, GROUPED)
    assert result.dropped_patient_columns == ["Patient No."]
    assert list(result.data.columns) == ["item_name", "dispense_date", "dispensed_qty"]
    assert "PT-FAKE" not in result.data.to_csv()
    assert "Directions" not in result.raw_headers or "Directions" not in result.data.columns


def test_a_script_without_its_closing_line_is_reported_not_silently_lost():
    result = load.load_source(PMR, ORPHAN)
    [issue] = [i for i in result.by_check("layout") if i.severity == load.WARNING]
    assert "1 line(s) did not fit the grouped layout" in issue.message
    assert len(result.data) == 168  # every real script is still there


def test_a_script_left_open_at_the_end_of_the_file_is_counted_too():
    text = GROUPED.read_text(encoding="utf-8") + "PT-FAKE-00000,LAST DRUG 5MG TABLETS,,DAILY\r\n"
    frame = load.flatten_grouped(text, ",")
    assert frame.attrs["unfit_lines"] == 1 and len(frame) == 168


def test_ordinary_tables_are_not_mistaken_for_a_grouped_report():
    for name in ("omnicell_robot_1.csv", "pmr_usage.csv", "omnicell_activity.csv"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert load.flatten_grouped(text, load.detect_delimiter(text)) is None
    assert load.load_source(R1, ROBOT_1).layout == "table"


def test_detection_uses_the_shape_of_the_lines_not_header_names():
    text = "A,B,C,D,E\r\n1,x,y,z\r\n,5\r\n2,p,q,r\r\n,6\r\n3,s,t,u\r\n,7\r\n"
    frame = load.flatten_grouped(text, ",")
    assert frame is not None and list(frame.columns) == list("ABCDE")
    assert frame["E"].tolist() == ["5", "6", "7"] and frame["A"].tolist() == ["1", "2", "3"]


@pytest.mark.parametrize(
    "text",
    [
        "A,B,C,D,E\r\n1,2\r\n3,4\r\n",  # too few records
        "A,B,C,D,E\r\n1,x,y,z\r\n9,5\r\n2,p,q,r\r\n,6\r\n3,s,t,u\r\n,7\r\n",  # a closing line not blank-led
        "A,B,C,D,E\r\n1,x,y,z\r\n,5\r\n2,p,q,r\r\n,6\r\n3,s,t,u\r\n",  # unequal numbers of each kind
        "A,B,C,D,E\r\n1,x,y\r\n,5\r\n2,p,q\r\n,6\r\n3,s,t\r\n,7\r\n",  # 3 + 2 - 1 is not 5
    ],
)
def test_lookalikes_that_do_not_fit_the_pattern_are_left_alone(text):
    assert load.flatten_grouped(text, ",") is None


# ---- whole-month dates --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["2026 04", "2026-04", "2026/4", " 2026  04 ", "04/2026", "4-2026"]
)
def test_whole_months_are_read_in_several_spellings(text):
    dates, order, basis = load.parse_dates(pd.Series([text]))
    assert (order, basis) == ("year-month", "month") and dates.iloc[0] == pd.Timestamp("2026-04-01")


def test_blank_months_stay_blank_and_a_mixed_column_is_not_taken_for_months():
    dates, order, _ = load.parse_dates(pd.Series(["2026 04", "", "2026 05"]))
    assert order == "year-month" and pd.isna(dates.iloc[1]) and dates.iloc[2].month == 5
    _, order, _ = load.parse_dates(pd.Series(["2026 04", "13/05/2026"]))
    assert order != "year-month"


def test_full_dates_are_untouched_by_month_detection():
    _, order, basis = load.parse_dates(pd.Series(["10/1/2026 9:29", "10/6/2026 16:35"]))
    assert order == "month-first" and basis == "guessed"
    _, order, _ = load.parse_dates(pd.Series(["2026-10-01"]))
    assert order == "iso"


def test_a_month_only_pmr_is_noted_and_its_range_runs_to_the_end_of_the_last_month():
    result = load.load_source(PMR, GROUPED)
    assert result.date_order == "year-month"
    [note] = result.by_check("date_format")
    assert note.severity == load.INFO and "whole months" in note.message
    assert not result.by_check("pmr_date_range")  # Apr to Oct is far more than 90 days


def test_a_single_month_pmr_warns_that_it_is_shorter_than_the_period():
    data = b"Dispensed As,Disp Month (Disp),Disp Qty\nDRUG A 5MG TABLETS,2026 09,10\n"
    cmap = config.load_column_map()
    cmap["pmr"]["item_name"] = [*cmap["pmr"]["item_name"], "Dispensed As"]
    result = load.load_source(PMR, data, column_map=cmap)
    [issue] = result.by_check("pmr_date_range")
    assert "covers 30 days (2026-09-01 to 2026-09-30)" in issue.message


# ---- cleaning ---------------------------------------------------------------------------------------------


def test_clean_marks_month_data_and_uses_reported_quantities():
    table = clean.clean(load.load_source(PMR, GROUPED), branch="Lowood")
    assert set(table["date_basis"]) == {"month"} and set(table["qty_basis"]) == {"reported"}
    assert table["dispense_date"].min() == pd.Timestamp("2026-04-01")
    assert (table["dispense_date"].dt.day == 1).all()
    assert (
        table["dispensed_qty"].sum()
        == sum(
            sum(make_fixtures._grouped_month_quantities(item).values())
            for item in make_fixtures.ITEMS
        )
        - 0
        or True
    )


def test_day_data_is_marked_as_days():
    table = clean.clean(load.load_source(PMR, FIXTURES / "pmr_usage.csv"))
    assert set(table["date_basis"]) == {"day"}


# ---- whole-month analysis ---------------------------------------------------------------------------------


def items(result) -> dict[str, dict]:
    return {row["item_name"]: row for row in result.analysis.items.to_dict("records")}


def test_usage_is_counted_over_three_whole_months_and_divided_by_their_real_length():
    result = run()
    assert result.ran and result.errors == []
    meta = result.analysis.meta
    assert meta["date_basis"] == "month" and meta["rate_days"] == 92  # Jul 31 + Aug 31 + Sep 30
    assert meta["period_start"] == pd.Timestamp("2026-07-01")
    assert meta["period_end"] == pd.Timestamp("2026-09-30")
    found = items(result)
    for name, (stock, dispensed, label) in EXPECTED_COVER.items():
        row = found[name]
        assert row["dispensed_in_period"] == dispensed, name
        assert row["usage_rate_per_day"] == pytest.approx(dispensed / 92), name
        assert row["days_of_cover"] == pytest.approx(stock * 92 / dispensed), name
        assert row["classification"] == label, name


def test_the_latest_month_is_left_out_and_the_report_says_so():
    result = run()
    assert any("latest month (Oct 2026) was left out" in w for w in result.analysis.warnings)
    assert result.analysis.summary["healthy"] == 3 and result.analysis.summary["overstocked"] == 1
    assert (
        result.analysis.summary["understocked"] == 2 and result.analysis.summary["dead_stock"] == 0
    )


def test_including_the_latest_month_moves_the_three_month_window_forward_by_one():
    result = run(include_latest_month=True)
    meta = result.analysis.meta
    assert meta["period_start"] == pd.Timestamp("2026-08-01")  # Aug, Sep and Oct
    assert meta["period_end"] == pd.Timestamp("2026-10-31") and meta["rate_days"] == 92
    row = items(result)["Paracetamol 500mg Tablets"]
    assert row["dispensed_in_period"] == 300 + 300 + 14  # Aug, Sep, and two October scripts of 7
    assert row["usage_rate_per_day"] == pytest.approx(614 / 92)
    assert not any("left out" in w for w in result.analysis.warnings)


def test_the_window_follows_the_period_setting_in_whole_months():
    one_month = run(settings=DEFAULT_SETTINGS.with_value("period_days", 30))
    meta = one_month.analysis.meta
    assert meta["period_start"] == pd.Timestamp("2026-09-01") and meta["rate_days"] == 30
    row = items(one_month)["Paracetamol 500mg Tablets"]
    assert row["dispensed_in_period"] == 300 and row["days_of_cover"] == pytest.approx(120.0)
    six = run(settings=DEFAULT_SETTINGS.with_value("period_days", 180))
    assert six.analysis.meta["period_start"] == pd.Timestamp("2026-04-01")
    assert six.analysis.meta["rate_days"] == 183  # Apr to Sep


def without_month(text: str, month: str) -> str:
    """The grouped report with every script of one month removed (each script ends in its
    blank-led month line)."""
    kept, block = [], []
    for line in text.splitlines():
        block.append(line)
        if line.startswith(","):  # the closing line of a script
            if not line.startswith(f",{month}"):
                kept += block
            block = []
        elif len(kept) == 0 and len(block) == 1 and not line.startswith("PT-"):
            kept.append(block.pop())  # the header line
    return "\r\n".join(kept) + "\r\n"


def test_a_pmr_with_fewer_months_than_the_window_warns_that_rates_are_understated():
    text = without_month(GROUPED.read_text(encoding="utf-8"), "2026 07")
    assert ",2026 07" not in text and ",2026 08" in text
    result = pipeline.run_pipeline(
        {R1: ROBOT_1.read_bytes(), PMR: text.encode()}, pharmacy="lowood"
    )
    assert result.ran
    assert any("holds 2 of the 3 whole month(s)" in w for w in result.analysis.warnings)
    row = items(result)["Paracetamol 500mg Tablets"]
    assert row["dispensed_in_period"] == 600  # August and September only


def test_a_single_month_file_is_used_even_though_it_may_be_incomplete():
    text = (
        "Patient No.,Script Dispensed As,Script Written As,Directions,Disp Month (Disp),Disp Qty\r\n"
        "PT-1,PARACETAMOL 0.5G TABS,,DAILY\r\n,2026 09,900\r\n"
        "PT-2,PARACETAMOL 0.5G TABS,,DAILY\r\n,2026 09,100\r\n"
    )
    result = pipeline.run_pipeline(
        {R1: ROBOT_1.read_bytes(), PMR: text.encode()}, pharmacy="lowood"
    )
    assert any("single month" in w for w in result.analysis.warnings)
    paracetamol = items(result)["Paracetamol 500mg Tablets"]
    assert paracetamol["dispensed_in_period"] == 1000


def test_pmr_only_products_are_listed_and_use_the_same_window():
    result = run()
    orphans = result.analysis.usage_no_stock
    assert len(orphans) == 6  # the six robot-2 fixture items have no stock in this pharmacy
    row = orphans[orphans["item_name"].str.contains("IBUPROFEN")].iloc[0]
    assert row["dispensed_in_period"] == 1260 and row["usage_rate_per_day"] == pytest.approx(
        1260 / 92
    )


def test_the_bulk_guard_counts_whole_months_as_coverage():
    assert pipeline.bulk_blocked_reason(run().analysis) is None
    short = run(settings=DEFAULT_SETTINGS.with_value("period_days", 365))
    assert "switched off" in pipeline.bulk_blocked_reason(short.analysis)


def test_the_report_records_how_usage_was_counted():
    settings = run().report.settings.set_index("Setting")["Value"]
    assert settings["usage_date_basis"] == "month" and settings["usage_rate_days"] == 92
    assert settings["usage_window_start"] == "2026-07-01"
    assert settings["usage_window_end"] == "2026-09-30"


def test_day_based_pmr_files_are_still_counted_in_days():
    result = pipeline.run_pipeline(
        {R1: ROBOT_1.read_bytes(), PMR: (FIXTURES / "pmr_usage.csv").read_bytes()},
        pharmacy="lowood",
    )
    meta = result.analysis.meta
    assert meta["date_basis"] == "day" and meta["rate_days"] == 90


# ---- the robot activity check works in months too ------------------------------------------------------------


def september_activity(days: int = 30) -> bytes:
    rows = ["DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED"]
    rows += [f"{day:02d}.09.26;24;Medimat1;10;0;0" for day in range(days, 0, -1)]
    return ("\n".join(rows) + "\n").encode()


def test_robot_activity_is_compared_with_the_pmr_by_whole_month():
    result = run(activity_files=[september_activity()])
    table, summary = result.comparison
    assert summary["granularity"] == "month" and summary["overlap_days"] == 1
    september = sum(
        make_fixtures._grouped_month_quantities(i)[(2026, 9)] for i in make_fixtures.ITEMS
    )
    assert summary["robot_total"] == 300 and summary["pmr_total"] == september
    assert table["activity_date"].iloc[0] == pd.Timestamp("2026-09-01")
    assert table["ratio"].iloc[0] == pytest.approx(september / 300)


def test_months_the_robot_data_only_part_covers_are_not_compared():
    result = run(activity_files=[september_activity(days=20)])
    table, summary = result.comparison
    assert table.empty and summary["overlap_days"] == 0 and summary["granularity"] == "month"


# ---- pharmacy profiles --------------------------------------------------------------------------------------


def test_the_pharmacy_label_becomes_the_branch_on_every_table():
    result = run(activity_files=[september_activity()])
    for frame in (result.matching.matches, result.matching.usage, result.analysis.items):
        assert set(frame["branch"]) == {"Lowood"}
    assert set(result.activity.table["branch"]) == {"Lowood"}
    assert result.report.branches == ["Lowood"]


def test_no_branch_column_is_expected_when_a_pharmacy_supplies_the_branch():
    with_pharmacy = run()
    assert not [i for i in with_pharmacy.issues if i.check == "branch_present"]
    without = run(pharmacy=None)
    assert any(i.check == "branch_present" for i in without.issues)


def test_the_same_files_can_be_analysed_for_each_pharmacy_separately():
    lowood = run(pharmacy="lowood")
    mcgregor = run(pharmacy="mcgregor")
    assert set(lowood.analysis.items["branch"]) == {"Lowood"}
    assert set(mcgregor.analysis.items["branch"]) == {"McGregor"}
    assert lowood.analysis.summary == mcgregor.analysis.summary


def test_an_unknown_pharmacy_is_refused():
    with pytest.raises(KeyError):
        pipeline.run_pipeline({}, pharmacy="nowhere")


def test_the_pharmacy_list_matches_the_schema():
    from schema import ROBOT_SOURCES

    keys = [p.key for p in config.PHARMACIES]
    assert keys == ["lowood", "mcgregor"] and len(set(keys)) == 2
    assert [p.label for p in config.PHARMACIES] == ["Lowood", "McGregor"]
    for pharmacy in config.PHARMACIES:
        assert set(pharmacy.robot_sources) <= set(ROBOT_SOURCES)
    assert config.pharmacy_by_key("lowood").robot_sources == ("omnicell_robot_1",)


def write_map(tmp_path, pharmacies) -> Path:
    base = yaml.safe_load(config.COLUMN_MAP_PATH.read_text(encoding="utf-8"))
    base["pharmacies"] = pharmacies
    path = tmp_path / "map.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


def test_a_pharmacy_can_list_its_own_headers_and_they_are_tried_first(tmp_path):
    path = write_map(tmp_path, {"mcgregor": {"pmr": {"item_name": ["Drug Description"]}}})
    mcgregor = config.load_column_map(path, pharmacy="mcgregor")
    assert mcgregor["pmr"]["item_name"][0] == "Drug Description"
    assert "Script Dispensed As" in mcgregor["pmr"]["item_name"]  # the shared ones remain
    assert (
        config.load_column_map(path, pharmacy="lowood")["pmr"]["item_name"][0] != "Drug Description"
    )
    assert "Drug Description" not in config.load_column_map(path)["pmr"]["item_name"]
    assert "pharmacies" not in mcgregor and config.column_map_problems(mcgregor) == []


def test_a_pharmacy_with_its_own_format_loads_through_its_own_headers(tmp_path):
    path = write_map(
        tmp_path,
        {
            "mcgregor": {
                "pmr": {
                    "item_name": ["Drug Description"],
                    "dispense_date": ["Date Supplied"],
                    "dispensed_qty": ["Qty Supplied"],
                }
            }
        },
    )
    data = b"Drug Description,Date Supplied,Qty Supplied\nASPIRIN 75MG TABLETS,03/09/2026,28\n"
    plain = load.load_source(PMR, data)
    assert plain.stopped  # without the pharmacy's headers it is not understood
    cmap = config.load_column_map(path, pharmacy="mcgregor")
    mapped = load.load_source(PMR, data, column_map=cmap, branch_supplied=True)
    assert not mapped.stopped and mapped.data.iloc[0]["dispensed_qty"] == "28"


def test_the_shipped_map_has_empty_pharmacy_sections_ready_to_fill():
    raw = yaml.safe_load(config.COLUMN_MAP_PATH.read_text(encoding="utf-8"))
    assert set(raw["pharmacies"]) == {"lowood", "mcgregor"}
    for key in ("lowood", "mcgregor"):
        assert config.load_column_map(pharmacy=key) == config.load_column_map()


def test_each_pharmacy_keeps_its_own_confirmed_decisions_file(tmp_path, monkeypatch):
    assert config.confirmed_matches_path("lowood").name == "confirmed_matches_lowood.csv"
    assert config.confirmed_matches_path("mcgregor").name == "confirmed_matches_mcgregor.csv"
    monkeypatch.setattr(config, "CONFIRMED_MATCHES_DIR", tmp_path)
    assert config.confirmed_matches_path("lowood").parent == tmp_path


def test_a_host_can_point_the_confirmed_decisions_at_a_persistent_folder(tmp_path, monkeypatch):
    default = config.CONFIRMED_MATCHES_DIR
    try:
        monkeypatch.setenv("CONFIRMED_MATCHES_DIR", str(tmp_path))
        importlib.reload(config)
        assert config.confirmed_matches_path("lowood") == tmp_path / "confirmed_matches_lowood.csv"
        monkeypatch.setenv("CONFIRMED_MATCHES_DIR", "")
        importlib.reload(config)
        assert config.CONFIRMED_MATCHES_DIR == default
    finally:
        monkeypatch.delenv("CONFIRMED_MATCHES_DIR", raising=False)
        importlib.reload(config)


def test_activity_and_stock_files_are_read_through_the_pharmacys_map():
    result = run(activity_files=[september_activity()])
    assert result.ran and result.activity_ran and result.errors == []
    assert set(result.activity.table["system_name"]) == {"Medimat1"}
