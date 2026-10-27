"""Parse the Arenyze API (which mirrors Ubisoft's full_profiles format). Written defensively:
these APIs are unofficial and change."""

from __future__ import annotations

from typing import Any

from .. import operators as opmeta
from ..models import BoardStats, OperatorStats

# Ubisoft has renamed boards before; map internal names to the ones we use.
BOARD_RENAMES = {"living_game_mode": "dual_front", "pvp_ranked": "ranked", "pvp_casual": "casual"}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _first(d: dict, *keys: str, default: Any = 0) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def parse_full_profiles(data: dict | None) -> dict[str, BoardStats]:
    """Parse the `full_profiles` payload (Ubisoft's raw one, or Arenyze's copy of it)."""
    boards: dict[str, BoardStats] = {}
    if not isinstance(data, dict):
        return boards
    for family in data.get("platform_families_full_profiles") or []:
        for board in family.get("board_ids_full_profiles") or []:
            board_id = board.get("board_id")
            board_id = BOARD_RENAMES.get(board_id, board_id)
            entries = [e for e in board.get("full_profiles") or [] if isinstance(e, dict)]
            if not board_id or not entries or board_id in boards:
                continue
            # Arenyze can list several seasons; keep the newest.
            entry = max(entries, key=lambda e: _int(_season_of(e)))
            boards[board_id] = _parse_board(board_id, entry)
    return boards


def _season_of(entry: dict) -> Any:
    return entry.get("season_id") or (entry.get("profile") or {}).get("season_id")


def _parse_board(board_id: str, entry: dict) -> BoardStats:
    profile = entry.get("profile") or {}
    season = entry.get("season_statistics") or {}
    outcomes = season.get("match_outcomes") or {}

    # Season numbers live in season_statistics; Arenyze also copies them into profile.
    def stat(src: dict, key: str, *fallbacks: str) -> int:
        return _int(src[key] if key in src else _first(profile, key, *fallbacks))

    return BoardStats(
        board=board_id,
        season_id=_int(_season_of(entry)),
        kills=stat(season, "kills"),
        deaths=stat(season, "deaths"),
        wins=stat(outcomes, "wins"),
        losses=stat(outcomes, "losses"),
        abandons=stat(outcomes, "abandons", "abandon"),
        rank=_int(profile.get("rank")),
        rank_points=_int(profile.get("rank_points")),
        max_rank=_int(profile.get("max_rank")),
        max_rank_points=_int(profile.get("max_rank_points")),
        top_rank_position=_int(profile.get("top_rank_position")),
    )


def _side(raw: Any, name: str) -> str | None:
    if isinstance(raw, str) and raw.strip():
        s = raw.strip().lower()
        if s.startswith("att"):
            return "attacker"
        if s.startswith("def"):
            return "defender"
    return opmeta.side_of(name)


def parse_operator_list(data: Any) -> list[OperatorStats]:
    """Parse Arenyze's /v2/operators response (also accepts a bare list of entries)."""
    entries: Any = data
    if isinstance(entries, dict):
        entries = entries.get("operators", entries)
    if isinstance(entries, dict):
        entries = entries.get("operators", [])
    if not isinstance(entries, list):
        return []
    ops: list[OperatorStats] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw_name = _first(e, "operator", "name", "statsDetail", default="")
        if not raw_name:
            continue
        name = opmeta.display_name(str(raw_name))
        ops.append(
            OperatorStats(
                name=name,
                side=_side(_first(e, "side", "role", "teamRole", default=None), name),
                rounds_played=_int(e.get("roundsPlayed")),
                rounds_won=_int(_first(e, "wins", "roundsWon")),
                rounds_lost=_int(_first(e, "losses", "roundsLost")),
                matches_played=_int(e.get("matchesPlayed")),
                matches_won=_int(e.get("matchesWon")),
                matches_lost=_int(e.get("matchesLost")),
                kills=_int(e.get("kills")),
                deaths=_int(_first(e, "deaths", "death")),
                assists=_int(e.get("assists")),
                headshots=_int(e.get("headshots")),
                aces=_int(e.get("aces")),
                first_bloods=_int(_first(e, "firstBloods", "openingKills")),
                first_deaths=_int(_first(e, "firstDeaths", "openingDeaths")),
                team_kills=_int(e.get("teamKills")),
                clutches=_int(e.get("clutches")),
                clutches_lost=_int(e.get("clutchesLost")),
                multi_kills={f"{n}k": _int(e.get(f"kills{n}K")) for n in range(1, 6) if f"kills{n}K" in e},
                time_played_ms=_int(e.get("timePlayedMs")),
            )
        )
    return merge_duplicate_operators(ops)


def merge_duplicate_operators(ops: list[OperatorStats]) -> list[OperatorStats]:
    """If an API ever lists the same operator+side twice, add them up instead of guessing."""
    merged: dict[str, OperatorStats] = {}
    for op in ops:
        if op.key not in merged:
            merged[op.key] = op
            continue
        m = merged[op.key]
        for f in (
            "rounds_played", "rounds_won", "rounds_lost", "matches_played", "matches_won", "matches_lost",
            "kills", "deaths", "assists", "headshots", "aces", "first_bloods", "first_deaths",
            "team_kills", "clutches", "clutches_lost", "time_played_ms",
        ):
            setattr(m, f, getattr(m, f) + getattr(op, f))
        for k, v in op.multi_kills.items():
            m.multi_kills[k] = m.multi_kills.get(k, 0) + v
    return list(merged.values())
