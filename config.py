"""Settings, registries and the column-map loader.

Thresholds and the classification level are taken from here (or from app settings built on
`Settings`), never hard-coded elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from schema import ALL_COLUMNS, OPTIONAL, REQUIRED, ROBOT_SOURCES, SOURCES, normalise_header

COLUMN_MAP_PATH = Path(__file__).parent / "column_map.yaml"

ASSUMPTION = "Assumption, to be confirmed"

PER_ROBOT = "per_robot"
COMBINED_PER_BRANCH = "combined_per_branch"
CLASSIFICATION_LEVELS = (PER_ROBOT, COMBINED_PER_BRANCH)


@dataclass(frozen=True)
class Setting:
    value: int | str
    label: str = ASSUMPTION


@dataclass(frozen=True)
class Settings:
    period_days: Setting = Setting(90)
    overstock_days: Setting = Setting(180)
    understock_days: Setting = Setting(14)
    classification_level: Setting = Setting(COMBINED_PER_BRANCH)

    def values(self) -> dict[str, int | str]:
        return {name: getattr(self, name).value for name in self.__dataclass_fields__}

    def labels(self) -> dict[str, str]:
        return {name: getattr(self, name).label for name in self.__dataclass_fields__}

    def with_value(self, name: str, value: int | str) -> Settings:
        """Return a copy with one value changed. The label is kept until someone confirms it."""
        if name not in self.__dataclass_fields__:
            raise KeyError(f"Unknown setting: {name}")
        return replace(self, **{name: replace(getattr(self, name), value=value)})

    def problems(self) -> list[str]:
        """Human-readable problems with the current values (empty list means usable)."""
        found = []
        for name in ("period_days", "overstock_days", "understock_days"):
            value = getattr(self, name).value
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                found.append(f"{name} must be a positive whole number of days")
        if not found and self.understock_days.value >= self.overstock_days.value:
            found.append("understock_days must be lower than overstock_days")
        if self.classification_level.value not in CLASSIFICATION_LEVELS:
            found.append(f"classification_level must be one of {', '.join(CLASSIFICATION_LEVELS)}")
        return found


DEFAULT_SETTINGS = Settings()


@dataclass(frozen=True)
class RobotInfo:
    branch: str
    confirmed: bool = False


# PLACEHOLDER: which branch each robot sits in is an open question (OPEN_QUESTIONS.md, Q1).
ROBOT_REGISTRY: dict[str, RobotInfo] = {
    "robot_1": RobotInfo(branch="Branch A"),
    "robot_2": RobotInfo(branch="Branch B"),
}


@dataclass(frozen=True)
class Pharmacy:
    """One pharmacy tab in the app. Its label is the branch written on every row it analyses."""

    key: str
    label: str
    robot_sources: tuple[str, ...]  # the stock-upload slots (schema.ROBOT_SOURCES keys) it offers


# One tab each. Lowood's robot is robot_1 (confirmed: Medimat1). McGregor's robot is a
# PLACEHOLDER (robot_2) and no McGregor export has been seen yet, so its headers are unknown:
# add them under `pharmacies: mcgregor:` in column_map.yaml.
PHARMACIES: tuple[Pharmacy, ...] = (
    Pharmacy("lowood", "Lowood", ("omnicell_robot_1",)),
    Pharmacy("mcgregor", "McGregor", ("omnicell_robot_2",)),
)


def pharmacy_by_key(key: str) -> Pharmacy:
    for pharmacy in PHARMACIES:
        if pharmacy.key == key:
            return pharmacy
    raise KeyError(f"Unknown pharmacy: {key}")


# Column-name fragments that mark a patient field. Matched against normalised headers.
# Detected columns are flagged and dropped before anything else happens (M3).
PATIENT_FIELD_PATTERNS: tuple[str, ...] = (
    "patient",
    "nhs",
    "dob",
    "date of birth",
    "surname",
    "forename",
    "address",
    "postcode",
    "mrn",
    "hospital number",
    "email",
    "mobile",
    "phone",
    "epes",  # EPES ID: an identifier of unconfirmed meaning, dropped to be safe
)


# Units that mean a whole pack rather than a dispensable unit. PMR counts single units, so a
# robot row in one of these will not line up. PLACEHOLDER list until real exports are seen.
PACK_UNITS: tuple[str, ...] = ("box", "pack", "carton", "case")

# Cleaning rules. All PLACEHOLDERS, written from the synthetic fixtures and pmr_test.csv; extend
# them as real exports show other spellings. Words are lowercase.
NAME_ABBREVIATIONS: dict[str, str] = {
    "tab": "tablets",
    "tabs": "tablets",
    "tablet": "tablets",
    "cap": "capsules",
    "caps": "capsules",
    "capsule": "capsules",
    "inj": "injection",
    "soln": "solution",
    "sol": "solution",
    "amox": "amoxicillin",
    "solu": "solution",
    "drps": "drops",
}
# Words dropped from the matching name: salts and release/format words that vary by source.
NAME_IGNORED_WORDS: frozenset[str] = frozenset(
    {
        "hydrochloride", "hcl", "sodium", "sulfate", "sulphate", "sulf",
        "gastro", "resistant", "gr", "powder", "for", "oral",
        "gb", "ni",  # region suffixes such as (GB) and (NI), seen on one side only
    }
)  # fmt: skip
# Multi-character spellings rewritten before the name is split into words (seen in real names).
NAME_PHRASES: dict[str, str] = {
    "p/r": "mr",  # prolonged release
    "m/r": "mr",  # modified release
}
UNIT_ALIASES: dict[str, str] = {
    "tab": "tablet",
    "tabs": "tablet",
    "tablet": "tablet",
    "tablets": "tablet",
    "cap": "capsule",
    "caps": "capsule",
    "capsule": "capsule",
    "capsules": "capsule",
    "btl": "bottle",
    "bottle": "bottle",
    "bottles": "bottle",
    "vial": "vial",
    "vials": "vial",
    "amp": "ampoule",
    "ampoule": "ampoule",
    "ampoules": "ampoule",
}
# The real stock listing's count (ISTANZAHL) may be a number of PACKS, not of units. Switch on
# to multiply it by the pack size so stock is in units like PMR quantities. PLACEHOLDER: off
# (stock used as exported) until someone confirms what the count means (OPEN_QUESTIONS.md).
# CONFIRMED 2026-11-05 by the user: the stock count (# / ISTANZAHL) is the number of boxes (packs).
STOCK_QTY_IS_PACK_COUNT = True

# Which robot an activity file's SYSTEMNAME refers to (lowercase name -> robot_id). PLACEHOLDER:
# only "Medimat1" has been seen, and which robot it is is unconfirmed (OPEN_QUESTIONS.md).
ACTIVITY_SYSTEM_ROBOT: dict[str, str] = {"medimat1": "robot_1"}

# Branch label used when the PMR gives none and the caller supplies no default.
UNKNOWN_BRANCH = "Unknown"

# Usage analysis (usage.py): a product whose quantity changed by at least this fraction against
# the window before is called rising or falling. PLACEHOLDER, "Assumption, to be confirmed".
TREND_THRESHOLD = 0.25
# ...but only for a product with at least this many scripts in the window; fewer than that is
# too little to call a trend (one large dispense can swing the quantity). PLACEHOLDER as well.
TREND_MIN_SCRIPTS = 5

# Robot suggestions (suggest.py): products are ranked by scripts. Those that together make the
# first ABC_A_SHARE of all scripts are A (fast movers), up to ABC_B_SHARE are B, the rest C (slow
# movers). The usual 80 / 15 / 5 convention. PLACEHOLDERS, "Assumption, to be confirmed".
ABC_A_SHARE = 0.80
ABC_B_SHARE = 0.95

# Matching (match.py). PLACEHOLDERS, labelled "Assumption, to be confirmed".
# Level 3 (fuzzy) proposes a candidate only when the name score (0-100) reaches the threshold
# and strengths are identical. Two candidates within the margin make the proposal "ambiguous".
FUZZY_THRESHOLD = 85
AMBIGUITY_MARGIN = 5
MAX_CANDIDATES = 3
CONFIRMED_MATCHES_PATH = Path(__file__).parent / "confirmed_matches.csv"
# A host with a disposable app folder (a container) points this at a persistent mount.
CONFIRMED_MATCHES_DIR = Path(os.environ.get("CONFIRMED_MATCHES_DIR") or Path(__file__).parent)
# Medicines the robot can hold (Robot Stock Report), names only. Optional: absent means no tags.
CATALOGUE_PATH = Path(__file__).parent / "reference" / "robot_catalogue.csv"


def confirmed_matches_path(pharmacy_key: str) -> Path:
    """Each pharmacy keeps its own confirmed decisions (a product not stocked in one may be in
    another). Looked up through the module attribute so tests can point it elsewhere."""
    return CONFIRMED_MATCHES_DIR / f"confirmed_matches_{pharmacy_key}.csv"


# Distinctive headers that identify a file type, so a file in the wrong upload slot is caught.
# A file is that type when ALL its signature headers are present (normalised, any order).
# UNCONFIRMED: taken from header names only. Types with no signature are never auto-detected.
# A type may list several alternative signatures (any one matching is enough): the OmniCell stock
# listing comes in the original German-header export and the 2026-11 friendly-header export.
FILE_TYPE_SIGNATURES: dict[str, frozenset[str] | tuple[frozenset[str], ...]] = {
    "omnicell_stock": (
        frozenset({"artikelcode", "istanzahl", "freianzahl"}),
        frozenset({"itemcode", "free#", "generickey"}),
    ),
    "omnicell_activity": frozenset({"systemname", "dispensed", "stocked", "rearranged"}),
}
FILE_TYPE_LABELS = {
    "omnicell_stock": "OmniCell stock listing",
    "omnicell_activity": "OmniCell activity statistics",
    "pmr": "PMR usage",
}
# The file type each upload slot expects.
EXPECTED_FILE_TYPE = {
    **dict.fromkeys(ROBOT_SOURCES, "omnicell_stock"),
    "pmr": "pmr",
    "omnicell_activity": "omnicell_activity",
}


# Plain-language description of each internal column, shown to whoever prepares an export when
# something is missing: what to include, and what the analysis loses without it.
COLUMN_GUIDE: dict[str, dict[str, str]] = {
    "pmr": {
        "item_name": "the drug name as dispensed (product name, with strength and form)",
        "dispense_date": "the date each item was dispensed (ideally with the day, not only the month)",
        "dispensed_qty": "the quantity dispensed per line. Without it each line counts as one, so usage is a rough proxy",
        "generic_name": "the generic or as-prescribed drug name. Helps match brand and generic",
        "pack_size": "the pack size. Helps convert between packs and units",
        "unit": "the unit of the quantity (tablet, capsule, dose...). Helps compare with robot stock",
        "item_code": "a product code that matches the robot's item code. Gives exact matching",
        "strength": "the strength, if it is not already in the drug name",
        "branch": "the pharmacy or branch name, if one file covers several",
        "marked_deleted": "a deleted flag, so deleted lines are not counted as usage",
        "not_dispensed": "a not-dispensed flag, so lines never handed out are not counted",
    },
    "omnicell_robot": {
        "item_name": "the product name",
        "stock_qty": "the stock on hand per product",
        "item_code": "the robot's item or barcode code. Gives exact matching to the PMR",
        "pack_size": "the pack size",
        "unit": "the unit the stock count is in (packs or units)",
        "strength": "the strength, if not in the name",
        "expiry_date": "the expiry date",
        "branch": "the pharmacy or branch name",
    },
    "omnicell_activity": {
        "activity_date": "the date of each daily total",
        "dispensed": "the number dispensed by the robot that day",
    },
}


def column_guide(source: str, internal: str) -> str:
    key = "omnicell_robot" if source in ROBOT_SOURCES else source
    return COLUMN_GUIDE.get(key, {}).get(internal, internal)


def load_column_map(
    path: Path | str | None = None, pharmacy: str | None = None
) -> dict[str, dict[str, list[str]]]:
    """Load column_map.yaml as {source: {internal_name: [source headers]}}.

    With a pharmacy key, that pharmacy's headers (the `pharmacies:` section) are tried first,
    so each pharmacy can have its own export format.
    """
    with open(path or COLUMN_MAP_PATH, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    per_pharmacy = raw.pop("pharmacies", None) or {}
    column_map = {
        source: {internal: list(aliases or []) for internal, aliases in (block or {}).items()}
        for source, block in raw.items()
    }
    for source, block in (per_pharmacy.get(pharmacy) or {}).items() if pharmacy else ():
        for internal, aliases in (block or {}).items():
            own = list(aliases or [])
            column_map.setdefault(source, {})[internal] = own + [
                a for a in column_map[source].get(internal, []) if a not in own
            ]
    return column_map


def column_map_problems(column_map: dict[str, dict[str, list[str]]]) -> list[str]:
    """Check a column map against the schema. Empty list means it is complete and consistent."""
    found = []
    for source in column_map:
        if source not in SOURCES:
            found.append(f"unknown source block: {source}")
    for source in SOURCES:
        block = column_map.get(source)
        if block is None:
            found.append(f"missing source block: {source}")
            continue
        for internal in block:
            if internal not in ALL_COLUMNS:
                found.append(f"{source}: unknown internal column {internal}")
        for internal in REQUIRED[source]:
            if not block.get(internal):
                found.append(f"{source}: required column {internal} has no source headers")
        allowed = {*REQUIRED[source], *OPTIONAL[source]}
        for internal in block:
            if internal in ALL_COLUMNS and internal not in allowed:
                found.append(f"{source}: {internal} is not part of this source's schema")
        owner: dict[str, str] = {}
        for internal, aliases in block.items():
            for alias in aliases:
                key = normalise_header(alias)
                if owner.setdefault(key, internal) != internal:
                    found.append(
                        f"{source}: header '{alias}' maps to both {owner[key]} and {internal}"
                    )
    return found


def header_lookup(source: str, column_map: dict | None = None) -> dict[str, str]:
    """{normalised source header: internal name} for one source."""
    column_map = column_map if column_map is not None else load_column_map()
    return {
        normalise_header(alias): internal
        for internal, aliases in column_map[source].items()
        for alias in aliases
    }


def resolve_headers(
    source: str, headers: list[str], column_map: dict | None = None
) -> tuple[dict[str, str], list[str]]:
    """Map raw headers to internal names. Returns ({raw: internal}, [unmapped raw headers])."""
    lookup = header_lookup(source, column_map)
    mapped, unmapped = {}, []
    for header in headers:
        internal = lookup.get(normalise_header(header))
        if internal is None:
            unmapped.append(header)
        else:
            mapped[header] = internal
    return mapped, unmapped


def robot_id_for(source: str) -> str:
    return ROBOT_SOURCES[source]
