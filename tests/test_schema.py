import schema


def test_normalise_header_ignores_case_spacing_and_separators():
    variants = ["Item Code", " item  code ", "ITEM_CODE", "item-code", "﻿Item Code"]
    assert {schema.normalise_header(v) for v in variants} == {"item code"}


def test_every_source_has_required_and_optional_columns():
    for source in schema.SOURCES:
        assert schema.REQUIRED[source]
        assert not set(schema.REQUIRED[source]) & set(schema.OPTIONAL[source])


def test_robot_sources_map_to_robot_ids():
    assert set(schema.ROBOT_SOURCES.values()) == {"robot_1", "robot_2"}
    assert schema.PMR_SOURCE not in schema.ROBOT_SOURCES


def test_all_columns_is_union_without_duplicates():
    assert len(schema.ALL_COLUMNS) == len(set(schema.ALL_COLUMNS))
    assert {"item_name", "stock_qty", "dispensed_qty", "dispense_date"} <= set(schema.ALL_COLUMNS)
