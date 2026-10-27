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
HISTORY_RETRY = timedelta(minutes=30)  # a player without match history gets checked again after this
HISTORY_FAILOVER = timedelta(minutes=30)  # history failing this long for a player -> count from the totals


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
        return sum(e.rp_change for e in self.ranked_events if e.rp_known)

    @property
    def rank_before(self) -> int | None:
        r = [e for e in self.ranked_events if e.rank_known]
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
        self._no_history_until: dict[int, datetime] = {}  # players whose match history isn't available
        self._history_failing_since: dict[int, datetime] = {}
        self._history_ref: dict[int, Snapshot] = {}  # snapshot from the last poll where history worked
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

        # Totals are only comparable with a snapshot from the same source (playlist names differ).
        comparable = prev if prev is not None and prev.sources == snap.sources else None
        if comparable:
            # A playlist missing from one response shouldn't make its whole season look new next time.
            season = snap.season_id
            for board_id, b in comparable.boards.items():
                if board_id not in snap.boards and b.season_id >= season:
                    snap.boards[board_id] = b
        totals = diff_boards(comparable, snap) if comparable else []

        use_history = self.fetcher.has_matches and now >= self._no_history_until.get(tp.id, now)
        fallback = await self._fetch_new_matches(tp, snap, result, comparable, totals) if use_history else totals
        if fallback is None:
            result.real_matches = True
        else:
            result.batches = fallback

        if self.fetcher.has_operators:
            result.operator_deltas = await self._update_operators(tp, prev, snap, now, bool(result.batches),
                                                                  force_operators)

        if (snap.player.name, snap.player.user_id) != (tp.info.name, tp.info.user_id):
            self.storage.update_player_info(tp.id, snap.player)
        self.storage.save_snapshot(tp.id, snap)
        for b in result.backfilled + result.batches:
            self.storage.add_match_batch(tp.id, b)
        if result.operator_deltas:
            self.storage.add_operator_deltas(tp.id, result.operator_deltas, now, snap.operator_season)
        self.storage.mark_checked(tp.id, error="; ".join(snap.errors.values()) or None, when=now)
        return result

    async def _fetch_new_matches(
        self, tp: TrackedPlayer, snap: Snapshot, result: PollResult, prev: Snapshot | None, totals: list[MatchBatch]
    ) -> list[MatchBatch] | None:
        """Read match history and keep the matches we haven't stored yet (into `result`).

        Returns None when match history handled this poll, or the totals-based matches to use instead.
        `prev` is the previous snapshot if it's comparable (same source), `totals` what it says changed.
        """
        assert self.fetcher
        baseline_key = f"match_baseline_at:{tp.id}"
        baseline_at = _when(self.storage.get_setting(baseline_key))
        had_history = self.storage.has_real_matches(tp.id)
        established = had_history or baseline_at is not None
        if baseline_at is None and not had_history and prev is not None:
            baseline_at = prev.taken_at  # already being tracked (from the totals): what ends from now on is news
        first_look = baseline_at is None and not had_history
        # Matches up to the newest one counted from the totals (older version, or a fallback) are counted already.
        counted_until = self.storage.last_totals_event_at(tp.id)
        found: dict[str, MatchBatch] = {}
        listed_any = False
        cursor = None
        for page_no in range(1 if first_look else MAX_MATCH_PAGES):
            try:
                page = await self.fetcher.matches(snap.player, cursor)
            except SourceError as e:  # incl. EndpointUnavailable on the first page
                snap.errors["matches"] = str(e)
                if page_no > 0:
                    # Catch up in one go on the next poll rather than leaving a gap behind.
                    log.warning("Couldn't read all of %s's new matches (%s); trying again next time.", tp.name, e)
                    return None
                return self._history_failed(tp, snap, totals, e, established)
            if page is None:  # the source has no match history
                return totals
            matches, cursor = page
            listed_any = listed_any or bool(matches)
            known = self.storage.known_match_ids(tp.id, [m.match_id or "" for m in matches])
            fresh = [
                m for m in matches
                if m.match_id and m.match_id not in known and not (counted_until and m.detected_at <= counted_until)
            ]
            for m in fresh:
                found.setdefault(m.match_id or "", m)
            if len(fresh) < len(matches) or not matches or cursor is None:
                break
        else:
            if not first_look:
                log.warning("%s played more than %d pages of matches since the last check; older ones skipped",
                            tp.name, MAX_MATCH_PAGES)
        self._history_failing_since.pop(tp.id, None)
        self._history_ref[tp.id] = snap

        new = sorted(found.values(), key=lambda m: m.detected_at)
        if first_look or not self.storage.get_setting(baseline_key):
            # From now on every match that ends is news, even if this first look was empty.
            self.storage.set_setting(baseline_key, (baseline_at or snap.taken_at).isoformat())
        if first_look:
            result.backfilled = new  # season unknown for these: season_id stays 0
            return None
        if not listed_any and totals:
            log.warning("R6 Tracker lists no matches for %s but the totals moved; counting from the totals.", tp.name)
            return totals
        # R6 Tracker can list a match late; one that ended before we started watching is history, not news.
        result.backfilled = [m for m in new if baseline_at and m.detected_at < baseline_at]
        result.batches = [m for m in new if not (baseline_at and m.detected_at < baseline_at)]
        same_season = prev is not None and prev.season_id == snap.season_id
        for m in result.batches:
            if same_season and prev and m.detected_at >= prev.taken_at:
                m.season_id = m.season_id or snap.season_id  # otherwise unknown (could span a new season)
        return None

    def _history_failed(
        self, tp: TrackedPlayer, snap: Snapshot, totals: list[MatchBatch], error: Exception, established: bool
    ) -> list[MatchBatch] | None:
        """The first page of match history couldn't be read. Returns totals-based matches to use, or None."""
        now = snap.taken_at
        if not established:
            # No match history for this player (yet): count from the totals and look again later.
            log.warning("No match history for %s (%s); using the totals, checking again later.", tp.name, error)
            self._no_history_until[tp.id] = now + HISTORY_RETRY
            return totals
        since = self._history_failing_since.setdefault(tp.id, now)
        if now - since < HISTORY_FAILOVER:
            return None  # a hiccup: the matches are picked up by id once history answers again
        log.warning("Match history for %s has failed since %s (%s); counting from the totals for now.",
                    tp.name, since.isoformat(timespec="minutes"), error)
        self._no_history_until[tp.id] = now + HISTORY_RETRY
        del self._history_failing_since[tp.id]
        ref = self._history_ref.pop(tp.id, None)
        if ref is not None and ref.sources == snap.sources:
            return diff_boards(ref, snap)  # everything since history last worked, nothing lost in between
        return totals

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
        """The latest session if it just ended and wasn't announced yet (each one is returned once).

        Sessions that ended before tracking began aren't announced; one that was going on when
        tracking began is, in full (including matches saved as history).
        """
        key = f"session_announced_at:{tp.id}"
        current = self.storage.get_player(tp.id) or tp
        announced_until = _when(self.storage.get_setting(key)) or self._legacy_announced(tp.id) or current.added_at
        events = self.storage.match_events(tp.id, since=announced_until - timedelta(hours=12))
        groups = group_sessions(events, self.session_gap)
        if not groups:
            return None
        end = groups[-1][-1].detected_at
        if end <= announced_until or utcnow() - end < self.session_gap:
            return None
        self.storage.set_setting(key, end.isoformat())
        session = self._with_operators(tp, groups[-1])
        return session if session.matches >= min_matches else None

    def _legacy_announced(self, player_id: int) -> datetime | None:
        """Earlier versions remembered the last announced session by event id; carry that over."""
        legacy = self.storage.get_setting(f"session_announced:{player_id}")
        if not legacy:
            return None
        events = [e for e in self.storage.match_events(player_id) if e.id is not None and e.id <= int(legacy)]
        return max((e.detected_at for e in events), default=None)

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


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _carry_operators(prev: Snapshot | None, snap: Snapshot) -> None:
    """Keep the last known operator numbers on snapshots where we didn't re-fetch them."""
    if prev and prev.operators_fetched_at:
        snap.operators = prev.operators
        snap.operator_season = prev.operator_season
        snap.operator_mode = prev.operator_mode
        snap.operators_fetched_at = prev.operators_fetched_at
