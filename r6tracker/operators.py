"""Operator metadata and icons (from r6operators by marcopixel, see data/ICONS_LICENSE.txt)."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
ICON_DIR = DATA_DIR / "icons"
ICON_BASE_URL = "https://r6operators.marcopixel.eu"

# Names the stats APIs use that don't map to an r6operators id by simple normalisation.
_ALIASES = {
    "recruit": "recruit_blue",
    "recruitattacker": "striker",
    "recruitdefender": "sentry",
}


@dataclass(frozen=True)
class OperatorInfo:
    id: str
    name: str
    role: str  # Attacker / Defender / Recruit
    org: str | None = None
    squad: str | None = None
    season: str | None = None
    country: str | None = None
    real_name: str | None = None

    @property
    def icon_path(self) -> Path | None:
        p = ICON_DIR / f"{self.id}.png"
        return p if p.exists() else None

    @property
    def side(self) -> str | None:
        return {"Attacker": "attacker", "Defender": "defender"}.get(self.role)


def normalize(name: str) -> str:
    """'Jäger' -> 'jager', 'Nøkk' -> 'nokk', 'Capitão' -> 'capitao'."""
    name = name.lower().replace("ø", "o").replace("æ", "ae").replace("ß", "ss")
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", name)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, OperatorInfo]:
    raw = json.loads((DATA_DIR / "operators.json").read_text(encoding="utf-8"))
    return {o["id"]: OperatorInfo(**o) for o in raw}


@lru_cache(maxsize=1)
def _by_normalized_name() -> dict[str, OperatorInfo]:
    out: dict[str, OperatorInfo] = {}
    for op in _catalog().values():
        out[normalize(op.id)] = op
        out[normalize(op.name)] = op
    for alias, op_id in _ALIASES.items():
        if op_id in _catalog():
            out[alias] = _catalog()[op_id]
    return out


def all_operators() -> list[OperatorInfo]:
    return sorted(
        (o for o in _catalog().values() if o.role != "Recruit"),
        key=lambda o: normalize(o.name),
    )


def find(name: str | None) -> OperatorInfo | None:
    """Look an operator up by any spelling the APIs (or a human) might use."""
    if not name:
        return None
    return _by_normalized_name().get(normalize(name))


def display_name(name: str) -> str:
    op = find(name)
    return op.name if op else name


def icon_path(name: str) -> Path | None:
    op = find(name)
    return op.icon_path if op else None


def icon_url(name: str) -> str | None:
    """PNG hosted on the r6operators site (Discord embeds can't show SVG)."""
    op = find(name)
    return f"{ICON_BASE_URL}/icons/png/{op.id}.png" if op else None


def side_of(name: str) -> str | None:
    op = find(name)
    return op.side if op else None
