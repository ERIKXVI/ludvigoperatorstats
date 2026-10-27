"""SQLite storage: tracked players, stat snapshots, and the matches we've detected."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import MatchBatch, OperatorDelta, PlayerInfo, Snapshot, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL,
    profile_id      TEXT,
    user_id         TEXT,
    added_at        TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_error      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS players_name ON players (lower(name), platform);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    taken_at    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_player ON snapshots (player_id, taken_at);

CREATE TABLE IF NOT EXISTS match_events (
    id           INTEGER PRIMARY KEY,
    player_id    INTEGER NOT NULL REFERENCES players(id),
    detected_at  TEXT NOT NULL,
    window_start TEXT,
    board        TEXT NOT NULL,
    season_id    INTEGER,
    matches      INTEGER NOT NULL,
    wins         INTEGER NOT NULL,
    losses       INTEGER NOT NULL,
    abandons     INTEGER NOT NULL,
    kills        INTEGER NOT NULL,
    deaths       INTEGER NOT NULL,
    rank_before  INTEGER,
    rank_after   INTEGER,
    rp_before    INTEGER,
    rp_after     INTEGER
);
CREATE INDEX IF NOT EXISTS match_events_player ON match_events (player_id, detected_at);

CREATE TABLE IF NOT EXISTS operator_events (
    id          INTEGER PRIMARY KEY,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    detected_at TEXT NOT NULL,
    season      TEXT,
    operator    TEXT NOT NULL,
    side        TEXT,
    rounds      INTEGER NOT NULL,
    rounds_won  INTEGER NOT NULL,
    kills       INTEGER NOT NULL,
    deaths      INTEGER NOT NULL,
    headshots   INTEGER NOT NULL,
    aces        INTEGER NOT NULL,
    clutches    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS operator_events_player ON operator_events (player_id, detected_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Logged with the "<player> complained" / "<player> argued" buttons in Discord.
CREATE TABLE IF NOT EXISTS complaints (
    id             INTEGER PRIMARY KEY,
    player_id      INTEGER NOT NULL REFERENCES players(id),
    kind           TEXT NOT NULL,
    topic          TEXT NOT NULL,
    category       TEXT,
    details        TEXT,
    created_at     TEXT NOT NULL,
    reporter_id    INTEGER,
    reporter_name  TEXT,
    guild_id       INTEGER,
    channel_id     INTEGER,
    message_id     INTEGER,
    match_event_id INTEGER REFERENCES match_events(id)
);
CREATE INDEX IF NOT EXISTS complaints_player ON complaints (player_id, created_at);
"""


@dataclass
class TrackedPlayer:
    id: int
    info: PlayerInfo
    added_at: datetime
    last_checked_at: datetime | None = None
    last_error: str | None = None

    @property
    def name(self) -> str:
        return self.info.name


@dataclass
class Complaint:
    id: int
    player_id: int
    kind: str  # "complaint" or "argument"
    topic: str  # what it was about
    details: str | None
    created_at: datetime
    reporter_name: str | None = None
    match_event_id: int | None = None
    category: str | None = None  # who/what got the blame, see COMPLAINT_CATEGORIES


COMPLAINT_KINDS = ("complaint", "argument")
# Who/what got the blame: key -> label (with emoji). Offered as a dropdown in the Discord pop-up.
COMPLAINT_CATEGORIES = {
    "teammates": "👥 Teammates",
    "enemies": "🎯 Enemies / cheaters",
    "ubisoft": "🖥️ Ubisoft / servers / netcode",
    "map": "🗺️ The map / spawns",
    "operators": "🔫 Operators / guns / gadgets",
    "self": "🪞 Their own play",
    "life": "🌍 Life in general",
    "other": "❓ Something else",
}


@dataclass
class OperatorEvent:
    detected_at: datetime
    delta: OperatorDelta


def _ts(dt: datetime | None) -> str | None:
    # Always store UTC so the ISO strings sort (and compare) chronologically.
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _batch(r: sqlite3.Row) -> MatchBatch:
    return MatchBatch(
        id=r["id"], board=r["board"], season_id=r["season_id"] or 0, matches=r["matches"], wins=r["wins"],
        losses=r["losses"], abandons=r["abandons"], kills=r["kills"], deaths=r["deaths"],
        rank_before=r["rank_before"] or 0, rank_after=r["rank_after"] or 0,
        rp_before=r["rp_before"] or 0, rp_after=r["rp_after"] or 0,
        detected_at=_dt(r["detected_at"]) or utcnow(), window_start=_dt(r["window_start"]),
    )


def fingerprint(snap: Snapshot) -> str:
    """Hash of the stats themselves (not when we fetched them), to skip storing duplicates."""
    d = snap.to_dict()
    for k in ("taken_at", "operators_fetched_at", "sources", "errors"):
        d.pop(k, None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --------------------------------------------------------------- players

    def _player(self, row: sqlite3.Row) -> TrackedPlayer:
        return TrackedPlayer(
            id=row["id"],
            info=PlayerInfo(
                name=row["name"], platform=row["platform"], profile_id=row["profile_id"], user_id=row["user_id"]
            ),
            added_at=_dt(row["added_at"]) or utcnow(),
            last_checked_at=_dt(row["last_checked_at"]),
            last_error=row["last_error"],
        )

    def add_player(self, info: PlayerInfo) -> TrackedPlayer:
        """Start tracking a player (or re-activate one we tracked before)."""
        existing = self.find_player(info.name, info.platform, include_inactive=True) or (
            self.find_player_by_user_id(info.user_id, include_inactive=True) if info.user_id else None
        )
        if existing:
            self.db.execute(
                "UPDATE players SET active = 1, name = ?, profile_id = COALESCE(?, profile_id), "
                "user_id = COALESCE(?, user_id) WHERE id = ?",
                (info.name, info.profile_id, info.user_id, existing.id),
            )
            self.db.commit()
            return self.get_player(existing.id)  # type: ignore[return-value]
        cur = self.db.execute(
            "INSERT INTO players (name, platform, profile_id, user_id, added_at) VALUES (?, ?, ?, ?, ?)",
            (info.name, info.platform, info.profile_id, info.user_id, _ts(utcnow())),
        )
        self.db.commit()
        return self.get_player(cur.lastrowid)  # type: ignore[arg-type,return-value]

    def update_player_info(self, player_id: int, info: PlayerInfo) -> None:
        self.db.execute(
            "UPDATE players SET name = ?, profile_id = COALESCE(?, profile_id), user_id = COALESCE(?, user_id) "
            "WHERE id = ?",
            (info.name, info.profile_id, info.user_id, player_id),
        )
        self.db.commit()

    def deactivate_player(self, player_id: int) -> None:
        self.db.execute("UPDATE players SET active = 0 WHERE id = ?", (player_id,))
        self.db.commit()

    def get_player(self, player_id: int) -> TrackedPlayer | None:
        row = self.db.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        return self._player(row) if row else None

    def find_player(self, name: str, platform: str | None = None, include_inactive: bool = False) -> TrackedPlayer | None:
        sql = "SELECT * FROM players WHERE lower(name) = lower(?)"
        args: list = [name.strip()]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        if not include_inactive:
            sql += " AND active = 1"
        row = self.db.execute(sql + " ORDER BY active DESC, id LIMIT 1", args).fetchone()
        return self._player(row) if row else None

    def find_player_by_user_id(self, user_id: str, include_inactive: bool = False) -> TrackedPlayer | None:
        sql = "SELECT * FROM players WHERE user_id = ?" + ("" if include_inactive else " AND active = 1")
        row = self.db.execute(sql, (user_id,)).fetchone()
        return self._player(row) if row else None

    def list_players(self, include_inactive: bool = False) -> list[TrackedPlayer]:
        sql = "SELECT * FROM players" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
        return [self._player(r) for r in self.db.execute(sql)]

    def mark_checked(self, player_id: int, error: str | None = None, when: datetime | None = None) -> None:
        self.db.execute(
            "UPDATE players SET last_checked_at = ?, last_error = ? WHERE id = ?",
            (_ts(when or utcnow()), error, player_id),
        )
        self.db.commit()

    # ------------------------------------------------------------- snapshots

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> Snapshot:
        snap = Snapshot.from_dict(json.loads(row["data"]))
        snap.taken_at = _dt(row["taken_at"]) or snap.taken_at  # the column is kept current, see save_snapshot
        return snap

    def latest_snapshot(self, player_id: int) -> Snapshot | None:
        row = self.db.execute(
            "SELECT taken_at, data FROM snapshots WHERE player_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        return self._snapshot(row) if row else None

    def save_snapshot(self, player_id: int, snap: Snapshot) -> bool:
        """Store the snapshot if the stats changed since the last one. Returns True if a new row was stored.

        If nothing changed, the last row's time is moved up instead, so "the previous check" is always
        the latest successful one (match detection uses it as the start of the window a match ended in).
        """
        fp = fingerprint(snap)
        last = self.db.execute(
            "SELECT id, fingerprint FROM snapshots WHERE player_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        if last and last["fingerprint"] == fp:
            self.db.execute("UPDATE snapshots SET taken_at = ? WHERE id = ?", (_ts(snap.taken_at), last["id"]))
            self.db.commit()
            return False
        self.db.execute(
            "INSERT INTO snapshots (player_id, taken_at, fingerprint, data) VALUES (?, ?, ?, ?)",
            (player_id, _ts(snap.taken_at), fp, json.dumps(snap.to_dict())),
        )
        self.db.commit()
        return True

    def snapshots(self, player_id: int, since: datetime | None = None) -> list[Snapshot]:
        rows = self.db.execute(
            "SELECT taken_at, data FROM snapshots WHERE player_id = ? AND taken_at >= ? ORDER BY taken_at",
            (player_id, _ts(since) or ""),
        )
        return [self._snapshot(r) for r in rows]

    # ---------------------------------------------------------------- events

    def add_match_batch(self, player_id: int, b: MatchBatch) -> MatchBatch:
        cur = self.db.execute(
            "INSERT INTO match_events (player_id, detected_at, window_start, board, season_id, matches, wins, "
            "losses, abandons, kills, deaths, rank_before, rank_after, rp_before, rp_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                player_id, _ts(b.detected_at), _ts(b.window_start), b.board, b.season_id, b.matches, b.wins,
                b.losses, b.abandons, b.kills, b.deaths, b.rank_before, b.rank_after, b.rp_before, b.rp_after,
            ),
        )
        self.db.commit()
        b.id = cur.lastrowid
        return b

    def match_events(
        self, player_id: int, since: datetime | None = None, until: datetime | None = None, after_id: int = 0
    ) -> list[MatchBatch]:
        sql = "SELECT * FROM match_events WHERE player_id = ? AND id > ?"
        args: list = [player_id, after_id]
        if since:
            sql += " AND detected_at >= ?"
            args.append(_ts(since))
        if until:
            sql += " AND detected_at < ?"
            args.append(_ts(until))
        return [_batch(r) for r in self.db.execute(sql + " ORDER BY detected_at, id", args)]

    def count_tracked_matches(self, player_id: int) -> int:
        row = self.db.execute("SELECT SUM(matches) FROM match_events WHERE player_id = ?", (player_id,)).fetchone()
        return row[0] or 0

    def add_operator_deltas(
        self, player_id: int, deltas: Iterable[OperatorDelta], detected_at: datetime, season: str | None
    ) -> None:
        self.db.executemany(
            "INSERT INTO operator_events (player_id, detected_at, season, operator, side, rounds, rounds_won, "
            "kills, deaths, headshots, aces, clutches) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (player_id, _ts(detected_at), season, d.name, d.side, d.rounds, d.rounds_won, d.kills, d.deaths,
                 d.headshots, d.aces, d.clutches)
                for d in deltas
            ],
        )
        self.db.commit()

    def operator_events(
        self, player_id: int, since: datetime | None = None, until: datetime | None = None
    ) -> list[OperatorEvent]:
        sql = "SELECT * FROM operator_events WHERE player_id = ?"
        args: list = [player_id]
        if since:
            sql += " AND detected_at >= ?"
            args.append(_ts(since))
        if until:
            sql += " AND detected_at < ?"
            args.append(_ts(until))
        rows = self.db.execute(sql + " ORDER BY detected_at, id", args)
        return [
            OperatorEvent(
                detected_at=_dt(r["detected_at"]) or utcnow(),
                delta=OperatorDelta(
                    name=r["operator"], side=r["side"], rounds=r["rounds"], rounds_won=r["rounds_won"],
                    kills=r["kills"], deaths=r["deaths"], headshots=r["headshots"], aces=r["aces"],
                    clutches=r["clutches"],
                ),
            )
            for r in rows
        ]

    # -------------------------------------------------------------- settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    # ------------------------------------------------------------ complaints

    def last_match_event(self, player_id: int) -> MatchBatch | None:
        row = self.db.execute(
            "SELECT * FROM match_events WHERE player_id = ? ORDER BY detected_at DESC, id DESC LIMIT 1", (player_id,)
        ).fetchone()
        return _batch(row) if row else None

    def add_complaint(
        self, player_id: int, kind: str, topic: str, details: str | None = None, *, category: str | None = None,
        reporter_id: int | None = None, reporter_name: str | None = None, guild_id: int | None = None,
        channel_id: int | None = None, message_id: int | None = None, match_event_id: int | None = None,
        when: datetime | None = None,
    ) -> Complaint:
        if kind not in COMPLAINT_KINDS:
            raise ValueError(f"kind must be one of {COMPLAINT_KINDS}")
        if category is not None and category not in COMPLAINT_CATEGORIES:
            raise ValueError(f"category must be one of {list(COMPLAINT_CATEGORIES)}")
        when = when or utcnow()
        topic = " ".join(topic.split())
        details = (details or "").strip() or None
        cur = self.db.execute(
            "INSERT INTO complaints (player_id, kind, topic, category, details, created_at, reporter_id, "
            "reporter_name, guild_id, channel_id, message_id, match_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (player_id, kind, topic, category, details, _ts(when), reporter_id, reporter_name, guild_id, channel_id,
             message_id, match_event_id),
        )
        self.db.commit()
        return Complaint(cur.lastrowid, player_id, kind, topic, details, when, reporter_name,  # type: ignore[arg-type]
                         match_event_id, category)

    def complaint_counts(self, player_id: int, since: datetime | None = None) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS n FROM complaints WHERE player_id = ? AND created_at >= ? GROUP BY kind",
            (player_id, _ts(since) or ""),
        )
        counts = dict.fromkeys(COMPLAINT_KINDS, 0)
        counts.update({r["kind"]: r["n"] for r in rows})
        return counts

    def complaint_topics(self, player_id: int, kind: str | None = None, limit: int = 5) -> list[tuple[str, int]]:
        """Most common things they complain/argue about (case-insensitive)."""
        sql = "SELECT MAX(topic) AS topic, COUNT(*) AS n FROM complaints WHERE player_id = ?"
        args: list = [player_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " GROUP BY lower(topic) ORDER BY n DESC, MAX(created_at) DESC LIMIT ?"
        return [(r["topic"], r["n"]) for r in self.db.execute(sql, [*args, limit])]

    def recent_complaints(self, player_id: int, limit: int = 5) -> list[Complaint]:
        rows = self.db.execute(
            "SELECT * FROM complaints WHERE player_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (player_id, limit),
        )
        return [
            Complaint(r["id"], r["player_id"], r["kind"], r["topic"], r["details"],
                      _dt(r["created_at"]) or utcnow(), r["reporter_name"], r["match_event_id"], r["category"])
            for r in rows
        ]

    def complaint_categories(self, player_id: int) -> list[tuple[str, int]]:
        """[(category key, count)] for logged complaints/arguments that picked a category."""
        rows = self.db.execute(
            "SELECT category, COUNT(*) AS n FROM complaints WHERE player_id = ? AND category IS NOT NULL "
            "GROUP BY category ORDER BY n DESC",
            (player_id,),
        )
        return [(r["category"], r["n"]) for r in rows]

    def complaint_reporters(self, player_id: int, limit: int = 3) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT MAX(reporter_name) AS name, COUNT(*) AS n FROM complaints WHERE player_id = ? "
            "GROUP BY reporter_id ORDER BY n DESC LIMIT ?",
            (player_id, limit),
        )
        return [(r["name"] or "someone", r["n"]) for r in rows]

    def complaints_by_result(self, player_id: int) -> dict[str, int]:
        """How many were logged right after a win / a loss (or with no recent match)."""
        rows = self.db.execute(
            "SELECT CASE WHEN m.id IS NULL THEN 'no recent match' "
            "            WHEN m.wins > m.losses THEN 'after a win' "
            "            WHEN m.losses > m.wins THEN 'after a loss' ELSE 'after a draw/abandon' END AS r, "
            "       COUNT(*) AS n "
            "FROM complaints c LEFT JOIN match_events m ON m.id = c.match_event_id "
            "WHERE c.player_id = ? GROUP BY r ORDER BY n DESC",
            (player_id,),
        )
        return {r["r"]: r["n"] for r in rows}
