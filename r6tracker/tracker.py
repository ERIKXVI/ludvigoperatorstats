"""Polls stats, records new matches, and summarises sessions.

With R6 Tracker the source has a real match history: each poll fetches the latest matches and
stores the ones we haven't seen (by match id), with map, score, K/D/A, RP change and operators.

Sources without match history only give running totals ("142 ranked wins this season"). Then
every poll compares with the previous numbers: if wins+losses+abandons went up on a playlist,
those are new matches, and the change in kills/deaths/RP is how they went.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo

from .fetcher import StatsFetcher
from .models import (
    BoardStats,
    MatchBatch,
    OperatorDelta,
    OperatorStats,
    Snapshot,
    pct,
    ratio,
    season_code,
    utcnow,
)
from .sources import SourceError
from .storage import Storage, TrackedPlayer

log = logging.getLogger(__name__)

OPERATOR_MODE = "all"  # operator stats we track: all playlists together
MAX_MATCH_PAGES = 5  # when catching up after downtime, read at most this many pages of match history


# ------------------------------------------------------------------ diffing


def _plausible(matches: int, window: timedelta | None) -> bool:
    """Guard against API glitches turning into "played 300 matches in 5 minutes"."""
    if window is None:
        return True
    return matches <= window.total_seconds() / 600 + 3  # at most ~1 match per 10 minutes, plus slack


def diff_boards(prev: Snapshot, cur: Snapshot) -> list[MatchBatch]:
    batches = []
    window = cur.taken_at - prev.taken_at
    for board_id, b in cur.boards.items():
        base = prev.boards.get(board_id)
        if base is None or b.season_id > base.season_id:
            if base is None and prev.season_id and b.season_id < prev.season_id:
                continue
            base = BoardStats(board=board_id, season_id=b.season_id)  # new playlist/season: counts start at 0
        elif b.season_id < base.season_id:
            continue
        d = {k: getattr(b, k) - getattr(base, k) for k in ("wins", "losses", "abandons", "kills", "deaths")}
        matches = d["wins"] + d["losses"] + d["abandons"]
        if matches <= 0:
            continue
        if min(d.values()) < 0 or not _plausible(matches, window):
            log.warning("Ignoring odd stat change on %s for %s: %s", board_id, cur.player.name, d)
            continue
        batches.append(
            MatchBatch(
                board=board_id,
                season_id=b.season_id,
                matches=matches,
                rank_before=base.rank,
                rank_after=b.rank,
                rp_before=base.rank_points,
                rp_after=b.rank_points,
                detected_at=cur.taken_at,
                window_start=prev.taken_at,
                **d,
            )
        )
    return batches


def diff_operators(prev_ops: list[OperatorStats], cur_ops: list[OperatorStats]) -> list[OperatorDelta]:
    before = {o.key: o for o in prev_ops}
    deltas = []
    for op in cur_ops:
        b = before.get(op.key) or OperatorStats(name=op.name, side=op.side)
        rounds = op.rounds_played - b.rounds_played
        if rounds <= 0:
            continue
        deltas.append(
            OperatorDelta(
                name=op.name,
                side=op.side,
                rounds=rounds,
                rounds_won=max(op.rounds_won - b.rounds_won, 0),
                kills=max(op.kills - b.kills, 0),
                deaths=max(op.deaths - b.deaths, 0),
                headshots=max(op.headshots - b.headshots, 0),
                aces=max(op.aces - b.aces, 0),
                clutches=max(op.clutches - b.clutches, 0),
            )
        )
    return sorted(deltas, key=lambda d: (-d.rounds, d.name))


def sum_operator_deltas(deltas: list[OperatorDelta]) -> list[OperatorDelta]:
    total: dict[tuple[str, str | None], OperatorDelta] = {}
    for d in deltas:
        t = total.setdefault((d.name, d.side), OperatorDelta(name=d.name, side=d.side))
        for f in ("rounds", "rounds_won", "kills", "deaths", "headshots", "aces", "clutches"):
            setattr(t, f, getattr(t, f) + getattr(d, f))
    return sorted(total.values(), key=lambda d: (-d.rounds, d.name))


# ---------------------------------------------------------------- summaries


@dataclass
class PlaySummary:
    """Totals over a set of detected matches (a session, a day, ...)."""

    events: list[MatchBatch]
    operators: list[OperatorDelta] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        if not self.events:
            return None
        first = self.events[0]
        return first.window_start or first.detected_at

    @property
    def end(self) -> datetime | None:
        return self.events[-1].detected_at if self.events else None

    def _sum(self, attr: str) -> int:
        return sum(getattr(e, attr) for e in self.events)

    matches = property(lambda self: self._sum("matches"))
    wins = property(lambda self: self._sum("wins"))
    losses = property(lambda self: self._sum("losses"))
    abandons = property(lambda self: self._sum("abandons"))
    kills = property(lambda self: self._sum("kills"))
    deaths = property(lambda self: self._sum("deaths"))

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)

    @property
    def win_rate(self) -> float:
        return pct(self.wins, self.matches)

    @property
    def by_board(self) -> dict[str, "PlaySummary"]:
        groups: dict[str, list[MatchBatch]] = defaultdict(list)
        for e in self.events:
            groups[e.board].append(e)
        return {b: PlaySummary(evs) for b, evs in groups.items()}

    @property
    def ranked_events(self) -> list[MatchBatch]:
        return [e for e in self.events if e.board == "ranked"]

    @property
    def operator_counts(self) -> list[tuple[str, int]]:
        """Operators from real match history: how many of these matches each one was played in."""
        counts: dict[str, int] = defaultdict(int)
        for e in self.events:
            for op in e.operators:
                counts[op] += 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def maps(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = defaultdict(int)
        for e in self.events:
            if e.map_name:
                counts[e.map_name] += 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def rp_change(self) -> int:
        return sum(e.rp_change for e in self.ranked_events)

    @property
    def rank_before(self) -> int | None:
        r = self.ranked_events
        return r[0].rank_before if r else None

    @property
    def rank_after(self) -> int | None:
        r = self.ranked_events
        return r[-1].rank_after if r else None


def group_sessions(events: list[MatchBatch], gap: timedelta) -> list[list[MatchBatch]]:
    groups: list[list[MatchBatch]] = []
    for e in events:
        if groups and e.detected_at - groups[-1][-1].detected_at <= gap:
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


@dataclass
class DayStats:
    day: date
    matches: int = 0
    wins: int = 0
    losses: int = 0
    abandons: int = 0
    kills: int = 0
    deaths: int = 0

    @property
    def kd(self) -> float:
        return ratio(self.kills, self.deaths)


# ------------------------------------------------------------------ tracker


@dataclass
class PollResult:
    player: TrackedPlayer
    snapshot: Snapshot | None = None
    previous: Snapshot | None = None
    batches: list[MatchBatch] = field(default_factory=list)  # new matches, to announce
    backfilled: list[MatchBatch] = field(default_factory=list)  # older matches saved as history, not announced
    real_matches: bool = False  # batches are real matches from match history (one per match)
    operator_deltas: list[OperatorDelta] = field(default_factory=list)
    error: str | None = None

    @property
    def first_poll(self) -> bool:
        return self.snapshot is not None and self.previous is None

    @property
    def has_news(self) -> bool:
        # With real matches, operators come with each match; no separate operator news.
        return bool(self.batches or (self.operator_deltas and not self.real_matches))


class Tracker:
    def __init__(
        self,
        storage: Storage,
        fetcher: StatsFetcher | None,  # None is fine for read-only use (summaries, history)
        *,
        session_gap: timedelta = timedelta(minutes=45),
        operator_refresh: timedelta = timedelta(minutes=60),
        operator_catchup: timedelta = timedelta(minutes=30),
    ):
        self.storage = storage
        self.fetcher = fetcher
        self.session_gap = session_gap
        self.operator_refresh = operator_refresh
        # After new matches, keep re-fetching operators for a while: the operator API can lag behind.
        self.operator_catchup = operator_catchup
        self._catchup_until: dict[int, datetime] = {}
        self._ops_attempted_at: dict[int, datetime] = {}
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def add_player(self, name: str, platform: str = "uplay") -> tuple[TrackedPlayer, PollResult]:
        assert self.fetcher, "Tracker was created without a fetcher"
        info = await self.fetcher.find_player(name, platform)
        tp = self.storage.add_player(info)
        return tp, await self.poll(tp)  # first poll = baseline the next matches are compared to

    async def poll_all(self) -> list[PollResult]:
        results = []
        for tp in self.storage.list_players():
            try:
                results.append(await self.poll(tp))
            except Exception as e:  # one broken player shouldn't stop the others
                log.exception("Polling %s failed", tp.name)
                results.append(PollResult(player=tp, error=f"{type(e).__name__}: {e}"))
        return results

    async def poll(self, tp: TrackedPlayer, force_operators: bool = False) -> PollResult:
        async with self._locks[tp.id]:
            return await self._poll(tp, force_operators)

    async def _poll(self, tp: TrackedPlayer, force_operators: bool) -> PollResult:
        assert self.fetcher, "Tracker was created without a fetcher"
        now = utcnow()
        prev = self.storage.latest_snapshot(tp.id)
        result = PollResult(player=tp, previous=prev)
        try:
            # A copy, so a rename spotted by the fetcher shows up as a difference below.
            snap = await self.fetcher.profile(replace(tp.info))
        except SourceError as e:
            log.warning("Couldn't fetch %s: %s", tp.name, e)
            self.storage.mark_checked(tp.id, error=str(e), when=now)
            result.error = str(e)
            return result
        snap.taken_at = now
        result.snapshot = snap

        if prev:
            # A playlist missing from one response shouldn't make its whole season look new next time.
            season = snap.season_id
            for board_id, b in prev.boards.items():
                if board_id not in snap.boards and b.season_id >= season:
                    snap.boards[board_id] = b

        if self.fetcher.has_matches and await self._fetch_new_matches(tp, snap, result):
            result.real_matches = True
        elif prev:
            result.batches = diff_boards(prev, snap)

        if self.fetcher.has_operators:
            result.operator_deltas = await self._update_operators(tp, prev, snap, now, bool(result.batches),
                                                                  force_operators)

        if (snap.player.name, snap.player.user_id) != (tp.info.name, tp.info.user_id):
            self.storage.update_player_info(tp.id, snap.player)
        self.storage.save_snapshot(tp.id, snap)
        for b in result.backfilled + result.batches:
            self.storage.add_match_batch(tp.id, b)
        if result.backfilled and not result.batches:
            # History we just learned about isn't news: don't announce it as a finished session either.
            self.storage.set_setting(f"session_announced:{tp.id}", str(max(b.id or 0 for b in result.backfilled)))
        if result.operator_deltas:
            self.storage.add_operator_deltas(tp.id, result.operator_deltas, now, snap.operator_season)
        self.storage.mark_checked(tp.id, error="; ".join(snap.errors.values()) or None, when=now)
        return result

    async def _fetch_new_matches(self, tp: TrackedPlayer, snap: Snapshot, result: PollResult) -> bool:
        """Read match history and keep the matches we haven't stored yet. False if there's no history."""
        assert self.fetcher
        baseline = not self.storage.has_real_matches(tp.id)  # first look: save as history, don't announce
        found: dict[str, MatchBatch] = {}
        cursor = None
        for page_no in range(1 if baseline else MAX_MATCH_PAGES):
            try:
                page = await self.fetcher.matches(snap.player, cursor)
            except SourceError as e:
                # Nothing is lost: matches are recognised by id, so the next poll picks them up.
                log.warning("Couldn't fetch matches for %s: %s", tp.name, e)
                snap.errors["matches"] = str(e)
                return True
            if page is None:  # the source has no match history after all
                return page_no > 0
            matches, cursor = page
            known = self.storage.known_match_ids(tp.id, [m.match_id or "" for m in matches])
            fresh = [m for m in matches if m.match_id and m.match_id not in known]
            for m in fresh:
                found.setdefault(m.match_id or "", m)
            if len(fresh) < len(matches) or not matches or cursor is None:
                break
        else:
            if not baseline:
                log.warning("%s played more than %d pages of matches since the last check; older ones skipped",
                            tp.name, MAX_MATCH_PAGES)
        new = sorted(found.values(), key=lambda m: m.detected_at)
        for m in new:
            m.season_id = m.season_id or snap.season_id
        if baseline:
            result.backfilled = new
        else:
            result.batches = new
        return True

    async def _update_operators(
        self, tp: TrackedPlayer, prev: Snapshot | None, snap: Snapshot, now: datetime, new_matches: bool,
        force: bool,
    ) -> list[OperatorDelta]:
        season = season_code(snap.season_id) or "all"
        last_try = self._ops_attempted_at.get(tp.id) or (prev.operators_fetched_at if prev else None)
        if new_matches:
            self._catchup_until[tp.id] = now + self.operator_catchup
        due = (
            force
            or last_try is None
            or bool(prev and prev.operator_season and prev.operator_season != season)  # new season
            or now < self._catchup_until.get(tp.id, now)
            or now - last_try >= self.operator_refresh
        )
        if not due:
            _carry_operators(prev, snap)
            return []
        self._ops_attempted_at[tp.id] = now  # failures wait for the next refresh too (e.g. a bad API key)
        try:
            ops = await self.fetcher.operators(snap.player, season, OPERATOR_MODE)
        except SourceError as e:
            log.warning("Couldn't fetch operators for %s: %s", tp.name, e)
            snap.errors["operators"] = str(e)
            _carry_operators(prev, snap)
            return []
        snap.operators, snap.operator_season, snap.operator_mode = ops, season, OPERATOR_MODE
        snap.operators_fetched_at = now

        if prev is None or prev.operators_fetched_at is None or prev.operator_mode != OPERATOR_MODE:
            return []
        if prev.operator_season == season:
            base = prev.operators
        elif prev.season_id and snap.season_id > prev.season_id:
            base = []  # new season: operator numbers start from zero
        else:
            return []
        deltas = diff_operators(base, ops)
        if deltas:
            self._catchup_until.pop(tp.id, None)
        return deltas

    # ------------------------------------------------------------- queries

    def summary(self, tp: TrackedPlayer, since: datetime | None = None, until: datetime | None = None) -> PlaySummary:
        events = self.storage.match_events(tp.id, since=since, until=until)
        return self._with_operators(tp, events)

    def _with_operators(self, tp: TrackedPlayer, events: list[MatchBatch]) -> PlaySummary:
        s = PlaySummary(events)
        if events and s.start and s.end and not any(e.match_id for e in events):
            # Operator numbers can arrive a little after the matches themselves.
            ops = self.storage.operator_events(tp.id, since=s.start, until=s.end + self.operator_catchup)
            s.operators = sum_operator_deltas([o.delta for o in ops])
        return s

    def today(self, tp: TrackedPlayer, tz: tzinfo) -> PlaySummary:
        midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.summary(tp, since=midnight)

    def last_session(self, tp: TrackedPlayer, lookback: timedelta = timedelta(days=30)) -> tuple[PlaySummary, bool]:
        """The most recent play session, and whether it's still going."""
        events = self.storage.match_events(tp.id, since=utcnow() - lookback)
        groups = group_sessions(events, self.session_gap)
        if not groups:
            return PlaySummary([]), False
        session = self._with_operators(tp, groups[-1])
        ongoing = session.end is not None and utcnow() - session.end < self.session_gap
        return session, ongoing

    def finished_session(self, tp: TrackedPlayer, min_matches: int = 2) -> PlaySummary | None:
        """A session that just ended and hasn't been announced yet (each one is returned once)."""
        key = f"session_announced:{tp.id}"
        last_id = int(self.storage.get_setting(key, "0") or 0)
        events = self.storage.match_events(tp.id, after_id=last_id)
        if not events or utcnow() - events[-1].detected_at < self.session_gap:
            return None
        self.storage.set_setting(key, str(events[-1].id))
        session = self._with_operators(tp, group_sessions(events, self.session_gap)[-1])
        return session if session.matches >= min_matches else None

    def daily(self, tp: TrackedPlayer, days: int, tz: tzinfo) -> list[DayStats]:
        today = datetime.now(tz).date()
        first = today - timedelta(days=days - 1)
        since = datetime(first.year, first.month, first.day, tzinfo=tz)
        out = {first + timedelta(days=i): DayStats(first + timedelta(days=i)) for i in range(days)}
        for e in self.storage.match_events(tp.id, since=since):
            d = out.get(e.detected_at.astimezone(tz).date())
            if d is None:
                continue
            for f in ("matches", "wins", "losses", "abandons", "kills", "deaths"):
                setattr(d, f, getattr(d, f) + getattr(e, f))
        return list(out.values())

    def rp_history(self, tp: TrackedPlayer, since: datetime | None = None) -> list[tuple[datetime, int]]:
        return [(e.detected_at, e.rp_after) for e in self.storage.match_events(tp.id, since=since) if e.board == "ranked"]


def _carry_operators(prev: Snapshot | None, snap: Snapshot) -> None:
    """Keep the last known operator numbers on snapshots where we didn't re-fetch them."""
    if prev and prev.operators_fetched_at:
        snap.operators = prev.operators
        snap.operator_season = prev.operator_season
        snap.operator_mode = prev.operator_mode
        snap.operators_fetched_at = prev.operators_fetched_at
