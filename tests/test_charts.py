"""The charts build from the real result tables, in both modes, and carry their data."""

import pandas as pd
import pytest

import charts
import export
import pipeline
from tests.test_pipeline import files


@pytest.fixture(scope="module")
def result():
    return pipeline.run_pipeline(files(), generated_at="2026-10-29 12:00")


@pytest.mark.parametrize("dark", [False, True])
def test_every_stock_chart_builds_to_a_spec(result, dark):
    tables = result.report.tables
    spec = charts.class_counts(result.report.counts, dark).to_dict()
    assert spec["layer"]
    items = charts.classified_items(tables)
    assert set(items["Class"]) <= {"Overstocked", "Understocked", "Healthy"}
    assert charts.cover_histogram(items, 14, 180, dark).to_dict()["layer"]
    assert charts.top_cover(tables["overstocked"], ascending=False, dark=dark) is not None


@pytest.mark.parametrize("dark", [False, True])
def test_every_usage_chart_builds_to_a_spec(result, dark):
    tables = export.usage_tables(result.usage)
    assert charts.top_products(tables["usage_products"], dark=dark) is not None
    assert charts.monthly_scripts(tables["usage_monthly"], dark) is not None


def test_empty_tables_give_no_chart_instead_of_an_error():
    empty = pd.DataFrame()
    assert charts.cover_histogram(charts.classified_items({}), 14, 180) is None
    assert charts.top_cover(empty, ascending=True) is None
    assert charts.top_products(empty) is None
    assert charts.monthly_scripts(empty) is None
    assert charts.robot_vs_pmr(empty) is None
    assert charts.activity_lines(empty) is None


def test_class_colours_are_in_a_fixed_order_whatever_the_counts():
    spec = charts.class_counts({"healthy": 5}).to_dict()
    scale = spec["layer"][0]["encoding"]["color"]["scale"]
    assert scale["domain"] == charts.CLASS_ORDER
    assert scale["range"] == charts.CATEGORICAL["light"]


def test_robot_against_pmr_and_activity_lines_build():
    robot = pd.DataFrame(
        {"Month": ["2026-01"], "Robot per weekday": [10.0], "PMR scripts per weekday": [9.0]}
    )
    assert charts.robot_vs_pmr(robot) is not None
    daily = pd.DataFrame(
        {
            "activity_date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "system_name": ["A", "A"],
            "dispensed": [5, 7],
        }
    )
    assert charts.activity_lines(daily, dark=True).to_dict()["layer"]
