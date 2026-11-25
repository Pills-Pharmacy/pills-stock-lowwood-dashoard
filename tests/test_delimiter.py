from pathlib import Path

import pytest

import load

FIXTURES = Path(__file__).parent / "fixtures"
R1 = "omnicell_robot_1"


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_delimiter_is_detected_and_columns_map(delimiter):
    rows = [["Item Description", "Qty On Hand"], ["Drug X", "5"]]
    data = "\n".join(delimiter.join(r) for r in rows).encode()
    result = load.load_source(R1, data)
    assert result.delimiter == delimiter
    assert list(result.data.columns) == ["item_name", "stock_qty"]
    assert result.data.iloc[0]["stock_qty"] == "5"


def test_comma_inside_quotes_does_not_fool_semicolon_detection():
    data = b'Item Description;Qty On Hand\n"Drug X, 5mg";5\n'
    result = load.load_source(R1, data)
    assert result.delimiter == ";"
    assert result.data.iloc[0]["item_name"] == "Drug X, 5mg"


def test_single_column_or_empty_header_defaults_to_comma():
    assert load.detect_delimiter("OnlyOne\nvalue") == ","
    assert load.detect_delimiter("") == ","


def test_delimiter_is_reported_in_the_info_message():
    result = load.load_source(R1, b"Item Description;Qty On Hand\nX;1\n")
    assert "delimiter ';'" in result.by_check("encoding")[0].message
    baseline = load.load_source(R1, FIXTURES / "omnicell_robot_1.csv")
    assert "delimiter ','" in baseline.by_check("encoding")[0].message


def test_tab_is_named_in_the_message():
    result = load.load_source(R1, b"Item Description\tQty On Hand\nX\t1\n")
    assert "delimiter 'tab'" in result.by_check("encoding")[0].message


def test_bom_file_still_detects_delimiter():
    result = load.load_source(R1, b"\xef\xbb\xbfItem Description;Qty On Hand\nX;1\n")
    assert result.encoding == "utf-8 (with BOM)" and result.delimiter == ";"
