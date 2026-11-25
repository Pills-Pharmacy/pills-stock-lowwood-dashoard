import csv
import io
from pathlib import Path

import pytest

import config
import load
import make_fixtures
import schema

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_OF = {
    "omnicell_robot_1": "omnicell_robot_1",
    "omnicell_robot_2": "omnicell_robot_2",
    "pmr_usage": "pmr",
    "omnicell_activity": "omnicell_activity",
}


def _read(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    delimiter = load.detect_delimiter(text)
    return list(csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter))


def _source(path: Path) -> str:
    return SOURCE_OF[path.name.split("__")[0].removesuffix(".csv")]


def test_generation_is_deterministic(tmp_path):
    first = make_fixtures.generate(tmp_path / "a")
    second = make_fixtures.generate(tmp_path / "b")
    assert [p.name for p in first] == [p.name for p in second]
    for a, b in zip(first, second):
        assert a.read_bytes() == b.read_bytes(), a.name


def test_committed_fixtures_match_the_script(tmp_path):
    for path in make_fixtures.generate(tmp_path):
        assert (FIXTURES / path.name).read_bytes() == path.read_bytes(), path.name


def test_readme_lists_every_planted_problem_and_fixture():
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "SYNTHETIC" in readme
    on_disk = {p.name for p in FIXTURES.glob("*.csv")}
    assert on_disk == {f"{n}.csv" for n in make_fixtures.baseline()} | {
        v.name for v in make_fixtures.VARIANTS
    }
    for name in on_disk:
        assert f"`{name}`" in readme
    for variant in make_fixtures.VARIANTS:
        assert variant.problem in readme and variant.expected_check in readme


def test_baseline_headers_use_different_styles():
    r1 = list(_read(FIXTURES / "omnicell_robot_1.csv")[0])
    r2 = list(_read(FIXTURES / "omnicell_robot_2.csv")[0])
    pmr = list(_read(FIXTURES / "pmr_usage.csv")[0])
    assert r1 != r2 and r2 != pmr
    assert any(h != h.strip() for h in r1)  # stray spaces
    assert "ITEM_CODE" in r2 and "strength" in r2  # mixed case


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.csv")), ids=lambda p: p.name)
def test_every_fixture_header_maps_or_is_a_patient_field(path):
    source = _source(path)
    headers = list(_read(path)[0])
    mapped, unmapped = config.resolve_headers(source, headers)
    for header in unmapped:
        text = schema.normalise_header(header)
        if path.name.endswith("bestand_format.csv") or "grouped_report" in path.name:
            continue  # real-shape exports carry columns the audit deliberately ignores
        assert any(p in text for p in config.PATIENT_FIELD_PATTERNS), f"{path.name}: {header}"
    missing = set(schema.REQUIRED[source]) - set(mapped.values())
    if path.name == "omnicell_activity__missing_required_column.csv":
        assert missing == {"dispensed"}
    elif path.name.endswith("missing_required_column.csv"):
        assert missing == {"stock_qty"}
    else:
        assert not missing


def test_pmr_has_a_patient_column_only_in_pmr():
    assert "Patient ID" in _read(FIXTURES / "pmr_usage.csv")[0]
    for name in ("omnicell_robot_1.csv", "omnicell_robot_2.csv"):
        assert not any("patient" in h.lower() for h in _read(FIXTURES / name)[0])


def test_encoding_fixtures():
    cp1252 = (FIXTURES / "omnicell_robot_1__cp1252.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        cp1252.decode("utf-8")
    assert b"\xb5g" in cp1252
    assert (FIXTURES / "omnicell_robot_1__utf8_bom.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert not (FIXTURES / "omnicell_robot_1.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def _col(rows, name):
    return [r[name] for r in rows]


def test_planted_problems_are_present():
    qty = " Qty On Hand "
    robot_1 = _read(FIXTURES / "omnicell_robot_1.csv")

    dup = _read(FIXTURES / "omnicell_robot_1__duplicate_rows.csv")
    codes = _col(dup, "Item Code ")
    assert len(codes) == len(robot_1) + 2 and len(set(codes)) == len(robot_1)

    neg = _read(FIXTURES / "omnicell_robot_1__negative_qty.csv")
    assert any(v.startswith("-") for v in _col(neg, qty)) and "" in _col(neg, qty)

    unit = _read(FIXTURES / "omnicell_robot_1__unit_mismatch.csv")
    assert unit[0]["Unit"] == "Box"

    extra = _read(FIXTURES / "omnicell_robot_1__item_not_in_pmr.csv")
    pmr_names = {r["product_name"] for r in _read(FIXTURES / "pmr_usage.csv")}
    assert extra[-1][" Item Description"] not in pmr_names

    both = {r["ITEM_CODE"] for r in _read(FIXTURES / "omnicell_robot_2__item_in_both_robots.csv")}
    assert both & set(_col(robot_1, "Item Code "))

    pmr = _read(FIXTURES / "pmr_usage__item_not_in_robots.csv")
    assert "Warfarin 3mg Tablets" in _col(pmr, "product_name")

    zero = _read(FIXTURES / "pmr_usage__zero_usage_item.csv")
    assert {r["qty_dispensed"] for r in zero if r["product_name"] == "Amox 500mg Caps"} == {"0"}

    blank = _read(FIXTURES / "pmr_usage__missing_branch.csv")
    assert _col(blank, "branch").count("") == 5

    shared = _read(FIXTURES / "pmr_usage__shared_codes.csv")
    assert {r["product_code"] for r in shared} == {i.code for i in make_fixtures.ITEMS}


def test_baseline_pmr_spans_the_period_and_totals_are_exact():
    pmr = _read(FIXTURES / "pmr_usage.csv")
    dates = [make_fixtures._parse_uk(r["date_dispensed"]) for r in pmr]
    assert (max(dates) - min(dates)).days + 1 == make_fixtures.PERIOD_DAYS
    for item in make_fixtures.ITEMS:
        total = sum(int(r["qty_dispensed"]) for r in pmr if r["product_code"] == item.pmr_code)
        assert total == item.dispensed

    short = _read(FIXTURES / "pmr_usage__short_range.csv")
    short_dates = [make_fixtures._parse_uk(r["date_dispensed"]) for r in short]
    assert (max(short_dates) - min(short_dates)).days + 1 <= 30


def test_robot_files_are_disjoint_in_baseline():
    r1 = set(_col(_read(FIXTURES / "omnicell_robot_1.csv"), "Item Code "))
    r2 = set(_col(_read(FIXTURES / "omnicell_robot_2.csv"), "ITEM_CODE"))
    assert not r1 & r2 and len(r1) == 6 and len(r2) == 6
