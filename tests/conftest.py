from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from r6tracker.models import BoardStats, MatchBatch, OperatorStats, PlayerInfo, Snapshot, utcnow
from r6tracker.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()


class FakeFetcher:
    """Stands in for StatsFetcher; tests set .boards / .ops / .history to whatever the 'API' should return.

    history=None means "no match history" (matches get worked out from the totals); a list (newest
    first) means real matches, served in pages of page_size."""

    def __init__(self, with_operators: bool = True):
        self.has_operators = with_operators
        self.boards: dict[str, BoardStats] = {}
        self.ops: list[OperatorStats] = []
        self.history: list[MatchBatch] | None = None
        self.page_size = 20
        self.level = 100
        self.profile_error: Exception | None = None
        self.operator_error: Exception | None = None
        self.matches_error: Exception | None = None
        self.profile_calls = 0
        self.operator_calls = 0
        self.matches_calls = 0

    @property
    def has_matches(self) -> bool:
        return self.history is not None

    async def matches(self, player, cursor=None):
        self.matches_calls += 1
        if self.history is None:
            return None
        if self.matches_error:
            raise self.matches_error
        page = cursor or 0
        chunk = self.history[page * self.page_size:(page + 1) * self.page_size]
        more = (page + 1) * self.page_size < len(self.history)
        return [replace(m, operators=list(m.operators), extra=dict(m.extra)) for m in chunk], (page + 1 if more else None)

    async def find_player(self, name, platform="uplay"):
        return PlayerInfo(name=name, platform=platform, profile_id="pid", user_id="uid")

    async def profile(self, player):
        self.profile_calls += 1
        if self.profile_error:
            raise self.profile_error
        snap = Snapshot(player=player, level=self.level, sources=["fake"])
        snap.boards = {k: BoardStats(**vars(v)) for k, v in self.boards.items()}
        return snap

    async def operators(self, player, season="all", mode="all"):
        self.operator_calls += 1
        if self.operator_error:
            raise self.operator_error
        return [OperatorStats(**{**vars(o), "multi_kills": dict(o.multi_kills)}) for o in self.ops]


@pytest.fixture
def fetcher():
    return FakeFetcher()


def board(name="ranked", season=43, **kw) -> BoardStats:
    return BoardStats(board=name, season_id=season, **kw)


def real_match(n: int, *, won=True, minutes_ago=30, kills=8, deaths=5, board="ranked", rp=25, ops=("Ash",)) -> MatchBatch:
    """A real match (as from match history) that ended `minutes_ago` minutes ago."""
    end = utcnow() - timedelta(minutes=minutes_ago)
    return MatchBatch(
        board=board, season_id=43, matches=1, wins=int(won), losses=int(not won), abandons=0, kills=kills,
        deaths=deaths, rank_before=20, rank_after=20, rp_before=3000, rp_after=3000 + rp, detected_at=end,
        window_start=end - timedelta(minutes=25), match_id=f"m{n}", map_name="Villa", board_name="Ranked",
        duration_seconds=1500, assists=2, headshots=3, rounds_won=4 if won else 2, rounds_lost=2 if won else 4,
        operators=list(ops),
    )


def _shift(ts: str | None, minutes: int) -> str | None:
    return (datetime.fromisoformat(ts) - timedelta(minutes=minutes)).isoformat() if ts else ts


def back_in_time(storage: Storage, player_id: int, minutes: int) -> None:
    """Pretend everything stored so far happened `minutes` earlier."""
    db = storage.db
    for r in db.execute("SELECT * FROM match_events WHERE player_id = ?", (player_id,)).fetchall():
        db.execute(
            "UPDATE match_events SET detected_at = ?, window_start = ? WHERE id = ?",
            (_shift(r["detected_at"], minutes), _shift(r["window_start"], minutes), r["id"]),
        )
    for r in db.execute("SELECT * FROM operator_events WHERE player_id = ?", (player_id,)).fetchall():
        db.execute("UPDATE operator_events SET detected_at = ? WHERE id = ?", (_shift(r["detected_at"], minutes), r["id"]))
    for r in db.execute("SELECT * FROM snapshots WHERE player_id = ?", (player_id,)).fetchall():
        d = json.loads(r["data"])
        d["taken_at"] = _shift(d["taken_at"], minutes)
        d["operators_fetched_at"] = _shift(d.get("operators_fetched_at"), minutes)
        db.execute(
            "UPDATE snapshots SET taken_at = ?, data = ? WHERE id = ?",
            (_shift(r["taken_at"], minutes), json.dumps(d), r["id"]),
        )
    db.commit()


__all__ = ["load", "FakeFetcher", "board", "real_match", "back_in_time", "utcnow"]
