from pathlib import Path

import pytest

import config
import load

FIXTURES = Path(__file__).parent / "fixtures"
R1, R2, PMR = "omnicell_robot_1", "omnicell_robot_2", "pmr"

# Header names only, as seen in the real exports; every value below is made up.
STOCK = b"NAME;FORM;PACKSIZE;ARTIKELCODE;ISTANZAHL;FREIANZAHL;STRENGTH\nDrug X;TAB;28;A1;5;3;5mg\n"
ACTIVITY = b"DATE;PERIOD(H);SYSTEMNAME;DISPENSED;STOCKED;REARRANGED\n2026-01-01;24;SYS;1;2;3\n"


def test_detects_each_real_header_shape():
    assert load.detect_file_type(["NAME", "ARTIKELCODE", "ISTANZAHL", "FREIANZAHL"]) == (
        "omnicell_stock"
    )
    headers = ["DATE", "PERIOD(H)", "SYSTEMNAME", "DISPENSED", "STOCKED", "REARRANGED"]
    assert load.detect_file_type(headers) == "omnicell_activity"


def test_detection_ignores_order_case_and_spacing():
    headers = [" rearranged", "Stocked ", "DISPENSED", "systemname", "date"]
    assert load.detect_file_type(headers) == "omnicell_activity"


def test_partial_signature_is_not_detected():
    assert load.detect_file_type(["ARTIKELCODE", "ISTANZAHL"]) is None
    assert load.detect_file_type([]) is None


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.csv")), ids=lambda p: p.name)
def test_old_style_fixtures_are_not_mistaken_for_a_known_type(path):
    if path.name.startswith("omnicell_activity"):
        pytest.skip("covered by test_activity_fixtures_are_recognised_as_activity")
    result = load.load_source(R1, path)
    assert not result.by_check("wrong_file_type")
    if path.name.endswith("bestand_format.csv"):  # the real-shape fixture IS a stock listing
        assert result.detected_type == "omnicell_stock"
    else:
        assert result.detected_type is None


def test_activity_file_in_a_robot_slot_is_rejected():
    for slot in (R1, R2):
        result = load.load_source(slot, ACTIVITY)
        assert result.stopped and result.detected_type == "omnicell_activity"
        [issue] = result.by_check("wrong_file_type")
        assert issue.severity == load.ERROR
        assert "activity statistics" in issue.message and "stock listing" in issue.message


def test_stock_file_in_the_pmr_slot_is_rejected():
    result = load.load_source(PMR, STOCK)
    [issue] = result.by_check("wrong_file_type")
    assert result.stopped and "PMR usage" in issue.message


def test_stock_file_in_a_robot_slot_passes_the_type_check():
    result = load.load_source(R1, STOCK)
    assert result.detected_type == "omnicell_stock"
    assert not result.by_check("wrong_file_type")


def test_patient_columns_are_dropped_before_the_type_check():
    data = b"Patient Name;" + ACTIVITY
    result = load.load_source(R1, data)
    assert result.dropped_patient_columns == ["Patient Name"]
    assert "Patient Name" not in result.raw_headers and result.by_check("wrong_file_type")


def test_no_real_header_is_mistaken_for_a_patient_field():
    stock = [h.strip() for h in STOCK.split(b"\n")[0].decode().split(";")]
    activity = [h.strip() for h in ACTIVITY.split(b"\n")[0].decode().split(";")]
    extra = ["SERIALNUMBERSINCEEXPIRYDATE", "GENERICKEY", "MINMENGE", "MAXMENGE", "HERSTELLER",
             "MINPACKMD", "BREITE", "HOEHE", "TIEFE", "GEWICHT", "STOCKLOCATIONID",
             "MACHINELOCATION", "LASTUPDATE", "SBTYPE", "SBDZ", "LAST_MS_CHANGE", "R$ID",
             "BARCODE", "FLAGS", "GNAME", "GENERICID", "DFORM", "PEINHEIT", "FLAVOUR", "CD",
             "AHT", "UNFIT", "DELETEFLAG", "SAISON", "MINSCHACHT", "MAXSCHACHT"]  # fmt: skip
    assert load.detect_patient_columns(stock + activity + extra) == []


def test_every_slot_has_an_expected_type_with_a_label():
    for expected in config.EXPECTED_FILE_TYPE.values():
        assert expected in config.FILE_TYPE_LABELS


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("omnicell_activity*.csv")), ids=lambda p: p.name
)
def test_activity_fixtures_are_recognised_as_activity(path):
    result = load.load_source("omnicell_activity", path)
    assert not result.by_check("wrong_file_type")
    if path.name.endswith("missing_required_column.csv"):
        # DISPENSED is part of the signature, so a file without it cannot be recognised: it
        # simply stops on the missing column instead.
        assert result.detected_type is None and result.by_check("required_columns")
        return
    assert result.detected_type == "omnicell_activity"
    in_robot_slot = load.load_source(R1, path)
    assert in_robot_slot.stopped and in_robot_slot.by_check("wrong_file_type")
