"""Parse Tracker Network (R6 Tracker) responses into our models.

Every stat is a node like {"value": 3839, "displayValue": "3,839", "displayType": "Number", "metadata": {...}}.
Time stats come in seconds or milliseconds depending on the endpoint, so units are read from displayType.
Written defensively: nothing here is an official contract.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import operators as opmeta
from ..models import BoardStats, MatchBatch, OperatorStats, PlayerInfo, Snapshot, utcnow
from .parse import merge_duplicate_operators

# Tracker Network gamemode ids -> our board ids.
GAMEMODE_BOARDS = {
    "pvp_ranked": "ranked",
    "pvp_casual": "quick_match",
    "pvp_standard": "standard",
    "pvp_unranked": "unranked",
    "pvp_living_game_mode": "dual_front",
    "pvp_r6cup": "siege_cup",
    "pvp_warmup": "arcade",
    "pvp_event": "event",
    "pvp_quickplay": "quickplay",  # lifetime "Standard + Quick Match" combined
}

PLATFORM_SLUGS = {"uplay": "ubi", "psn": "psn", "xbl": "xbl"}
SLUG_PLATFORMS = {"ubi": "uplay", "uplay": "uplay", "psn": "psn", "xbl": "xbl"}

_PLAYSTYLE = re.compile(r"playstyle(Attacker|Defender)(\w+)")


# ------------------------------------------------------------------ basics


def _data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def _node(stats: dict, *keys: str) -> dict | None:
    for k in keys:
        n = stats.get(k)
        if isinstance(n, dict) and n.get("value") is not None:
            return n
    return None


def num(stats: dict, *keys: str, default: float = 0) -> float:
    n = _node(stats, *keys)
    if n is None:
        return default
    try:
        return float(n["value"])
    except (TypeError, ValueError):
        return default


def count(stats: dict, *keys: str) -> int:
    return int(round(num(stats, *keys)))


def seconds(stats: dict, *keys: str) -> int:
    """A time stat in seconds, whatever unit the endpoint used."""
    n = _node(stats, *keys)
    if n is None:
        return 0
    value = float(n["value"] or 0)
    return int(value / 1000) if n.get("displayType") == "TimeMilliseconds" else int(value)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    # Python < 3.11 only accepts 3 or 6 fractional digits.
    s = re.sub(r"\.(\d+)", lambda m: "." + (m[1] + "000000")[:6], s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def board_of(attributes: dict) -> str:
    gamemode = str(attributes.get("gamemode") or "")
    if gamemode in GAMEMODE_BOARDS:
        return GAMEMODE_BOARDS[gamemode]
    session = str(attributes.get("sessionType") or gamemode or "unknown")
    return session.replace("-", "_").removeprefix("pvp_")


# ----------------------------------------------------------------- profile


def player_from_profile(payload: Any, platform: str) -> PlayerInfo:
    data = _data(payload) or {}
    info = data.get("platformInfo") or {}
    meta = data.get("metadata") or {}
    uid = info.get("platformUserId") or meta.get("uplayUserId")
    return PlayerInfo(
        name=info.get("platformUserHandle") or info.get("platformUserIdentifier") or "",
        platform=SLUG_PLATFORMS.get(str(info.get("platformSlug") or ""), platform),
        profile_id=uid,
        user_id=uid,
    )


def snapshot_from_profile(payload: Any, platform: str) -> Snapshot:
    data = _data(payload) or {}
    meta = data.get("metadata") or {}
    segments = [s for s in data.get("segments") or [] if isinstance(s, dict)]
    snap = Snapshot(player=player_from_profile(payload, platform))
    snap.level = _int_or_none(meta.get("clearanceLevel"))
    snap.battle_pass_level = _int_or_none(meta.get("battlepassLevel"))
    changes = [c for c in meta.get("nameChanges") or [] if isinstance(c, dict) and c.get("name")]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    changes.sort(key=lambda c: parse_time(c.get("timestamp")) or oldest, reverse=True)  # newest first
    snap.name_changes = [c["name"] for c in changes]
    bans = meta.get("banAlerts")
    if isinstance(bans, list):
        snap.banned = bool(bans)

    overview = next((s for s in segments if s.get("type") == "overview"), None)
    if overview:
        st = overview.get("stats") or {}
        snap.playtime_seconds = seconds(st, "timePlayed") or None
        snap.pvp_seconds = seconds(st, "pvpTimePlayed") or None
        snap.lifetime = BoardStats(
            board="lifetime",
            kills=count(st, "kills"),
            deaths=count(st, "deaths"),
            wins=count(st, "matchesWon"),
            losses=count(st, "matchesLost"),
            abandons=count(st, "matchesAbandoned"),
            name="Lifetime",
        )
        hs, kills = num(st, "headshots"), num(st, "kills")
        extras = {
            "headshots": hs,
            # % of kills, like everywhere else in the bot. (headshotPercentage is headshot *accuracy*:
            # headshots / (headshots + headshotsMissed).)
            "headshot_pct": round(100 * hs / kills, 2) if hs and kills else num(st, "headshotPct"),
            "assists": num(st, "assists"),
            "team_kills": num(st, "teamKills"),
            "wallbangs": num(st, "wallbangs"),
            "damage_dealt": num(st, "damageDealt"),
            "kills_per_match": num(st, "killsPerMatch"),
            "time_alive_seconds": seconds(st, "timeAlive"),
        }
        snap.extras = {k: v for k, v in extras.items() if v}
        for key in st:
            m = _PLAYSTYLE.fullmatch(key)
            if m and count(st, key):
                snap.playstyles[f"{m[1].lower()}_{_snake(m[2])}"] = count(st, key)

    seasons = [s for s in segments if s.get("type") == "season"]
    current = _int_or_none(meta.get("currentSeason")) or max(
        (_int_or_none((s.get("attributes") or {}).get("season")) or 0 for s in seasons), default=0
    )
    best: dict[str, dict] = {}
    for s in seasons:
        attrs = s.get("attributes") or {}
        if _int_or_none(attrs.get("season")) != current:
            continue
        board = board_of(attrs)
        if board not in best or _prefer(s, best[board]):
            best[board] = s
    for board, s in best.items():
        snap.boards[board] = _season_board(board, current, s)
    return snap


def _prefer(a: dict, b: dict) -> bool:
    """Older seasons can be split by region: prefer the unsplit one, then the one with more matches."""
    ra, rb = bool((a.get("attributes") or {}).get("region")), bool((b.get("attributes") or {}).get("region"))
    if ra != rb:
        return not ra
    return num(a.get("stats") or {}, "matchesPlayed") > num(b.get("stats") or {}, "matchesPlayed")


def _season_board(board: str, season: int, seg: dict) -> BoardStats:
    st = seg.get("stats") or {}
    meta = seg.get("metadata") or {}
    return BoardStats(
        board=board,
        season_id=season,
        kills=count(st, "kills"),
        deaths=count(st, "deaths"),
        wins=count(st, "matchesWon"),
        losses=count(st, "matchesLost"),
        abandons=count(st, "matchesAbandoned"),
        rank=count(st, "rank"),
        rank_points=count(st, "rankPoints", "mmr"),
        max_rank=count(st, "maxRank"),
        max_rank_points=count(st, "maxRankPoints", "maxMmr"),
        top_rank_position=count(st, "topRankPosition"),
        name=meta.get("gamemodeName") or meta.get("sessionTypeName"),
    )


# --------------------------------------------------------------- operators


def parse_operators(payload: Any) -> list[OperatorStats]:
    """/profile/{platform}/{name}/segments/operator: a list of operator segments."""
    entries = _data(payload)
    if isinstance(entries, dict):
        entries = entries.get("segments") or entries.get("operators") or []
    if not isinstance(entries, list):
        return []
    ops = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        attrs = e.get("attributes") or {}
        meta = e.get("metadata") or {}
        raw = meta.get("operatorName") or attrs.get("operator")
        if not raw:
            continue
        name = opmeta.display_name(str(raw))
        st = e.get("stats") or {}
        kills = count(st, "kills")
        headshots = count(st, "headshots") if _node(st, "headshots") else round(num(st, "headshotPct") * kills / 100)
        side = str(attrs.get("side") or "").lower()
        ops.append(
            OperatorStats(
                name=name,
                side=side if side in ("attacker", "defender") else opmeta.side_of(name),
                rounds_played=count(st, "roundsPlayed"),
                rounds_won=count(st, "roundsWon"),
                rounds_lost=count(st, "roundsLost"),
                matches_played=count(st, "matchesPlayed"),
                matches_won=count(st, "matchesWon"),
                matches_lost=count(st, "matchesLost"),
                kills=kills,
                deaths=count(st, "deaths"),
                assists=count(st, "assists"),
                headshots=headshots,
                aces=count(st, "aces"),
                first_bloods=count(st, "firstBloods", "openingKills"),
                first_deaths=count(st, "firstDeaths", "openingDeaths"),
                team_kills=count(st, "teamKills"),
                clutches=count(st, "clutches"),
                clutches_lost=count(st, "clutchesLost"),
                multi_kills={f"{n}k": count(st, f"kills{n}K") for n in range(1, 7) if _node(st, f"kills{n}K")},
                time_played_ms=seconds(st, "timePlayed") * 1000,
            )
        )
    return merge_duplicate_operators(ops)


# ----------------------------------------------------------------- matches


def requesting_player_id(payload: Any) -> str | None:
    """The Ubisoft id a /matches response is about (to make sure it's the right account)."""
    data = _data(payload)
    if isinstance(data, dict):
        return (data.get("requestingPlayerAttributes") or {}).get("playerId")
    return None


def parse_matches(payload: Any, player: PlayerInfo) -> tuple[list[MatchBatch], int | None]:
    """/matches/{platform}/{name}: one page of real matches (newest first) and the next-page cursor."""
    data = _data(payload) or {}
    if not isinstance(data, dict):
        return [], None
    me = (data.get("requestingPlayerAttributes") or {}).get("playerId") or player.user_id
    out = []
    for m in data.get("matches") or []:
        if isinstance(m, dict):
            b = _match(m, me, player.name)
            if b:
                out.append(b)
    # Sometimes the "previous" RP/rank of a match is null. The next older match on the same
    # playlist ended where this one started, so take it from there.
    for i, b in enumerate(out):
        older = next((o for o in out[i + 1:] if o.board == b.board), None)
        if older is None:
            continue
        if b.extra.get("rp_unknown"):
            b.rp_before = older.rp_after
            del b.extra["rp_unknown"]
        if b.extra.pop("rank_unknown", False):
            b.rank_before = older.rank_after
    cursor = (data.get("metadata") or {}).get("next")
    return out, cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else None


def _match(m: dict, me: str | None, name: str) -> MatchBatch | None:
    attrs = m.get("attributes") or {}
    meta = m.get("metadata") or {}
    if meta.get("isRollback") or not attrs.get("id"):
        return None  # rolled-back matches don't count in game either
    overviews = [s for s in m.get("segments") or [] if isinstance(s, dict) and s.get("type") == "overview"]
    seg = next((s for s in overviews if me and (s.get("attributes") or {}).get("playerId") == me), None)
    seg = seg or next(
        (s for s in overviews if str((s.get("metadata") or {}).get("platformUserIdentifier", "")).lower() == name.lower()),
        None,
    )
    if seg is None and len(overviews) == 1:
        seg = overviews[0]
    if seg is None:
        return None
    st = seg.get("stats") or {}
    smeta = seg.get("metadata") or {}

    wins, losses, abandons = count(st, "wins"), count(st, "losses"), count(st, "abandons")
    if wins + losses + abandons == 0:
        result = str(smeta.get("result") or "").lower()
        wins, losses = int(result == "win" or smeta.get("hasWon") is True), int(result == "loss")
        abandons = int(not wins and not losses)
    duration = int((meta.get("duration") or 0) / 1000) or seconds(st, "timePlayed")
    # metadata.timestamp is when the match ENDED: back-to-back matches only line up (no overlaps) that way.
    end = parse_time(meta.get("timestamp")) or utcnow()
    rp_after = count(st, "rankPoints")
    if _node(st, "rankPointsPrevious"):
        rp_before, rp_known = count(st, "rankPointsPrevious"), True
    elif _node(st, "rankPointsDelta"):
        rp_before, rp_known = rp_after - count(st, "rankPointsDelta"), True
    else:
        rp_before, rp_known = rp_after, False  # unknown, not "no change"; parse_matches may fill it in
    rank_known = _node(st, "rankPrevious") is not None

    extra: dict[str, Any] = {k: count(st, key) for k, key in (
        ("first_bloods", "firstBloods"), ("first_deaths", "firstDeaths"), ("clutches", "clutches"),
        ("clutches_lost", "clutchesLost"), ("aces", "aces"), ("team_kills", "teamKills"),
        ("kills_3k", "kills3K"), ("kills_4k", "kills4K"), ("kills_5k", "kills5K"),
    ) if count(st, key)}
    if meta.get("isSurrender") or meta.get("isForfeit"):
        extra["surrender"] = True
    if not rp_known and _node(st, "rankPoints"):
        extra["rp_unknown"] = True
    if not rank_known and _node(st, "rank"):
        extra["rank_unknown"] = True

    return MatchBatch(
        board=board_of(attrs),
        season_id=0,
        matches=1,
        wins=min(wins, 1),
        losses=min(losses, 1),
        abandons=min(abandons, 1) if not (wins or losses) else 0,
        kills=count(st, "kills"),
        deaths=count(st, "deaths"),
        rank_before=count(st, "rankPrevious") if rank_known else count(st, "rank"),
        rank_after=count(st, "rank"),
        rp_before=rp_before,
        rp_after=rp_after,
        detected_at=end,
        window_start=end - timedelta(seconds=duration),
        match_id=str(attrs["id"]),
        map_name=meta.get("sessionMapName") or _title(attrs.get("sessionMap")),
        board_name=meta.get("sessionTypeName") or meta.get("gamemodeName"),
        duration_seconds=duration,
        assists=count(st, "assists"),
        headshots=count(st, "headshots"),
        rounds_won=count(st, "roundsWon"),
        rounds_lost=count(st, "roundsLost"),
        operators=[opmeta.display_name(str(o["name"])) for o in smeta.get("operators") or []
                   if isinstance(o, dict) and o.get("name")],
        extra=extra,
    )


# ------------------------------------------------------------------- utils


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _title(v: Any) -> str | None:
    return str(v).replace("_", " ").title() if v else None
