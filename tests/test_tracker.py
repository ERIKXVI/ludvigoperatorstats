import asyncio
from datetime import datetime, timedelta

from conftest import FakeFetcher, back_in_time, board

from r6tracker.models import OperatorStats, PlayerInfo
from r6tracker.sources import SourceError
from r6tracker.tracker import Tracker, diff_boards


def run(coro):
    return asyncio.run(coro)


def ranked(**kw):
    base = dict(kills=100, deaths=90, wins=10, losses=8, abandons=0, rank=20, rank_points=2900)
    base.update(kw)
    return board("ranked", **base)


def make(storage, fetcher, **kw):
    return Tracker(storage, fetcher, **kw)


def test_first_poll_is_baseline(storage, fetcher):
    fetcher.boards = {"ranked": ranked()}
    tp, first = run(make(storage, fetcher).add_player("Ludvig"))
    assert first.first_poll and not first.batches and not first.operator_deltas
    assert storage.latest_snapshot(tp.id).boards["ranked"].wins == 10


def test_detects_a_won_ranked_match(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)

    fetcher.boards = {"ranked": ranked(kills=109, deaths=95, wins=11, rank=21, rank_points=2928)}
    res = run(tr.poll(tp))
    assert len(res.batches) == 1
    b = res.batches[0]
    assert (b.board, b.matches, b.wins, b.losses, b.kills, b.deaths) == ("ranked", 1, 1, 0, 9, 5)
    assert b.result_text == "WIN" and b.rp_change == 28 and (b.rank_before, b.rank_after) == (20, 21)
    assert storage.match_events(tp.id)[0].kills == 9

    # Same numbers again: nothing new, and no duplicate snapshot row.
    rows = storage.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert not run(tr.poll(tp)).batches
    assert storage.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == rows


def test_new_playlist_and_new_season_count_from_zero(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 60)

    # First quick match of the season shows up as a brand-new board.
    fetcher.boards = {"ranked": ranked(), "standard": board("standard", kills=7, deaths=3, wins=1)}
    res = run(tr.poll(tp))
    assert [(b.board, b.matches, b.kills) for b in res.batches] == [("standard", 1, 7)]
    back_in_time(storage, tp.id, 60)

    # Season rolls over: ranked resets to the new season's (small) numbers.
    fetcher.boards = {"ranked": ranked(season=44, kills=12, deaths=6, wins=1, losses=1, rank=0, rank_points=1000)}
    res = run(tr.poll(tp))
    assert [(b.board, b.season_id, b.matches, b.wins, b.losses) for b in res.batches] == [("ranked", 44, 2, 1, 1)]


def test_ignores_glitches(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 10)

    # Board briefly missing from the API response -> no fake "whole season" batch when it returns.
    fetcher.boards = {}
    assert not run(tr.poll(tp)).batches
    back_in_time(storage, tp.id, 10)
    fetcher.boards = {"ranked": ranked()}
    assert not run(tr.poll(tp)).batches

    # Numbers going backwards, or 200 matches in 10 minutes, are ignored.
    back_in_time(storage, tp.id, 10)
    fetcher.boards = {"ranked": ranked(wins=9)}
    assert not run(tr.poll(tp)).batches
    back_in_time(storage, tp.id, 10)
    fetcher.boards = {"ranked": ranked(wins=250)}
    assert not run(tr.poll(tp)).batches


def test_source_error_keeps_previous_snapshot(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    fetcher.profile_error = SourceError("Ubisoft down")
    res = run(tr.poll(tp))
    assert res.error == "Ubisoft down" and res.snapshot is None
    assert storage.get_player(tp.id).last_error == "Ubisoft down"
    assert storage.latest_snapshot(tp.id).boards["ranked"].wins == 10


def test_operator_deltas_and_catchup(storage, fetcher):
    tr = make(storage, fetcher, operator_refresh=timedelta(hours=1), operator_catchup=timedelta(minutes=30))
    fetcher.boards = {"ranked": ranked()}
    fetcher.ops = [OperatorStats(name="Ash", side="attacker", rounds_played=10, rounds_won=6, kills=9, deaths=7)]
    tp, _ = run(tr.add_player("Ludvig"))
    assert fetcher.operator_calls == 1
    back_in_time(storage, tp.id, 5)

    # Nothing changed and operators were fetched recently -> no operator call.
    run(tr.poll(tp))
    assert fetcher.operator_calls == 1

    # A match finishes but the operator API hasn't caught up yet.
    fetcher.boards = {"ranked": ranked(wins=11, kills=105, deaths=93)}
    res = run(tr.poll(tp))
    assert res.batches and not res.operator_deltas and fetcher.operator_calls == 2

    # Next poll: still catching up, so operators are fetched again, and now they've moved.
    fetcher.ops = [
        OperatorStats(name="Ash", side="attacker", rounds_played=14, rounds_won=9, kills=12, deaths=8),
        OperatorStats(name="Jäger", side="defender", rounds_played=3, rounds_won=2, kills=2, deaths=2),
    ]
    res = run(tr.poll(tp))
    assert fetcher.operator_calls == 3
    assert [(d.name, d.rounds, d.kills, d.deaths) for d in res.operator_deltas] == [
        ("Ash", 4, 3, 1),
        ("Jäger", 3, 2, 2),
    ]
    # Caught up -> back to the slow refresh schedule.
    run(tr.poll(tp))
    assert fetcher.operator_calls == 3
    assert len(storage.operator_events(tp.id)) == 2


def test_operator_failure_carries_last_known(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.ops = [OperatorStats(name="Ash", side="attacker", rounds_played=10)]
    tp, _ = run(tr.add_player("Ludvig"))
    fetcher.operator_error = SourceError("arenyze down")
    fetcher.boards = {"ranked": ranked(wins=11)}
    res = run(tr.poll(tp))
    assert res.batches and res.snapshot.operators[0].rounds_played == 10
    assert "arenyze down" in storage.get_player(tp.id).last_error


def test_sessions_today_and_daily(storage, fetcher):
    from zoneinfo import ZoneInfo

    tr = make(storage, fetcher, session_gap=timedelta(minutes=45))
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))

    wins, losses, kills, rp = 10, 8, 100, 2900
    for won, rp_change in ((1, 25), (0, -20), (1, 25)):  # W, L, W, 15 minutes apart
        back_in_time(storage, tp.id, 15)
        wins, losses, kills, rp = wins + won, losses + (1 - won), kills + 8, rp + rp_change
        fetcher.boards = {"ranked": ranked(wins=wins, losses=losses, kills=kills, deaths=95, rank_points=rp)}
        run(tr.poll(tp))

    session, ongoing = tr.last_session(tp)
    assert ongoing
    assert (session.matches, session.wins, session.losses, session.kills) == (3, 2, 1, 24)
    assert session.rp_change == 30
    assert tr.finished_session(tp) is None  # still going

    back_in_time(storage, tp.id, 60)
    done = tr.finished_session(tp)
    assert done is not None and done.matches == 3
    assert tr.finished_session(tp) is None  # announced only once

    days = tr.daily(tp, 3, ZoneInfo("UTC"))
    assert len(days) == 3 and sum(d.matches for d in days) == 3


def test_diff_boards_window_plausibility():
    from r6tracker.models import PlayerInfo, Snapshot

    a = Snapshot(player=PlayerInfo("x"), boards={"ranked": ranked()})
    b = Snapshot(player=PlayerInfo("x"), boards={"ranked": ranked(wins=40)}, taken_at=a.taken_at + timedelta(hours=10))
    assert diff_boards(a, b)[0].matches == 30  # 30 matches over 10 hours: fine


def test_match_window_starts_at_previous_successful_check(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 24 * 60)  # stats last changed yesterday...
    run(tr.poll(tp))  # ...but we checked (no change) just now
    rows = storage.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    fetcher.boards = {"ranked": ranked(wins=11)}
    [b] = run(tr.poll(tp)).batches
    assert b.detected_at - b.window_start < timedelta(minutes=1)
    assert storage.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == rows + 1


def test_failing_operator_source_backs_off(storage, fetcher):
    tr = make(storage, fetcher, operator_refresh=timedelta(hours=1))
    fetcher.boards = {"ranked": ranked()}
    fetcher.operator_error = SourceError("bad key")
    tp, _ = run(tr.add_player("Ludvig"))
    assert fetcher.operator_calls == 1
    run(tr.poll(tp))
    run(tr.poll(tp))
    assert fetcher.operator_calls == 1  # not retried every poll


# ------------------------------------------------------------ match history


def test_first_look_saves_history_without_announcing(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(n, minutes_ago=60 * 24 + n * 30) for n in range(3)]  # yesterday's session
    tp, first = run(tr.add_player("Ludvig"))
    assert first.real_matches and not first.batches and len(first.backfilled) == 3 and not first.has_news
    assert [e.match_id for e in storage.match_events(tp.id)] == ["m2", "m1", "m0"]  # stored oldest first
    assert tr.finished_session(tp) is None  # old history isn't announced as a "session over" either


def test_new_real_matches_are_detected_once(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=120)]
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 60)  # we started watching an hour ago

    fetcher.history = [real_match(2, minutes_ago=2, won=False), real_match(1, minutes_ago=30)] + fetcher.history
    fetcher.boards = {"ranked": ranked(wins=11, losses=9)}  # totals moved too; must NOT double count
    res = run(tr.poll(tp))
    assert res.real_matches and res.has_news
    assert [(b.match_id, b.result_text, b.map_name) for b in res.batches] == [("m1", "WIN", "Villa"), ("m2", "LOSS", "Villa")]
    assert storage.count_tracked_matches(tp.id) == 3

    again = run(tr.poll(tp))
    assert not again.batches and not again.has_news
    session, ongoing = tr.last_session(tp)
    assert ongoing and session.matches == 2 and session.operator_counts == [("Ash", 2)] and session.maps == [("Villa", 2)]


def test_catching_up_reads_more_pages(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.page_size = 2
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=600)]
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 590)
    fetcher.history = [real_match(n, minutes_ago=600 - n * 30) for n in range(5, 0, -1)] + fetcher.history
    res = run(tr.poll(tp))
    assert [b.match_id for b in res.batches] == ["m1", "m2", "m3", "m4", "m5"]  # 3 pages, oldest first
    assert fetcher.matches_calls == 1 + 3


def test_match_history_errors_dont_fall_back_to_totals(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0)]
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 10)
    fetcher.matches_error = SourceError("R6 Tracker hiccup")
    fetcher.boards = {"ranked": ranked(wins=11)}
    res = run(tr.poll(tp))
    assert res.real_matches and not res.batches  # no guessed match from the totals...
    assert "hiccup" in storage.get_player(tp.id).last_error
    fetcher.matches_error = None
    fetcher.history = [real_match(1, minutes_ago=3)] + fetcher.history
    assert [b.match_id for b in run(tr.poll(tp)).batches] == ["m1"]  # ...the real one shows up next time


def test_without_match_history_totals_are_used(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))  # fetcher.history is None: source has no match history
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(wins=11)}
    res = run(tr.poll(tp))
    assert not res.real_matches and [(b.matches, b.match_id) for b in res.batches] == [(1, None)]


# ------------------------------------------------- review regressions


def test_switching_from_totals_to_match_history_doesnt_double_count(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))  # older version / backup source: no match history
    back_in_time(storage, tp.id, 120)
    fetcher.boards = {"ranked": ranked(wins=13)}
    [diff] = run(tr.poll(tp)).batches  # 3 matches noticed from the totals
    back_in_time(storage, tp.id, 30)
    assert storage.count_tracked_matches(tp.id) == 3

    # Now match history is available and lists those same 3 matches, plus one played since.
    fetcher.history = [real_match(9, minutes_ago=5)] + [real_match(n, minutes_ago=40 + n * 20) for n in range(3)]
    res = run(tr.poll(tp))
    assert res.real_matches and [b.match_id for b in res.batches + res.backfilled] == ["m9"]
    assert storage.count_tracked_matches(tp.id) == 4


def test_empty_first_look_still_counts_as_the_baseline(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []  # nothing on R6 Tracker yet
    tp, _ = run(tr.add_player("Ludvig"))
    fetcher.history = [real_match(1, minutes_ago=0)]
    assert [b.match_id for b in run(tr.poll(tp)).batches] == ["m1"]  # announced, not swallowed


def test_history_that_stays_empty_falls_back_to_totals(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(wins=11)}
    res = run(tr.poll(tp))
    assert not res.real_matches and [(b.matches, b.match_id) for b in res.batches] == [(1, None)]


def test_missing_history_for_one_player_is_per_player_and_retried(storage, fetcher):
    from conftest import real_match

    from r6tracker.sources import EndpointUnavailable

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=200)]
    fetcher.matches_error = EndpointUnavailable("404")
    tp, first = run(tr.add_player("Ludvig"))
    assert not first.real_matches and tp.id in tr._no_history_until
    calls = fetcher.matches_calls
    run(tr.poll(tp))
    assert fetcher.matches_calls == calls  # not asked again until the retry time
    other = storage.add_player(PlayerInfo("Bob", user_id="b"))
    fetcher.matches_error = None
    assert run(tr.poll(other)).real_matches  # other players are unaffected


def test_session_running_when_tracking_starts_is_summarised_in_full(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher, session_gap=timedelta(minutes=45))
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(n, minutes_ago=5 + n * 30) for n in range(3)]  # 3 already played
    tp, first = run(tr.add_player("Ludvig"))
    assert len(first.backfilled) == 3 and all(b.season_id == 0 for b in first.backfilled)  # season unknown
    fetcher.history = [real_match(4, minutes_ago=0), real_match(3, minutes_ago=0)] + fetcher.history
    assert len(run(tr.poll(tp)).batches) == 2
    back_in_time(storage, tp.id, 120)
    done = tr.finished_session(tp)
    assert done is not None and done.matches == 5 == tr.last_session(tp)[0].matches


def test_old_finished_session_isnt_announced(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(n, minutes_ago=600 + n * 20) for n in range(3)]
    tp, _ = run(tr.add_player("Ludvig"))
    assert tr.finished_session(tp) is None


def test_source_change_doesnt_mix_playlists(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked(), "casual": board("casual", wins=40)}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)

    async def profile_from_other_source(player):
        snap = await FakeFetcher.profile(fetcher, player)
        snap.sources = ["Other"]
        return snap

    fetcher.profile = profile_from_other_source
    fetcher.boards = {"ranked": ranked(), "quick_match": board("quick_match", wins=40)}
    res = run(tr.poll(tp))
    assert set(res.snapshot.boards) == {"ranked", "quick_match"} and not res.batches


def test_tracked_count_starts_when_tracking_began(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(n, minutes_ago=100 + n) for n in range(4)]
    tp, _ = run(tr.add_player("Ludvig"))
    tp = storage.get_player(tp.id)
    assert storage.count_tracked_matches(tp.id) == 4 and storage.count_tracked_matches(tp.id, since=tp.added_at) == 0


def test_history_recovering_after_a_fallback_announces_new_matches(storage, fetcher):
    from conftest import real_match

    from r6tracker.sources import EndpointUnavailable

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []
    fetcher.matches_error = EndpointUnavailable("404")
    tp, _ = run(tr.add_player("Ludvig"))  # no history for this player yet: totals for now
    back_in_time(storage, tp.id, 40)
    tr._no_history_until.clear()  # retry time reached
    fetcher.matches_error = None
    fetcher.history = [real_match(1, minutes_ago=0)]
    res = run(tr.poll(tp))
    assert [b.match_id for b in res.batches] == ["m1"] and res.has_news  # not swallowed as a "first look"


def test_lasting_history_outage_falls_back_to_totals_without_losing_matches(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=100)]
    tp, _ = run(tr.add_player("Ludvig"))
    run(tr.poll(tp))  # history works (this is the reference point)
    fetcher.matches_error = SourceError("R6 Tracker down")
    fetcher.boards = {"ranked": ranked(wins=11)}
    assert not run(tr.poll(tp)).batches  # a hiccup at first: wait for history
    outage_for(storage, tp, minutes=31)
    fetcher.boards = {"ranked": ranked(wins=12)}
    res = run(tr.poll(tp))
    assert not res.real_matches and [(b.matches, b.wins) for b in res.batches] == [(2, 2)]  # both, none lost


def outage_for(storage, tp, minutes):
    """Pretend match history has been failing for this player for `minutes`."""
    key = f"history_failing_since_at:{tp.id}"
    since = datetime.fromisoformat(storage.get_setting(key))
    storage.set_setting(key, (since - timedelta(minutes=minutes)).isoformat())


def test_long_outage_over_several_retries_and_a_restart_loses_nothing(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=100)]
    tp, _ = run(tr.add_player("Ludvig"))
    run(tr.poll(tp))
    fetcher.matches_error = SourceError("R6 Tracker down")
    wins = 10
    counted = 0
    for cycle in range(3):
        wins += 1
        fetcher.boards = {"ranked": ranked(wins=wins)}
        counted += sum(b.matches for b in run(tr.poll(tp)).batches)  # hiccup (cycle 0) or failover
        if cycle == 0:
            outage_for(storage, tp, minutes=31)
        tr._no_history_until.clear()  # time to retry history (it still fails)
        if cycle == 1:
            tr = make(storage, fetcher)  # the bot restarts in the middle of the outage
        wins += 1
        fetcher.boards = {"ranked": ranked(wins=wins)}
        counted += sum(b.matches for b in run(tr.poll(tp)).batches)
    assert counted == wins - 10 == 6  # every match during the outage counted exactly once


def test_failure_on_a_later_page_retries_the_whole_catch_up(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.page_size = 2
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = [real_match(0, minutes_ago=300)]
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 290)
    fetcher.history = [real_match(n, minutes_ago=280 - n * 10) for n in range(5, 0, -1)] + fetcher.history
    real_page = fetcher.matches

    async def flaky(player, cursor=None):
        if cursor:
            raise SourceError("rate limited")
        return await real_page(player, cursor)

    fetcher.matches = flaky
    assert not run(tr.poll(tp)).batches  # nothing half-stored
    fetcher.matches = real_page
    assert [b.match_id for b in run(tr.poll(tp)).batches] == ["m1", "m2", "m3", "m4", "m5"]


def test_upgrade_keeps_the_old_session_announcement(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    for wins in (11, 12, 13):
        back_in_time(storage, tp.id, 15)
        fetcher.boards = {"ranked": ranked(wins=wins)}
        run(tr.poll(tp))
    back_in_time(storage, tp.id, 60 * 20)  # that session was yesterday...
    last = storage.match_events(tp.id)[-1]
    storage.set_setting(f"session_announced:{tp.id}", str(last.id))  # ...and an older version announced it
    assert tr.finished_session(tp) is None


def test_new_matches_get_the_season_only_when_its_certain(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)
    fetcher.history = [real_match(1, minutes_ago=0)]
    assert run(tr.poll(tp)).batches[0].season_id == 43
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(season=44, wins=1)}  # a new season started meanwhile
    fetcher.history = [real_match(2, minutes_ago=0)] + fetcher.history
    assert run(tr.poll(tp)).batches[0].season_id == 0  # might belong to either season


def test_previous_rp_comes_from_the_last_snapshot_when_consistent(storage, fetcher):
    from dataclasses import replace as dc_replace

    from conftest import real_match

    def unknown_prev(m):
        return dc_replace(m, rp_before=m.rp_after, rank_before=m.rank_after,
                          extra={"rp_unknown": True, "rank_unknown": True})

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked(rank_points=3000, rank=20)}
    fetcher.history = []
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(rank_points=3025, rank=21, wins=11)}
    fetcher.history = [unknown_prev(real_match(1, minutes_ago=0, rp=25))]  # ends at 3025, previous unknown
    [m] = run(tr.poll(tp)).batches
    assert (m.rp_before, m.rp_change, m.rank_before, m.rp_known) == (3000, 25, 20, True)

    # The snapshot from before it (3025) matches the last ranked match we stored (3025): fills again.
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(rank_points=3005, rank=21, wins=11, losses=9)}
    fetcher.history = [dc_replace(unknown_prev(real_match(2, minutes_ago=0, won=False, rp=-20)), rp_after=3005)] + fetcher.history
    [m2] = run(tr.poll(tp)).batches
    assert (m2.rp_before, m2.rp_change) == (3025, -20)

    # The profile lags behind the match list (still 3025 after m2 ended at 3005): don't guess from it.
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(rank_points=3025, rank=21, wins=11, losses=9)}
    run(tr.poll(tp))
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(rank_points=3030, rank=21, wins=12, losses=9)}
    fetcher.history = [dc_replace(unknown_prev(real_match(3, minutes_ago=0)), rp_after=3030)] + fetcher.history
    [m3] = run(tr.poll(tp)).batches
    assert not m3.rp_known


def test_unknown_previous_rank_isnt_shown_as_unranked_or_zero():
    from conftest import real_match

    from r6tracker.bot import embeds
    from r6tracker.tracker import PlaySummary

    m = real_match(1)
    m.extra = {"rp_unknown": True, "rank_unknown": True}
    s = PlaySummary([m])
    assert s.rank_before is None and not s.rp_known
    e = embeds.summary_embed(PlayerInfo("Ludvig"), s, datetime.now().astimezone().tzinfo, "Today", "")
    ranked_field = next(f.value for f in e.fields if f.name == "Ranked")
    assert "Unranked" not in ranked_field and "±0" not in ranked_field and "RP" not in ranked_field
    no_rank = real_match(2)
    no_rank.extra = {"no_rank": True}
    assert PlaySummary([real_match(3), no_rank]).rank_after == 20  # a match without rank data isn't "Unranked"


def test_session_isnt_announced_twice_when_a_match_is_listed_late(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher, session_gap=timedelta(minutes=45))
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 200)
    fetcher.history = [real_match(2, minutes_ago=100), real_match(1, minutes_ago=130)]
    run(tr.poll(tp))
    assert tr.finished_session(tp).matches == 2
    fetcher.history = [real_match(3, minutes_ago=60)] + fetcher.history  # ended 40 min after m2, listed now
    assert len(run(tr.poll(tp)).batches) == 1
    assert tr.finished_session(tp) is None


def test_legacy_marker_is_migrated_once(storage, fetcher):
    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 30)
    fetcher.boards = {"ranked": ranked(wins=12)}
    run(tr.poll(tp))
    back_in_time(storage, tp.id, 600)
    last = storage.match_events(tp.id)[-1]
    storage.set_setting(f"session_announced:{tp.id}", str(last.id))
    assert tr.finished_session(tp) is None
    assert storage.get_setting(f"session_announced_at:{tp.id}") is not None


def test_late_listed_match_keeps_its_season(storage, fetcher):
    from conftest import real_match

    tr = make(storage, fetcher)
    fetcher.boards = {"ranked": ranked()}
    fetcher.history = []
    tp, _ = run(tr.add_player("Ludvig"))
    back_in_time(storage, tp.id, 60)
    run(tr.poll(tp))
    back_in_time(storage, tp.id, 5)
    fetcher.history = [real_match(1, minutes_ago=8)]  # ended before the previous poll, listed only now
    assert run(tr.poll(tp)).batches[0].season_id == 43
