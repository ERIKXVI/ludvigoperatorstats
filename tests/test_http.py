"""The real HTTP clients against the fake R6 Tracker / Arenyze server."""

import asyncio

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from fake_api import API_KEY, ARENYZE_KEY, TRN_PREFIX, USER_ID, FakeApis

from r6tracker.fetcher import StatsFetcher
from r6tracker.models import PlayerInfo
from r6tracker.sources import ArenyzeClient, AuthError, EndpointUnavailable, PlayerNotFound, SourceError, TrackerNetworkClient
from r6tracker.tracker import Tracker


def run_with_server(test):
    """Run `test(apis, make_trn, make_arenyze)` with a live fake server."""

    async def main():
        apis = FakeApis()
        server = TestServer(apis.app())
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        try:
            async with ClientSession() as session:
                def trn(key=API_KEY):
                    return TrackerNetworkClient(key, session, base_url=base + TRN_PREFIX)

                def are(key=ARENYZE_KEY):
                    return ArenyzeClient(key, session, base_url=base + "/r6/api")

                await test(apis, trn, are)
        finally:
            await server.close()

    asyncio.run(main())


def test_full_tracking_flow(storage):
    async def test(apis, trn, are):
        tracker = Tracker(storage, StatsFetcher(trn()))
        tp, first = await tracker.add_player("ludvig")
        assert tp.name == "Ludvig" and tp.info.user_id == USER_ID  # exact name + Ubisoft id from R6 Tracker
        snap = first.snapshot
        assert snap.level == 287 and snap.ranked.rank_points == 3839 and snap.sources == ["R6 Tracker"]
        assert snap.operator_season == "Y11S3" and len(snap.operators) == 4
        assert len(first.backfilled) == 6 and not first.has_news  # recent history saved, not announced
        assert any("season=43" in r for r in apis.requests)  # operators asked for the current season

        match_id = apis.play_match(won=True, kills=9, deaths=4, rp=27, map_name="Clubhouse", operators=("Ash", "Jäger"))
        res = await tracker.poll(tp)
        [m] = res.batches
        assert (m.match_id, m.result_text, m.map_name, m.kills, m.deaths, m.rp_change) == (
            match_id, "WIN", "Clubhouse", 9, 4, 27
        )
        assert m.operators == ["Ash", "Jäger"] and m.rounds_won == 4
        assert res.snapshot.ranked.wins == 80  # the profile moved too, but no double count:
        assert storage.count_tracked_matches(tp.id) == 7
        assert not (await tracker.poll(tp)).batches

    run_with_server(test)


def test_errors_are_explained():
    async def test(apis, trn, are):
        player = PlayerInfo("Ludvig")
        with pytest.raises(AuthError, match="rejected TRN_API_KEY"):
            await trn(key="wrong").profile(player)
        apis.r6_forbidden = True
        with pytest.raises(AuthError, match="isn't allowed to read Rainbow Six Siege"):
            await trn().profile(player)
        apis.r6_forbidden = False
        apis.cloudflare_block = True
        with pytest.raises(SourceError, match="web page instead of data"):
            await trn().profile(player)
        apis.cloudflare_block = False
        with pytest.raises(PlayerNotFound):
            await trn().find_player("nobody")
        apis.no_match_history = True
        with pytest.raises(EndpointUnavailable):
            await trn().matches(player)

    run_with_server(test)


def test_renamed_player_is_found_by_ubisoft_id(storage):
    async def test(apis, trn, are):
        tracker = Tracker(storage, StatsFetcher(trn()))
        tp, _ = await tracker.add_player("Ludvig")
        apis.rename("LudvigTheGreat")
        res = await tracker.poll(tp)
        assert res.error is None and res.snapshot.player.name == "LudvigTheGreat"
        assert storage.get_player(tp.id).name == "LudvigTheGreat"
        assert any(USER_ID in r for r in apis.requests)

    run_with_server(test)


def test_no_match_history_falls_back_to_totals(storage):
    async def test(apis, trn, are):
        from conftest import back_in_time

        apis.no_match_history = True
        tracker = Tracker(storage, StatsFetcher(trn()))
        tp, first = await tracker.add_player("Ludvig")
        assert not first.real_matches and tp.id in tracker._no_history_until  # retried later, per player
        back_in_time(storage, tp.id, 30)
        apis.play_match(won=False, kills=2, deaths=6, rp=-20)
        res = await tracker.poll(tp)
        assert [(b.board, b.result_text, b.kills, b.deaths, b.rp_change, b.match_id) for b in res.batches] == [
            ("ranked", "LOSS", 2, 6, -20, None)
        ]

    run_with_server(test)


def test_arenyze_backup_source():
    async def test(apis, trn, are):
        fetcher = StatsFetcher(None, are())
        assert not fetcher.has_matches
        player = await fetcher.find_player("Ludvig")
        snap = await fetcher.profile(player)
        assert snap.sources == ["Arenyze"] and snap.level == 287 and snap.banned is False
        assert snap.ranked.wins == 34 and snap.ranked.season_id == 43
        assert {o.name for o in await fetcher.operators(player, "Y11S3")} >= {"Ash", "Jäger"}
        with pytest.raises(AuthError, match="ARENYZE_API_KEY"):
            await are(key="nope").operators(player)

    run_with_server(test)


def test_trn_wins_when_both_keys_are_set():
    async def test(apis, trn, are):
        fetcher = StatsFetcher(trn(), are())
        assert fetcher.arenyze is None and fetcher.source_name == "R6 Tracker"

    run_with_server(test)


def test_reused_name_does_not_switch_accounts(storage):
    async def test(apis, trn, are):
        tracker = Tracker(storage, StatsFetcher(trn()))
        tp, _ = await tracker.add_player("Ludvig")
        apis.give_name_to("99999999-9999-9999-9999-999999999999")
        res = await tracker.poll(tp)
        assert "different Ubisoft account" in (res.error or "") and not res.batches
        assert storage.get_player(tp.id).info.user_id == USER_ID  # still the original account

    run_with_server(test)


def test_redirects_are_not_followed_with_the_key():
    async def test(apis, trn, are):
        apis.redirect = True
        with pytest.raises(SourceError, match="redirected"):
            await trn().profile(PlayerInfo("Ludvig"))
        assert apis.leaked == []

    run_with_server(test)


def test_invalid_season_is_an_error_not_lifetime_stats():
    async def test(apis, trn, are):
        with pytest.raises(SourceError, match="Unknown season 'Y11S9'"):
            await trn().operators(PlayerInfo("Ludvig"), "Y11S9")
        assert not any("segments/operator" in r for r in apis.requests)

    run_with_server(test)


def test_key_without_r6_access_is_explained():
    async def test(apis, trn, are):
        apis.invalid_key = True  # what a valid key without R6 access gets, too
        with pytest.raises(AuthError, match="isn't allowed to read R6 data"):
            await trn().profile(PlayerInfo("Ludvig"))

    run_with_server(test)


def test_missing_later_page_keeps_the_matches_already_read(storage):
    async def test(apis, trn, are):
        fetcher = StatsFetcher(trn())
        tracker = Tracker(storage, fetcher)
        tp, _ = await tracker.add_player("Ludvig")
        ids = [apis.play_match(won=n % 2 == 0, kills=5, deaths=5, rp=10) for n in range(22)]  # bot was offline
        apis.missing_page = 1  # R6 Tracker 404s on the second page
        res = await tracker.poll(tp)
        assert len(res.batches) == 20 and res.real_matches and fetcher.has_matches
        assert {b.match_id for b in res.batches} == set(ids[2:])  # the newest 20, kept and posted

    run_with_server(test)
