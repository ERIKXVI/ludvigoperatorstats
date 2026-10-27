"""Rank names for Ranked 2.0 / 3.0 (Y7S4 onward). Ubisoft reports a rank id 0-36."""

from __future__ import annotations

_TIERS = ["Copper", "Bronze", "Silver", "Gold", "Platinum", "Emerald", "Diamond"]
_DIVISIONS = ["V", "IV", "III", "II", "I"]

RANK_NAMES: list[str] = ["Unranked"] + [f"{t} {d}" for t in _TIERS for d in _DIVISIONS] + ["Champion"]

# Embed colours per tier (roughly the in-game colours).
TIER_COLORS: dict[str, int] = {
    "Unranked": 0x7F8C8D,
    "Copper": 0xB0573C,
    "Bronze": 0xA5793B,
    "Silver": 0xA7A9AC,
    "Gold": 0xE8B43A,
    "Platinum": 0x3BB5B0,
    "Emerald": 0x2ECC71,
    "Diamond": 0xA77BF3,
    "Champion": 0xE0115F,
}


def rank_name(rank_id: int | None) -> str:
    if rank_id is None or not 0 <= rank_id < len(RANK_NAMES):
        return "Unranked" if not rank_id else f"Rank {rank_id}"
    return RANK_NAMES[rank_id]


def rank_tier(rank_id: int | None) -> str:
    return rank_name(rank_id).split(" ")[0]


def rank_color(rank_id: int | None) -> int:
    return TIER_COLORS.get(rank_tier(rank_id), TIER_COLORS["Unranked"])
