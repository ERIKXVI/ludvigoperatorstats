"""Plain data models shared by the API clients, the tracker, the CLI and the bot."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

# Playlists ("boards"), in the order we like to show them. Sources usually send their own display
# name too (BoardStats.name), which wins over these.
BOARD_LABELS: dict[str, str] = {
    "ranked": "Ranked",
    "quick_match": "Quick Match",
    "standard": "Standard",
    "unranked": "Unranked",
    "casual": "Casual",
    "dual_front": "Dual Front",
    "siege_cup": "Siege Cup",
    "arcade": "Arcade",
    "event": "Event",
    "warmup": "Warm-up",
}


def board_label(board: str) -> str:
    return BOARD_LABELS.get(board, board.replace("_", " ").title())


def ratio(a: float, b: float) -> float:
    """K/D style ratio: a / b, but a when b is zero (how the game shows it)."""
    return round(a / b, 2) if b else float(a)


def pct(a: float, total: float) -> float:
    return round(100.0 * a / total, 1) if total else 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def season_code(season_id: int | None) -> str | None:
    """Ubisoft numbers seasons 1, 2, 3...; Y1S1 is season 1, Y11S3 is season 43."""
    if not season_id or season_id < 1:
        return None
    return f"Y{(season_id - 1) // 4 + 1}S{(season_id - 1) % 4 + 1}"


def season_id_from_code(code: str) -> int | None:
    """'Y11S3' -> 43 (None if it isn't a season code)."""
    m = re.fullmatch(r"Y(\d+)S([1-4])", code.strip().upper())
    return (int(m[1]) - 1) * 4 + int(m[2]) if m else None


@dataclass
class PlayerInfo:
    name: str
    platform: str = "uplay"
    profile_id: str | None = None
    user_id: str | None = None

    @property
    def avatar_url(self) -> str | None:
        if not self.user_id:
            return None
        return f"https://ubisoft-avatars.akamaized.net/{self.user_id}/default_256_256.png"


@dataclass
class BoardStats:
    """Season stats for one playlist (ranked, quick match, ...)."""

    board: str
    season_id: int = 0
    kills: int = 0
    deaths: int = 0
    wins: int = 0
    losses: int = 0
    abandons: int = 0
    rank: int = 0
    rank_points: int = 0
    max_rank: int = 0
    max_rank_points: int = 0
    top_rank_position: int = 0
    name: str | None = None  # display name from the source, e.g. "Quick Match"

    @property
    def matches(self) -> int:
        return self.wins + self.losses + self.abandons

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)

    @property
    def win_rate(self) -> float:
        return pct(self.wins, self.matches)

    @property
    def label(self) -> str:
        return self.name or board_label(self.board)


@dataclass
class OperatorStats:
    """Stats for one operator (for one season + playlist filter)."""

    name: str
    side: str | None = None  # "attacker" / "defender"
    rounds_played: int = 0
    rounds_won: int = 0
    rounds_lost: int = 0
    matches_played: int = 0
    matches_won: int = 0
    matches_lost: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    headshots: int = 0
    aces: int = 0
    first_bloods: int = 0
    first_deaths: int = 0
    team_kills: int = 0
    clutches: int = 0
    clutches_lost: int = 0
    multi_kills: dict[str, int] = field(default_factory=dict)  # {"1k": n, ..., "5k": n}
    time_played_ms: int = 0

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)

    @property
    def win_rate(self) -> float:
        """Round win %."""
        return pct(self.rounds_won, self.rounds_played)

    @property
    def headshot_pct(self) -> float:
        return pct(self.headshots, self.kills)

    @property
    def kills_per_round(self) -> float:
        return round(self.kills / self.rounds_played, 2) if self.rounds_played else 0.0

    @property
    def survival_pct(self) -> float:
        return pct(max(self.rounds_played - self.deaths, 0), self.rounds_played)

    @property
    def key(self) -> str:
        return f"{self.name}|{self.side or ''}"


@dataclass
class Snapshot:
    """Everything we know about a player at one moment."""

    player: PlayerInfo
    taken_at: datetime = field(default_factory=utcnow)
    level: int | None = None
    xp: int | None = None
    playtime_seconds: int | None = None
    pvp_seconds: int | None = None
    pve_seconds: int | None = None
    battle_pass_level: int | None = None
    commendations: dict[str, int] = field(default_factory=dict)
    boards: dict[str, BoardStats] = field(default_factory=dict)  # this season, per playlist
    lifetime: BoardStats | None = None  # all seasons, all playlists
    # Extra lifetime numbers when the source has them: headshots, headshot_pct, assists, team_kills,
    # wallbangs, damage_dealt, kills_per_match, time_alive_seconds...
    extras: dict[str, float] = field(default_factory=dict)
    playstyles: dict[str, int] = field(default_factory=dict)  # e.g. {"attacker_entry_fragger": 4598}
    name_changes: list[str] = field(default_factory=list)  # earlier names, newest first
    operators: list[OperatorStats] = field(default_factory=list)
    operator_season: str | None = None  # e.g. "Y11S3"
    operator_mode: str | None = None  # e.g. "all"
    operators_fetched_at: datetime | None = None
    banned: bool | None = None
    sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def season_id(self) -> int:
        ids = [b.season_id for b in self.boards.values() if b.season_id]
        return max(ids) if ids else 0

    @property
    def total(self) -> BoardStats:
        """All playlists added together."""
        t = BoardStats(board="all", season_id=self.season_id)
        for b in self.boards.values():
            t.kills += b.kills
            t.deaths += b.deaths
            t.wins += b.wins
            t.losses += b.losses
            t.abandons += b.abandons
        return t

    @property
    def ranked(self) -> BoardStats | None:
        return self.boards.get("ranked")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["taken_at"] = self.taken_at.isoformat()
        if self.operators_fetched_at:
            d["operators_fetched_at"] = self.operators_fetched_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        d = dict(d)
        d["player"] = _build(PlayerInfo, d["player"])
        d["taken_at"] = datetime.fromisoformat(d["taken_at"])
        if d.get("operators_fetched_at"):
            d["operators_fetched_at"] = datetime.fromisoformat(d["operators_fetched_at"])
        d["boards"] = {k: _build(BoardStats, v) for k, v in d.get("boards", {}).items()}
        if d.get("lifetime"):
            d["lifetime"] = _build(BoardStats, d["lifetime"])
        d["operators"] = [_build(OperatorStats, o) for o in d.get("operators", [])]
        return _build(cls, d)


def _build(cls, data: dict[str, Any]):
    """Construct a dataclass ignoring unknown keys (so old saved snapshots keep loading)."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class OperatorDelta:
    """What changed for one operator between two snapshots."""

    name: str
    side: str | None
    rounds: int = 0
    rounds_won: int = 0
    kills: int = 0
    deaths: int = 0
    headshots: int = 0
    aces: int = 0
    clutches: int = 0

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)


@dataclass
class MatchBatch:
    """New matches on one playlist. With a match-history source it's exactly one real match
    (match_id, map, operators... are filled in); otherwise it's what changed between two polls."""

    board: str
    season_id: int
    matches: int
    wins: int
    losses: int
    abandons: int
    kills: int
    deaths: int
    rank_before: int = 0
    rank_after: int = 0
    rp_before: int = 0
    rp_after: int = 0
    detected_at: datetime = field(default_factory=utcnow)  # when the match ended (or when we noticed)
    window_start: datetime | None = None  # when it started (or the previous check)
    id: int | None = None
    # Only known for real matches from a match-history source:
    match_id: str | None = None
    map_name: str | None = None
    board_name: str | None = None  # the source's playlist name
    duration_seconds: int = 0
    assists: int = 0
    headshots: int = 0
    rounds_won: int = 0
    rounds_lost: int = 0
    operators: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # first_bloods, clutches, aces, kills_3k, surrender...

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)

    @property
    def rp_change(self) -> int:
        return self.rp_after - self.rp_before

    @property
    def label(self) -> str:
        return self.board_name or board_label(self.board)

    @property
    def result_text(self) -> str:
        if self.matches == 1:
            if self.wins:
                return "WIN"
            if self.losses:
                return "LOSS"
            return "ABANDON"
        parts = [f"{self.wins}W", f"{self.losses}L"]
        if self.abandons:
            parts.append(f"{self.abandons} abandoned")
        return " ".join(parts)
