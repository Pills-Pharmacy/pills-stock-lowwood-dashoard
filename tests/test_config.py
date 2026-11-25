import ast
from pathlib import Path

import pytest

import config
import schema

ROOT = Path(__file__).resolve().parent.parent


def test_default_settings_and_labels():
    settings = config.DEFAULT_SETTINGS
    assert settings.values() == {
        "period_days": 90,
        "overstock_days": 180,
        "understock_days": 14,
        "classification_level": "combined_per_branch",
    }
    assert set(settings.labels().values()) == {"Assumption, to be confirmed"}
    assert settings.problems() == []


def test_with_value_changes_copy_only():
    changed = config.DEFAULT_SETTINGS.with_value("overstock_days", 365)
    assert changed.overstock_days.value == 365
    assert config.DEFAULT_SETTINGS.overstock_days.value == 180
    assert changed.overstock_days.label == config.ASSUMPTION
    with pytest.raises(KeyError):
        config.DEFAULT_SETTINGS.with_value("nope", 1)


@pytest.mark.parametrize(
    "name, value",
    [
        ("period_days", 0),
        ("understock_days", -1),
        ("overstock_days", 14),
        ("classification_level", "per_pharmacist"),
        ("period_days", True),
    ],
)
def test_invalid_settings_are_reported(name, value):
    assert config.DEFAULT_SETTINGS.with_value(name, value).problems()


def test_classification_level_accepts_both_options():
    for level in config.CLASSIFICATION_LEVELS:
        assert config.DEFAULT_SETTINGS.with_value("classification_level", level).problems() == []


def test_robot_registry_covers_every_robot_and_is_unconfirmed():
    assert set(config.ROBOT_REGISTRY) == set(schema.ROBOT_SOURCES.values())
    assert not any(info.confirmed for info in config.ROBOT_REGISTRY.values())


def test_column_map_loads_with_every_source():
    column_map = config.load_column_map()
    assert set(column_map) == set(schema.SOURCES)


def test_column_map_covers_every_internal_column_or_marks_it_optional():
    column_map = config.load_column_map()
    for source in schema.SOURCES:
        for internal in schema.REQUIRED[source]:
            assert column_map[source].get(internal), f"{source} lacks required {internal}"
        for internal in schema.OPTIONAL[source]:
            if internal not in column_map[source]:
                continue  # explicitly optional and not mapped
            assert column_map[source][internal], f"{source}.{internal} mapped to nothing"
    assert config.column_map_problems(column_map) == []


def test_column_map_problems_detects_gaps():
    column_map = config.load_column_map()
    column_map["pmr"] = {**column_map["pmr"], "dispense_date": []}
    column_map["omnicell_robot_1"] = {**column_map["omnicell_robot_1"], "bogus": ["x"]}
    column_map["omnicell_robot_2"] = {
        **column_map["omnicell_robot_2"],
        "unit": ["Pack"],  # already used for pack_size
    }
    problems = " | ".join(config.column_map_problems(column_map))
    assert "pmr: required column dispense_date" in problems
    assert "unknown internal column bogus" in problems
    assert "maps to both" in problems


def test_resolve_headers_is_tolerant_and_reports_unmapped():
    mapped, unmapped = config.resolve_headers(
        "omnicell_robot_1", [" Item Code ", "ITEM  DESCRIPTION", "Qty on hand", "Mystery"]
    )
    assert mapped == {
        " Item Code ": "item_code",
        "ITEM  DESCRIPTION": "item_name",
        "Qty on hand": "stock_qty",
    }
    assert unmapped == ["Mystery"]


def test_no_source_header_is_hard_coded_in_python_files():
    aliases = {
        alias
        for block in config.load_column_map().values()
        for aliases in block.values()
        for alias in aliases
    }
    # export.py and app.py only label output for people ("Branch", "Unit"); they never read a
    # source header, so their display strings may coincide with header guesses.
    presentation = {"export.py", "app.py"}
    offenders = []
    for path in ROOT.glob("*.py"):
        if path.name in presentation:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in aliases:
                offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
    assert offenders == []
