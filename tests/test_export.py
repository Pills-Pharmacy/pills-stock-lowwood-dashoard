import re
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

import analyse
import clean
import export
import load
import match
from config import DEFAULT_SETTINGS
from frames import stock_of, stock_row, usage_of, usage_row

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"
STAMP = "2026-10-29 12:00"
SHEETS = [
    "Summary",
    "Dead stock",
    "Overstocked",
    "Understocked",
    "Healthy",
    "Review",
    "Unclassified",
    "Usage no stock",
    "Validation",
    "Settings",
]


def pipeline(robot_1="omnicell_robot_1.csv", pmr="pmr_usage.csv", settings=DEFAULT_SETTINGS):
    results = {
        R1: load.load_source(R1, FIXTURES / robot_1),
        R2: load.load_source(R2, FIXTURES / "omnicell_robot_2.csv"),
        PMR: load.load_source(PMR, FIXTURES / pmr),
    }
    stock, usage = clean.clean_all(results)
    matching = match.match_items(stock, usage)
    analysis = analyse.analyse(matching, settings)
    issues = [i for r in results.values() for i in r.issues] + load.validate_across(results)
    return export.build_report(analysis, matching, issues, generated_at=STAMP), analysis, matching


def excel_sheets(report) -> dict[str, pd.DataFrame]:
    return pd.read_excel(BytesIO(export.to_excel_bytes(report)), sheet_name=None, dtype=object)


class _Tables(HTMLParser):
    """Counts <tr data-branch> rows per table id and records visible text."""

    def __init__(self):
        super().__init__()
        self.table = None
        self.rows: dict[str, list[str]] = {}
        self.text: list[str] = []
        self.external: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.table = attributes.get("id")
            self.rows[self.table] = []
        elif tag == "tr" and "data-branch" in attributes:
            self.rows[self.table].append(attributes["data-branch"])
        for name in ("src", "href"):
            if attributes.get(name):
                self.external.append(attributes[name])

    def handle_endtag(self, tag):
        if tag == "table":
            self.table = None

    def handle_data(self, data):
        self.text.append(data)


def parse_html(html: str) -> _Tables:
    parser = _Tables()
    parser.feed(html)
    return parser


# ---- the report object -------------------------------------------------------------------


def test_report_holds_every_table_and_the_baseline_counts():
    report, *_ = pipeline()
    assert list(report.tables) == [
        "dead_stock",
        "overstocked",
        "understocked",
        "healthy",
        "review",
        "unclassified",
        "usage_no_stock",
    ]
    assert report.counts == {
        "dead_stock": 0,
        "overstocked": 2,
        "understocked": 3,
        "healthy": 7,
        "review": 0,
        "unclassified": 0,
        "usage_no_stock": 0,
    }
    assert report.match_rate == 1.0 and report.matched == 12 and report.total_items == 12
    assert report.branches == ["Branch A", "Branch B"]


def test_summary_counts_equal_the_table_row_counts():
    report, *_ = pipeline(robot_1="omnicell_robot_1__item_not_in_pmr.csv")
    summary = dict(zip(report.summary["Measure"], report.summary["Value"]))
    assert summary["Overstocked"] == len(report.tables["overstocked"])
    assert summary["Review list"] == len(report.tables["review"]) > 0
    assert summary["Match rate"].startswith("92.3%") and "12 of 13" in summary["Match rate"]


def test_class_tables_use_friendly_labels_and_types():
    report, *_ = pipeline()
    table = report.tables["understocked"]
    assert list(table.columns) == list(export.ITEM_COLUMNS.values())
    assert table["Days of cover"].dtype.kind == "f"
    assert pd.api.types.is_datetime64_any_dtype(table["Earliest expiry"])
    assert set(table["Confirmed match"]) == {""}


def test_settings_table_lets_a_run_be_reproduced():
    report, *_ = pipeline(settings=DEFAULT_SETTINGS.with_value("overstock_days", 365))
    settings = report.settings.set_index("Setting")
    assert settings.loc["overstock_days", "Value"] == 365
    assert settings.loc["overstock_days", "Status"] == "Assumption, to be confirmed"
    assert settings.loc["classification_level", "Value"] == "combined_per_branch"
    assert settings.loc["usage_window_end", "Value"] == "2026-10-19"
    assert settings.loc["usage_window_start", "Value"] == "2026-07-22"
    assert settings.loc["generated_at", "Value"] == STAMP


def test_validation_rows_carry_counts_not_cell_values():
    report, *_ = pipeline(pmr="pmr_usage__negative_qty.csv")
    validation = report.validation
    row = validation[validation["Check"] == "negative_or_missing_qty"].iloc[0]
    assert row["Rows"] == 1 and "dispensed_qty" in row["Message"]
    assert (validation["Check"] == "warning").any()  # the analysis warning is included too


# ---- patient data never reaches the outputs ---------------------------------------------------


def test_the_guard_refuses_a_patient_like_column():
    tables = {"x": pd.DataFrame({"Item": ["a"], "Patient Tag": ["b"]})}
    with pytest.raises(ValueError, match="Patient Tag"):
        export.check_no_patient_columns(tables)
    for name in ("Email", "Mobile No.", "EPES ID", "Date of Birth"):
        with pytest.raises(ValueError):
            export.check_no_patient_columns({"x": pd.DataFrame({name: [1]})})
    export.check_no_patient_columns({"x": pd.DataFrame({"Item": ["a"], "Branch": ["b"]})})


def test_a_patient_column_in_a_result_table_stops_the_report(monkeypatch):
    monkeypatch.setitem(export.REVIEW_COLUMNS, "stock_key", "Patient Number")
    with pytest.raises(ValueError, match="patient-like"):
        pipeline()


def test_no_patient_column_name_or_value_appears_in_excel_or_html():
    report, *_ = pipeline()
    sheets = excel_sheets(report)
    html = export.to_html(report)
    texts = [html] + [
        " ".join(map(str, [*frame.columns, *frame.to_numpy().ravel()])) for frame in sheets.values()
    ]
    for text in texts:
        for forbidden in ("PT-FAKE", "Patient ID", "EPES", "patient_id"):
            assert forbidden not in text
    for frame in sheets.values():
        export.check_no_patient_columns({"sheet": frame})


def test_the_patient_warning_names_no_column():
    report, *_ = pipeline()
    message = report.validation[report.validation["Check"] == "patient_fields"]["Message"].iloc[0]
    assert "Patient ID" not in message and "dropped" in message


# ---- Excel ----------------------------------------------------------------------------------


def test_excel_has_one_sheet_per_class_the_review_list_and_settings():
    report, *_ = pipeline()
    workbook = load_workbook(BytesIO(export.to_excel_bytes(report)))
    assert workbook.sheetnames == SHEETS


def test_excel_rows_equal_report_rows_and_headers_are_the_labels():
    report, *_ = pipeline(robot_1="omnicell_robot_1__item_not_in_pmr.csv")
    sheets = excel_sheets(report)
    for key, (title, _) in export.TABLES.items():
        assert len(sheets[title]) == report.counts[key], title
        assert list(sheets[title].columns) == list(report.tables[key].columns)
    assert sheets["Review"]["Item"].str.contains("Zzz Placeholder").any()


def test_excel_values_match_the_hand_calculated_cover():
    report, *_ = pipeline()
    healthy = excel_sheets(report)["Healthy"]
    row = healthy[healthy["Item"] == "Paracetamol 500mg Tablets"].iloc[0]
    assert float(row["Days of cover"]) == pytest.approx(120.0)
    assert row["Branch"] == "Branch A" and row["Robot"] == "robot_1"


def test_excel_formatting_freeze_filter_and_number_formats():
    report, *_ = pipeline()
    sheet = load_workbook(BytesIO(export.to_excel_bytes(report)))["Understocked"]
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref
    assert sheet["A1"].font.bold
    headers = [cell.value for cell in sheet[1]]
    cover = sheet.cell(row=2, column=headers.index("Days of cover") + 1)
    assert cover.number_format == "0.0"
    expiry = sheet.cell(row=2, column=headers.index("Earliest expiry") + 1)
    assert expiry.number_format == "yyyy-mm-dd"


def test_excel_settings_sheet_round_trips_the_thresholds():
    report, *_ = pipeline()
    settings = excel_sheets(report)["Settings"].set_index("Setting")
    assert int(settings.loc["period_days", "Value"]) == 90
    assert int(settings.loc["understock_days", "Value"]) == 14


def test_excel_is_returned_as_bytes_and_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report, *_ = pipeline()
    assert isinstance(export.to_excel_bytes(report), bytes)
    assert not list(tmp_path.iterdir())


# ---- HTML -----------------------------------------------------------------------------------


def test_html_is_self_contained():
    html = export.to_html(pipeline()[0])
    parsed = parse_html(html)
    assert parsed.external == []
    assert not re.search(r"(https?:)?//[\w.-]+\.\w", html.replace("<!doctype", ""))
    assert "<style>" in html and "<script>" in html


def test_html_shows_match_rate_before_any_table_and_has_a_branch_filter():
    report, *_ = pipeline()
    html = export.to_html(report)
    assert html.index("Match rate") < html.index("<table")
    assert "100.0%" in html and "12 of 12 robot items matched" in html
    assert 'id="branch"' in html
    assert '<option value="Branch A">' in html and '<option value="Branch B">' in html


def test_html_row_counts_equal_excel_row_counts():
    report, *_ = pipeline(robot_1="omnicell_robot_1__item_not_in_pmr.csv")
    parsed = parse_html(export.to_html(report))
    sheets = excel_sheets(report)
    for key, (title, _) in export.TABLES.items():
        assert len(parsed.rows[f"tbl-{key}"]) == len(sheets[title]), title
    assert len(parsed.rows["tbl-settings"]) == len(sheets["Settings"])


def test_html_branch_row_counts_equal_excel_branch_counts():
    report, *_ = pipeline()
    parsed = parse_html(export.to_html(report))
    sheets = excel_sheets(report)
    for key in ("overstocked", "understocked", "healthy"):
        title = export.TABLES[key][0]
        for branch in ("Branch A", "Branch B"):
            assert parsed.rows[f"tbl-{key}"].count(branch) == int(
                (sheets[title]["Branch"] == branch).sum()
            ), (key, branch)


def test_html_tile_counts_equal_the_summary_counts():
    report, *_ = pipeline()
    html = export.to_html(report)
    for key, count in report.counts.items():
        assert re.search(rf'data-count-for="tbl-{key}">{count}</b>', html), key


def test_html_numbers_are_formatted_and_dates_are_iso():
    html = export.to_html(pipeline()[0])
    assert '<td class="num">120.0</td>' in html  # paracetamol days of cover
    assert re.search(r"<td>20\d\d-\d\d-\d\d</td>", html)


def test_html_escapes_item_names():
    stock = stock_of(stock_row("<script>alert(1)</script> 10mg Tablets", qty=5.0))
    usage = usage_of(usage_row("<script>alert(1)</script> 10mg Tablets", qty=9.0))
    matching = match.match_items(stock, usage)
    analysis = analyse.analyse(matching, as_of="2026-10-19")
    html = export.to_html(export.build_report(analysis, matching, generated_at=STAMP))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)" in html


def test_html_is_deterministic_for_a_fixed_timestamp():
    first = export.to_html(pipeline()[0])
    assert first == export.to_html(pipeline()[0]) and STAMP in first


def test_html_lists_warnings_and_hides_info_messages():
    report, *_ = pipeline(pmr="pmr_usage__short_range.csv")
    html = export.to_html(report)
    assert "Validation messages" in html and "understated" in html
    assert "Read as utf-8" not in html


def test_empty_results_still_render_in_both_formats():
    matching = match.match_items(stock_of(), usage_of())
    analysis = analyse.analyse(matching, as_of="2026-10-19")
    report = export.build_report(analysis, matching, generated_at=STAMP)
    assert report.match_rate == 0.0 and report.counts["healthy"] == 0
    html = export.to_html(report)
    assert "0.0%" in html and "None." in html
    assert load_workbook(BytesIO(export.to_excel_bytes(report))).sheetnames == SHEETS


def test_rows_without_a_branch_are_kept_visible_by_the_filter_rule():
    report, *_ = pipeline(pmr="pmr_usage__item_not_in_robots.csv")
    parsed = parse_html(export.to_html(report))
    assert "" in parsed.rows["tbl-review"]  # a PMR-side review row has no branch
    assert "node.hidden" not in export._TEMPLATE and "!own" in export._TEMPLATE


# ---- robot activity in the reports -----------------------------------------------------------------------------


ACTIVITY_FILES = [
    (FIXTURES / "omnicell_activity.csv").read_bytes(),
    (FIXTURES / "omnicell_activity__pull_2.csv").read_bytes(),
]
ACTIVITY_SHEETS = ["Activity summary", "Activity daily", "Activity changes", "Activity vs PMR"]
USAGE_SHEETS = ["Usage products", "Usage stopped", "Usage monthly", "Usage vs robot"]
SUGGEST_SHEETS = ["Sales groups", "Add to robot", "Review for removal", "Stock up", "Swap pairs"]


def activity_report():
    import pipeline

    files = {
        R1: (FIXTURES / "omnicell_robot_1.csv").read_bytes(),
        R2: (FIXTURES / "omnicell_robot_2.csv").read_bytes(),
        PMR: (FIXTURES / "pmr_usage.csv").read_bytes(),
    }
    result = pipeline.run_pipeline(files, generated_at=STAMP, activity_files=ACTIVITY_FILES)
    return result.report


def test_activity_sheets_follow_the_class_tables_and_only_appear_with_activity():
    with_activity = load_workbook(BytesIO(export.to_excel_bytes(activity_report()))).sheetnames
    # a report built through the pipeline also carries the PMR usage analysis
    assert with_activity == [
        *SHEETS[:8],
        *USAGE_SHEETS,
        *SUGGEST_SHEETS,
        *ACTIVITY_SHEETS,
        "Validation",
        "Settings",
    ]
    without = load_workbook(BytesIO(export.to_excel_bytes(pipeline_report()))).sheetnames
    assert without == SHEETS


def pipeline_report():
    report, *_ = pipeline()
    return report


def test_activity_excel_rows_equal_report_rows_and_html_rows():
    report = activity_report()
    sheets = excel_sheets(report)
    parsed = parse_html(export.to_html(report))
    for key, (title, _) in export.ACTIVITY_TABLES.items():
        assert len(sheets[title]) == report.counts[key], title
        assert len(parsed.rows[f"tbl-{key}"]) == len(sheets[title]), title
    assert report.counts["activity_changes"] == 3
    assert report.counts["activity_daily"] == 33


def test_activity_summary_row_and_values_appear_in_the_outputs():
    report = activity_report()
    summary = dict(zip(report.summary["Measure"], report.summary["Value"]))
    assert summary["Robot activity"] == "33 day(s), 2026-09-19 to 2026-10-21"
    html = export.to_html(report)
    assert "Activity vs PMR" in html and "Medimat1" in html
    assert "changed between pulls" in " ".join(report.validation["Message"])


def test_activity_excel_formats_dates_and_numbers():
    sheet = load_workbook(BytesIO(export.to_excel_bytes(activity_report())))["Activity daily"]
    headers = [cell.value for cell in sheet[1]]
    date_cell = sheet.cell(row=2, column=headers.index("Date") + 1)
    assert date_cell.number_format == "yyyy-mm-dd"
    average = sheet.cell(row=2, column=headers.index("Dispensed 7-day average") + 1)
    assert average.number_format == "0.0"


def test_no_patient_data_in_the_activity_outputs():
    report = activity_report()
    export.check_no_patient_columns(report.tables)
    text = export.to_html(report) + " ".join(
        " ".join(map(str, frame.to_numpy().ravel())) for frame in excel_sheets(report).values()
    )
    assert "PT-FAKE" not in text and "Patient ID" not in text


def test_printable_html_has_the_chosen_tables_a_print_button_and_escapes_cells():
    evil = pd.DataFrame({"Product": ["<script>alert(1)</script> TABS", "B"], "Scripts": [1234, 5]})
    page = export.printable_html(
        "Robot suggestions",
        "Lowood & test",
        [("Add to robot", "A <note>", evil), ("Empty", "", evil.iloc[0:0])],
    )
    assert "window.print()" in page and "@media print" in page
    assert "Add to robot (2)" in page and "Empty (0)" in page and "<p>None.</p>" in page
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; TABS" in page
    assert "Lowood &amp; test" in page and "1,234" in page


def test_printable_html_refuses_patient_columns():
    table = pd.DataFrame({"Patient Name": ["x"]})
    with pytest.raises(ValueError, match="patient"):
        export.printable_html("t", "s", [("Bad", "", table)])
