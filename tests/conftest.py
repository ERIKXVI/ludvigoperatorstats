from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from r6tracker.models import BoardStats, OperatorStats, PlayerInfo, Snapshot, utcnow
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
    """Stands in for StatsFetcher; tests set .boards / .ops to whatever the 'API' should return."""

    def __init__(self, with_operators: bool = True):
        self.has_operators = with_operators
        self.boards: dict[str, BoardStats] = {}
        self.ops: list[OperatorStats] = []
        self.level = 100
        self.profile_error: Exception | None = None
        self.operator_error: Exception | None = None
        self.profile_calls = 0
        self.operator_calls = 0

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


__all__ = ["load", "FakeFetcher", "board", "back_in_time", "utcnow"]
