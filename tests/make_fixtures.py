"""Generate the synthetic fixtures in tests/fixtures/. Deterministic: re-running rewrites
identical bytes. Run from the repo root:  python tests/make_fixtures.py

All data is made up. Drug names are realistic in format only; codes are obviously fake. The
column headers below belong to the synthetic exports (they are the INPUT of the pipeline, so
they are the one place headers appear in Python). The real mapping lives in column_map.yaml.
"""

from __future__ import annotations

import csv
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SEED = 20261019
FIXTURE_DIR = Path(__file__).parent / "fixtures"
PERIOD_END = date(2026, 10, 19)
PERIOD_DAYS = 90
EVENTS_PER_ITEM = 12
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

R1_HEADER = [
    "Item Code ", " Item Description", "Strength", "Pack Size", " Qty On Hand ", "Unit",
    "Expiry Date",
]  # fmt: skip
R2_HEADER = ["ITEM_CODE", "Drug Name", "strength", "Pack", "Stock Level", "UoM"]
PMR_HEADER = [
    "product_code", "product_name", "pack_size", "unit", "qty_dispensed", "date_dispensed",
    "branch", "Patient ID",
]  # fmt: skip

BRANCH = {"robot_1": "Branch A", "robot_2": "Branch B"}


@dataclass(frozen=True)
class Item:
    n: int
    robot: str
    robot_name: str
    robot_strength: str
    robot_pack: int
    unit: str
    pmr_name: str
    pmr_pack: int
    stock: int  # on hand in the robot, in units
    dispensed: int  # total dispensed in the 90-day period, in units

    @property
    def code(self) -> str:
        return f"FAKE-{self.n:04d}"

    @property
    def pmr_code(self) -> str:
        return f"PMR-{self.n * 7 + 100:05d}"


# stock / (dispensed / 90) gives days of cover; the comment shows it for hand-checking.
ITEMS = [
    Item(1, "robot_1", "Paracetamol 500mg Tablets", "500mg", 100, "Tablet",
         "Paracetamol 0.5g Tabs", 32, 1200, 900),  # 120 days
    Item(2, "robot_1", "Amoxicillin 500mg Capsules", "500mg", 21, "Capsule",
         "Amox 500mg Caps", 21, 40, 630),  # 5.7 days
    Item(3, "robot_1", "Metformin 500mg Tablets", "500mg", 84, "Tablet",
         "Metformin Hydrochloride 500 mg Tablets", 56, 2000, 840),  # 214 days
    Item(4, "robot_1", "Atorvastatin 20mg Tablets", "20mg", 28, "Tablet",
         "Atorvastatin 20mg Tabs", 28, 280, 560),  # 45 days
    Item(5, "robot_1", "Omeprazole 20mg Capsules", "20mg", 28, "Capsule",
         "Omeprazole 20mg Gastro-resistant Caps", 28, 300, 280),  # 96 days
    Item(6, "robot_1", "Levothyroxine 100µg Tablets", "100µg", 28, "Tablet",
         "Levothyroxine 0.1mg Tablets", 28, 56, 1260),  # 4 days
    Item(7, "robot_2", "IBUPROFEN 400MG TABS", "0.4g", 84, "Tablet",
         "Ibuprofen 400mg Tablets", 24, 840, 1260),  # 60 days
    Item(8, "robot_2", "Amlodipine 5mg Tabs", "5mg", 28, "Tablet",
         "Amlodipine 5 mg Tablets", 28, 3000, 280),  # 964 days
    Item(9, "robot_2", "Furosemide 40mg Tabs", "40mg", 28, "Tablet",
         "Furosemide 40mg Tabs", 28, 168, 504),  # 30 days
    Item(10, "robot_2", "Morphine Sulf 10mg/5ml Oral Soln", "10mg/5ml", 100, "Bottle",
         "Morphine Sulfate 10mg/5ml Oral Solution 100ml", 1, 6, 18),  # 30 days
    Item(11, "robot_2", "Ceftriaxone 1g Inj", "1g", 1, "Vial",
         "Ceftriaxone Sodium 1g Powder for Injection", 1, 20, 450),  # 4 days
    Item(12, "robot_2", "Prednisolone 5mg Tabs", "5mg", 28, "Tablet",
         "Prednisolone 5mg Tablets", 28, 560, 2800),  # 18 days
]  # fmt: skip

# Used only by variants: stocked in a robot but never dispensed, and dispensed but never stocked.
DEAD_ITEM = Item(14, "robot_1", "Zzz Placeholder Drug 10mg Tablets", "10mg", 28, "Tablet",
                 "", 0, 500, 0)  # fmt: skip
PMR_ONLY_ITEM = Item(15, "robot_1", "", "", 0, "Tablet",
                     "Warfarin 3mg Tablets", 28, 0, 280)  # fmt: skip


@dataclass
class Table:
    header: list[str]
    rows: list[dict[str, str]]
    delimiter: str = ","

    def copy(self) -> Table:
        return Table(list(self.header), [dict(r) for r in self.rows], self.delimiter)

    def row_for(self, **match: str) -> dict[str, str]:
        for row in self.rows:
            if all(row[key] == value for key, value in match.items()):
                return row
        raise KeyError(match)


def _fmt_iso(d: date) -> str:
    return d.isoformat()


def _fmt_uk(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def _fmt_dmy_text(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def _robot_1_row(item: Item, index: int, expiry: date) -> dict[str, str]:
    name = item.robot_name
    name = (f" {name}", f"{name} ", name)[index % 3]  # stray spaces
    return {
        "Item Code ": item.code,
        " Item Description": name,
        "Strength": item.robot_strength,
        "Pack Size": str(item.robot_pack),
        " Qty On Hand ": str(item.stock),
        "Unit": item.unit,
        "Expiry Date": _fmt_iso(expiry),
    }


def _robot_2_row(item: Item) -> dict[str, str]:
    return {
        "ITEM_CODE": item.code,
        "Drug Name": item.robot_name,
        "strength": item.robot_strength,
        "Pack": str(item.robot_pack),
        "Stock Level": str(item.stock),
        "UoM": item.unit,
    }


def _pmr_rows(rng: random.Random, items: list[Item]) -> list[dict[str, str]]:
    start = PERIOD_END - timedelta(days=PERIOD_DAYS - 1)
    days = [start + timedelta(days=i) for i in range(PERIOD_DAYS)]
    raw = []
    for item in items:
        n = min(EVENTS_PER_ITEM, item.dispensed)
        dates = sorted([days[0], days[-1], *rng.sample(days[1:-1], n - 2)])
        cuts = sorted(rng.sample(range(1, item.dispensed), n - 1))
        quantities = [b - a for a, b in zip([0, *cuts], [*cuts, item.dispensed])]
        for when, qty in zip(dates, quantities):
            patient = f"PT-FAKE-{rng.randint(1, 99999):05d}"
            raw.append((when, item.n, item, qty, patient))
    raw.sort(key=lambda r: (r[0], r[1]))
    return [
        {
            "product_code": item.pmr_code,
            "product_name": item.pmr_name,
            "pack_size": str(item.pmr_pack),
            "unit": item.unit,
            "qty_dispensed": str(qty),
            "date_dispensed": _fmt_uk(when),
            "branch": BRANCH[item.robot],
            "Patient ID": patient,
        }
        for when, _, item, qty, patient in raw
    ]


# Robot activity statistics in the shape of the real export: ';' delimited, dd.mm.yy dates,
# newest day first, one row per day for one system. Weekends are quiet, as in the real files.
ACTIVITY_HEADER = ["DATE", "PERIOD(H)", "SYSTEMNAME", "DISPENSED", "STOCKED", "REARRANGED"]
ACTIVITY_DAYS = 31
ACTIVITY_END = PERIOD_END  # the same last day as the PMR fixtures


def _activity_row(day: date, dispensed: int, stocked: int, rearranged: int, system="Medimat1"):
    return {
        "DATE": f"{day.day:02d}.{day.month:02d}.{day.year % 100:02d}",
        "PERIOD(H)": "24",
        "SYSTEMNAME": system,
        "DISPENSED": str(dispensed),
        "STOCKED": str(stocked),
        "REARRANGED": str(rearranged),
    }


def _activity_rows() -> list[dict[str, str]]:
    rng = random.Random(SEED + 2)
    rows = []
    for back in range(ACTIVITY_DAYS):
        day = ACTIVITY_END - timedelta(days=back)
        quiet = day.weekday() >= 5  # Saturday and Sunday
        dispensed = 0 if quiet else rng.randint(150, 300)
        stocked = 0 if quiet else rng.randint(100, 280)
        rearranged = 0 if quiet or back == 0 or rng.random() < 0.7 else rng.randint(100, 800)
        rows.append(_activity_row(day, dispensed, stocked, rearranged))
    return rows


def baseline() -> dict[str, Table]:
    """The clean files. Every variant is derived from these."""
    rng = random.Random(SEED)
    robot_1_items = [i for i in ITEMS if i.robot == "robot_1"]
    robot_2_items = [i for i in ITEMS if i.robot == "robot_2"]
    expiries = [PERIOD_END + timedelta(days=rng.randint(90, 720)) for _ in robot_1_items]
    return {
        "omnicell_robot_1": Table(
            list(R1_HEADER),
            [_robot_1_row(i, k, expiries[k]) for k, i in enumerate(robot_1_items)],
        ),
        "omnicell_robot_2": Table(list(R2_HEADER), [_robot_2_row(i) for i in robot_2_items]),
        "pmr_usage": Table(list(PMR_HEADER), _pmr_rows(rng, ITEMS)),
        "omnicell_activity": Table(list(ACTIVITY_HEADER), _activity_rows(), ";"),
    }


# Each variant: (output name, problem, expected check, builder(baseline) -> Table, encoding).
@dataclass(frozen=True)
class Variant:
    name: str
    problem: str
    expected_check: str
    build: object  # callable(dict[str, Table]) -> Table (or -> str when raw)
    encoding: str = "utf-8"
    delimiter: str = ","
    raw: bool = False  # the builder returns the file text itself, not a Table


def _missing_required_column(base):
    table = base["omnicell_robot_1"].copy()
    table.header.remove(" Qty On Hand ")
    for row in table.rows:
        del row[" Qty On Hand "]
    return table


def _duplicate_rows(base):
    table = base["omnicell_robot_1"].copy()
    table.rows.append(dict(table.rows[0]))  # exact duplicate
    conflicting = dict(table.rows[2])
    conflicting[" Qty On Hand "] = "999"  # same key, different quantity
    table.rows.append(conflicting)
    return table


def _negative_qty(base):
    table = base["omnicell_robot_1"].copy()
    table.rows[1][" Qty On Hand "] = "-12"
    table.rows[4][" Qty On Hand "] = ""
    return table


def _unit_mismatch(base):
    table = base["omnicell_robot_1"].copy()
    table.rows[0]["Unit"] = "Box"  # PMR counts this item in tablets
    table.rows[0]["Pack Size"] = "1"
    return table


def _robot_item_not_in_pmr(base):
    table = base["omnicell_robot_1"].copy()
    row = _robot_1_row(DEAD_ITEM, 0, PERIOD_END + timedelta(days=400))
    table.rows.append(row)
    return table


def _encoded_robot_1(base):
    return base["omnicell_robot_1"].copy()  # contains a micro sign in the item name


def _item_in_both_robots(base):
    table = base["omnicell_robot_2"].copy()
    levo = next(i for i in ITEMS if i.n == 6)
    row = _robot_2_row(levo)
    row["Stock Level"] = "30"
    table.rows.append(row)
    return table


def _robot_2_in_robot_1_style(base):
    robot_2 = base["omnicell_robot_2"]
    rows = []
    for k, source in enumerate(robot_2.rows):
        item = next(i for i in ITEMS if i.code == source["ITEM_CODE"])
        rows.append(_robot_1_row(item, k, PERIOD_END + timedelta(days=200 + k)))
    return Table(list(R1_HEADER), rows)


def _pmr_item_not_in_robots(base):
    table = base["pmr_usage"].copy()
    rng = random.Random(SEED + 1)
    table.rows.extend(_pmr_rows(rng, [PMR_ONLY_ITEM]))
    return table


def _pmr_zero_usage(base):
    table = base["pmr_usage"].copy()
    for row in table.rows:
        if row["product_code"] == ITEMS[1].pmr_code:  # Amoxicillin: stocked, zero dispensed
            row["qty_dispensed"] = "0"
    return table


def _pmr_short_range(base):
    table = base["pmr_usage"].copy()
    cutoff = PERIOD_END - timedelta(days=29)  # last 30 days only
    table.rows = [r for r in table.rows if _parse_uk(r["date_dispensed"]) >= cutoff]
    return table


def _pmr_missing_branch(base):
    table = base["pmr_usage"].copy()
    for row in table.rows[:3] + table.rows[-2:]:
        row["branch"] = ""
    return table


def _pmr_negative_qty(base):
    table = base["pmr_usage"].copy()
    table.rows[3]["qty_dispensed"] = "-5"
    return table


def _pmr_shared_codes(base):
    table = base["pmr_usage"].copy()
    by_pmr = {i.pmr_code: i.code for i in ITEMS}
    for row in table.rows:
        row["product_code"] = by_pmr[row["product_code"]]
    return table


# Header names of the real OmniCell stock listing (BestandArtikel), in file order. Names only:
# every value in the fixture below is made up.
BESTAND_HEADER = [
    "SERIALNUMBERSINCEEXPIRYDATE", "NAME", "FORM", "PACKSIZE", "ARTIKELCODE", "ISTANZAHL",
    "FREIANZAHL", "FLAGS", "GENERICKEY", "MAXSCHACHT", "MINMENGE", "MAXMENGE", "MINLAGERTEMP",
    "MAXLAGERTEMP", "BARCODE", "CD", "AHT", "UNFIT", "DELETEFLAG", "SERIALNUMBERSINCEEXPIRYDATE1",
    "GNAME", "GENERICID", "ARTIKELCODE1", "GENERICID1", "NAME1", "DFORM", "PEINHEIT", "PACKSIZE1",
    "HERSTELLER", "STRENGTH", "FLAVOUR", "BARCODE1", "CD1", "ISTANZAHL1", "FREIANZAHL1",
    "MINMENGE1", "MAXMENGE1", "GNAME1", "MINLAGERTEMP1", "MAXLAGERTEMP1", "FLAGS1", "MAXSCHACHT1",
    "MINSCHACHT", "MINPACKMD", "AHT1", "SAISON", "UNFIT1", "BREITE", "HOEHE", "TIEFE", "GEWICHT",
    "DELETEFLAG1", "STOCKLOCATIONID", "MACHINELOCATION", "SERIALNUMBERSINCEEXPIRYDATE2",
    "LASTUPDATE", "SBTYPE", "SBDZ", "MAXSCHACHTBER", "LAST_MS_CHANGE", "R$ID",
]  # fmt: skip
BESTAND_FORMS = {"Tablet": "Tab", "Capsule": "Cap", "Bottle": "Oth", "Vial": "Inj"}
LONG_NAME = "BUDESONIDE 500MCG/2ML NEBULISER LIQUID UNIT DOSE VIALS"  # over 50 chars: NAME1 cuts it


def _bestand_row(name: str, form: str, pack: int, code: str, count: int, row_id: int) -> dict:
    row = dict.fromkeys(BESTAND_HEADER, "")
    row.update(
        NAME=name, FORM=form, PACKSIZE=str(pack), ARTIKELCODE=code, ISTANZAHL=str(count),
        FREIANZAHL=str(count), MINMENGE="-1", MAXMENGE="-1", BARCODE=code, CD="0", UNFIT="M",
        GNAME=name, DFORM=form, SERIALNUMBERSINCEEXPIRYDATE="01.01.80",
        LASTUPDATE="23.02.26 09:25:46", **{"R$ID": str(row_id)},
    )  # fmt: skip
    for header in BESTAND_HEADER:  # every column ending in 1 or 2 is a copy of its base column
        base = header.rstrip("12")
        if base != header and base in row:
            row[header] = row[base][:50] if header == "NAME1" else row[base]
    return row


def _bestand_format(base):
    """The six robot-1 items in the shape of the real stock listing, plus awkward rows."""
    items = [i for i in ITEMS if i.robot == "robot_1"]
    rows = [
        _bestand_row(
            i.pmr_name.upper(),
            BESTAND_FORMS[i.unit],
            i.robot_pack,
            f"{i.n:014d}",
            max(1, i.stock // i.robot_pack),
            n,
        )
        for n, i in enumerate(items, start=1)
    ]
    first = items[0]  # the same drug under a second barcode: a second row, not a duplicate
    rows.append(
        _bestand_row(first.pmr_name.upper(), "Tab", first.robot_pack, "00000000000101", 3, 7)
    )
    rows.append(_bestand_row(LONG_NAME, "Oth", 20, "5060000000001", 4, 8))
    return Table(list(BESTAND_HEADER), rows)


def _activity_pull_2(base):
    """A later, shorter pull: two newer days, 21 days, and earlier days revised."""
    table = base["omnicell_activity"].copy()
    newer = [
        _activity_row(date(2026, 10, 21), 210, 150, 0),
        _activity_row(date(2026, 10, 20), 180, 120, 40),
    ]
    kept = [dict(r) for r in table.rows[:21]]
    for row in kept:
        if row["DATE"] == "19.10.26":  # revised: dispensed +1, rearranged 0 -> 829
            row["DISPENSED"] = str(int(row["DISPENSED"]) + 1)
            row["REARRANGED"] = "829"
        if row["DATE"] == "16.10.26":  # revised: stocked +5
            row["STOCKED"] = str(int(row["STOCKED"]) + 5)
    table.rows = newer + kept
    return table


def _activity_shuffled(base):
    table = base["omnicell_activity"].copy()
    table.header = ["PERIOD(H)", "SYSTEMNAME", "DISPENSED", "STOCKED", "REARRANGED", "DATE"]
    return table


def _activity_missing_day(base):
    table = base["omnicell_activity"].copy()
    del table.rows[3]  # 16.10.26, a Friday: a gap in the middle
    return table


def _activity_duplicate_days(base):
    table = base["omnicell_activity"].copy()
    table.rows.append(dict(table.rows[0]))  # exact repeat of 19.10.26
    changed = dict(table.rows[3])
    changed["DISPENSED"] = str(int(changed["DISPENSED"]) + 7)  # same day, different count
    table.rows.append(changed)
    return table


def _activity_period_not_24(base):
    table = base["omnicell_activity"].copy()
    table.rows[0]["PERIOD(H)"] = "12"
    return table


def _activity_negative_count(base):
    table = base["omnicell_activity"].copy()
    table.rows[3]["DISPENSED"] = "-4"
    table.rows[4]["STOCKED"] = ""
    return table


def _activity_unknown_system(base):
    table = base["omnicell_activity"].copy()
    table.rows[0]["SYSTEMNAME"] = "Medimat2"
    table.rows[3]["SYSTEMNAME"] = "Medimat2"
    return table


def _activity_missing_required(base):
    table = base["omnicell_activity"].copy()
    table.header.remove("DISPENSED")
    for row in table.rows:
        del row["DISPENSED"]
    return table


# The Lowood PMR "Script Report Generator" layout: each script is a line (patient, dispensed-as,
# written-as, directions), optional lines carrying wrapped directions, then a line that starts
# with a blank field and holds the month and quantity. All values here are made up.
GROUPED_HEADER = (
    "Patient No.,Script Dispensed As,Script Written As,Directions,Disp Month (Disp),Disp Qty"
)
GROUPED_MONTHS = [(2026, m) for m in range(4, 11)]  # Apr to Oct 2026; October is the partial one
GROUPED_LATEST_QTY = 7  # per script line, so the latest month is easy to spot


def _grouped_month_quantities(item: Item) -> dict[tuple[int, int], int]:
    """Quantity per month. Jul, Aug and Sep together equal item.dispensed (the 3-month window)."""
    share, extra = divmod(item.dispensed, 3)
    window = [share + (1 if extra > 0 else 0), share + (1 if extra > 1 else 0), share]
    earlier = max(2, item.dispensed // 6)
    quantities = {(2026, m): earlier for m in (4, 5, 6)}
    quantities.update({(2026, m): q for m, q in zip((7, 8, 9), window)})
    quantities[(2026, 10)] = GROUPED_LATEST_QTY * 2
    return quantities


def _grouped_report(base):
    lines = [GROUPED_HEADER]
    patient = 0
    for item in ITEMS:
        for (year, month), quantity in _grouped_month_quantities(item).items():
            first = quantity // 2  # two scripts a month, so months have to be summed
            for part, qty in enumerate((first, quantity - first)):
                patient += 1
                written = "" if patient % 3 == 0 else item.pmr_name.upper()
                line = f"PT-FAKE-{patient:05d},{item.pmr_name.upper()},{written},"
                if patient % 4 == 0:  # directions wrapped onto a second physical line
                    lines += [line + "TAKE ONE", "TWICE DAILY"]
                else:
                    lines.append(line + "AS DIRECTED")
                lines.append(f",{year} {month:02d},{qty}")
    return "\r\n".join(lines) + "\r\n"


def _grouped_report_orphan_line(base):
    lines = _grouped_report(base).split("\r\n")
    lines.insert(11, "PT-FAKE-99999,ORPHAN DRUG 5MG TABLETS,,ONE DAILY")  # never closed
    return "\r\n".join(lines)


def _parse_uk(text: str) -> date:
    day, month, year = text.split("/")
    return date(int(year), int(month), int(day))


VARIANTS = [
    Variant("omnicell_robot_1__missing_required_column.csv",
            "Required column missing (stock quantity column removed)",
            "required_columns: names `stock_qty` and stops", _missing_required_column),
    Variant("omnicell_robot_1__duplicate_rows.csv",
            "Exact duplicate row, and a second row with the same key but a different quantity",
            "duplicates: keyed on (robot_id, item_code or name)", _duplicate_rows),
    Variant("omnicell_robot_1__negative_qty.csv",
            "One negative and one blank stock quantity",
            "negative_or_missing_qty: 2 affected rows", _negative_qty),
    Variant("omnicell_robot_1__unit_mismatch.csv",
            "Paracetamol held as Unit=Box, Pack Size=1; PMR counts it in tablets",
            "unit_mismatch: robot vs PMR unit/pack size", _unit_mismatch),
    Variant("omnicell_robot_1__item_not_in_pmr.csv",
            "Item in a robot export but not in PMR (also zero usage by absence)",
            "match: level 4 review list; analyse: dead stock / stock not in PMR",
            _robot_item_not_in_pmr),
    Variant("omnicell_robot_1__cp1252.csv",
            "Saved as Windows-1252 (micro sign is byte 0xB5, invalid UTF-8)",
            "encoding: UTF-8 fails, falls back to Windows-1252 and reports it",
            _encoded_robot_1, "cp1252"),
    Variant("omnicell_robot_1__utf8_bom.csv",
            "UTF-8 with a byte-order mark (first header would otherwise read as \\ufeffItem Code)",
            "encoding: BOM stripped, header still maps to item_code", _encoded_robot_1,
            "utf-8-sig"),
    Variant("omnicell_robot_2__item_in_both_robots.csv",
            "Levothyroxine stocked in robot 1 and robot 2",
            "match/analyse: summed under combined_per_branch, separate under per_robot",
            _item_in_both_robots),
    Variant("omnicell_robot_2__same_headers_as_robot_1.csv",
            "Robot 2 content in robot 1 header style (the passing case)",
            "same_headers: no warning; the baseline pair differs and raises it",
            _robot_2_in_robot_1_style),
    Variant("pmr_usage__item_not_in_robots.csv",
            "Warfarin dispensed in PMR but stocked in neither robot",
            "match: level 4 review list (PMR side)", _pmr_item_not_in_robots),
    Variant("pmr_usage__zero_usage_item.csv",
            "Amoxicillin rows all have quantity 0 (stocked, zero usage)",
            "analyse: dead stock, no division by zero", _pmr_zero_usage),
    Variant("pmr_usage__short_range.csv",
            "PMR covers only the last 30 days, period is 90",
            "pmr_date_range: warns that the range is shorter than period_days", _pmr_short_range),
    Variant("pmr_usage__missing_branch.csv",
            "Five rows have a blank branch",
            "branch_present: 5 affected rows", _pmr_missing_branch),
    Variant("pmr_usage__negative_qty.csv",
            "One negative dispensed quantity (a return)",
            "negative_or_missing_qty: 1 affected row", _pmr_negative_qty),
    Variant("omnicell_robot_1__bestand_format.csv",
            "Real OmniCell stock listing shape: ';' delimiter, 61 columns, copy columns, blank "
            "STRENGTH, placeholder expiry dates, one drug under two barcodes, a name over 50 characters",
            "load: delimiter ';', type omnicell_stock; ARTIKELCODE/NAME/PACKSIZE/ISTANZAHL map, "
            "copies and expiry placeholder ignored; no duplicates flagged",
            _bestand_format, delimiter=";"),
    Variant("omnicell_activity__pull_2.csv",
            "A later pull: 21 days up to 21.10.26, with 19.10.26 and 16.10.26 revised",
            "merge: latest pull wins; 3 changed counts on 2 days reported",
            _activity_pull_2, delimiter=";"),
    Variant("omnicell_activity__shuffled_columns.csv",
            "Columns in a different order (as in the real files)",
            "load: columns map by name, same result as the baseline",
            _activity_shuffled, delimiter=";"),
    Variant("omnicell_activity__missing_day.csv",
            "A weekday (16.10.26) is missing from the middle of the range",
            "missing_days: 1 day missing", _activity_missing_day, delimiter=";"),
    Variant("omnicell_activity__duplicate_days.csv",
            "19.10.26 repeated, and 16.10.26 repeated with a different count",
            "duplicates: 2 days, 1 with different counts", _activity_duplicate_days,
            delimiter=";"),
    Variant("omnicell_activity__period_not_24.csv",
            "One row covers a 12-hour period",
            "period_hours: 1 affected row", _activity_period_not_24, delimiter=";"),
    Variant("omnicell_activity__negative_count.csv",
            "One negative dispensed count and one blank stocked count",
            "negative_or_missing_qty: 2 issues", _activity_negative_count, delimiter=";"),
    Variant("omnicell_activity__unknown_system.csv",
            "Two rows name a system (Medimat2) that is not in the robot registry",
            "system_registry: branch unknown", _activity_unknown_system, delimiter=";"),
    Variant("omnicell_activity__missing_required_column.csv",
            "Required column missing (DISPENSED removed)",
            "required_columns: names `dispensed` and stops", _activity_missing_required,
            delimiter=";"),
    Variant("pmr_usage__grouped_report.csv",
            "Lowood PMR report layout: each script is a line plus a month/quantity line, "
            "directions wrapped onto extra lines, whole months only, Apr to Oct 2026",
            "load: flattened (layout grouped), Patient No. dropped, month dates; analyse: "
            "Jul-Sep window of 92 days, October left out as possibly incomplete",
            _grouped_report, raw=True),
    Variant("pmr_usage__grouped_report_orphan_line.csv",
            "The grouped layout with one script line that never gets its month/quantity line",
            "load: warning that 1 line did not fit the grouped layout",
            _grouped_report_orphan_line, raw=True),
    Variant("pmr_usage__shared_codes.csv",
            "PMR product codes equal the robot item codes",
            "match: level 1 exact code works when switched on", _pmr_shared_codes),
]  # fmt: skip

BASELINE_NOTES = [
    (
        "omnicell_robot_1.csv",
        "Baseline. Title Case headers with stray spaces, items 1-6, ISO "
        "expiry dates, UTF-8 (includes a micro sign).",
    ),
    (
        "omnicell_robot_2.csv",
        "Baseline. Mixed case / UPPER_SNAKE headers, items 7-12, no expiry "
        "column, abbreviated names (Tabs, Inj, Soln).",
    ),
    (
        "omnicell_activity.csv",
        "Baseline. Robot activity statistics in the real shape: ';' "
        "delimiter, dd.mm.yy dates, newest first, 31 days to 19.10.26, one system (Medimat1), "
        "quiet weekends.",
    ),
    (
        "pmr_usage.csv",
        "Baseline. snake_case headers, items 1-12, 90 days to 19/10/2026, "
        "dd/mm/yyyy dates, names in other forms (0.5g vs 500mg, Amox, pack sizes differ) and a fake "
        "`Patient ID` column that must be dropped.",
    ),
]


def _write_csv(path: Path, table: Table, encoding: str, delimiter: str = ",") -> None:
    with open(path, "w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=table.header, delimiter=delimiter, lineterminator="\r\n"
        )
        writer.writeheader()
        writer.writerows(table.rows)


def _readme() -> str:
    lines = [
        "# Synthetic fixtures",
        "",
        "These files are SYNTHETIC. Every value is made up; codes are `FAKE-nnnn` / `PMR-nnnnn` "
        "and patient IDs are `PT-FAKE-nnnnn`. Drug names are realistic in format only.",
        "",
        "Generated by `tests/make_fixtures.py` (fixed seed). Do not edit by hand: re-run "
        "`python tests/make_fixtures.py`. A test fails if these files drift from the script.",
        "",
        "Headers are placeholders chosen for the fixtures; they are not real OmniCell or PMR "
        "headers. The baseline robot files deliberately use different header styles, so the "
        "`same_headers` check is expected to raise on that pair.",
        "",
        "Days of cover for the baseline items (stock / (dispensed / 90)) are in the comments "
        "beside `ITEMS` in the script, for hand-checking later milestones.",
        "",
        "## Baseline (clean) files",
        "",
        "| File | Notes |",
        "|------|-------|",
        *[f"| `{name}` | {note} |" for name, note in BASELINE_NOTES],
        "",
        "## Planted problems",
        "",
        "| File | Problem | Expected check that catches it |",
        "|------|---------|--------------------------------|",
        *[f"| `{v.name}` | {v.problem} | {v.expected_check} |" for v in VARIANTS],
        "",
    ]
    return "\n".join(lines)


def generate(out_dir: Path = FIXTURE_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = baseline()
    written = []
    for name, table in base.items():
        path = out_dir / f"{name}.csv"
        _write_csv(path, table, "utf-8", table.delimiter)
        written.append(path)
    for variant in VARIANTS:
        path = out_dir / variant.name
        built = variant.build(base)
        if variant.raw:
            with open(path, "w", encoding=variant.encoding, newline="") as handle:
                handle.write(built)
        else:
            _write_csv(path, built, variant.encoding, variant.delimiter)
        written.append(path)
    readme = out_dir / "README.md"
    readme.write_bytes(_readme().encode("utf-8"))
    written.append(readme)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE_DIR
    for written_path in generate(target):
        print(written_path)
