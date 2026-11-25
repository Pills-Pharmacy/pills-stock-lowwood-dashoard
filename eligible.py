"""Robot catalogue: the medicines the robot can hold, from the pharmacy's Robot Stock Report.

The report (PDF, converted to CSV) lists everything that can be stored in the robot, not what
is in it now. Its stock figures are the pharmacy system's book stock, so only names are used.
Names are turned into the same product key the PMR uses (`match.make_key`), so a PMR product
can be checked against the catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from clean import clean_text, normalise_name, parse_strength
from match import make_key

NAME_COLUMN = "trade_name"


@dataclass
class Catalogue:
    keys: set[str]  # product keys of every usable catalogue name
    rows: int  # rows read
    skipped: int  # rows with no usable name

    def __contains__(self, key: object) -> bool:
        return key in self.keys


def load_catalogue(path: Path | str) -> Catalogue:
    """Read the catalogue CSV into a set of product keys. Duplicate names collapse to one key."""
    frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    if NAME_COLUMN not in frame:
        raise ValueError(f"{path}: no '{NAME_COLUMN}' column (found: {list(frame.columns)})")
    keys: set[str] = set()
    skipped = 0
    for raw in frame[NAME_COLUMN]:
        name = clean_text(raw)
        name_norm = normalise_name(name)
        if not name_norm:
            skipped += 1
            continue
        keys.add(make_key(name_norm, parse_strength(name)))
    return Catalogue(keys=keys, rows=len(frame), skipped=skipped)
