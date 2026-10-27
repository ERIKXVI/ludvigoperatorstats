import asyncio
from datetime import timedelta

from conftest import back_in_time, board

from r6tracker.models import OperatorStats
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
