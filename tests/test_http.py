"""The real HTTP clients against a fake Ubisoft/Arenyze server."""

import asyncio

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from conftest import back_in_time
from fake_api import API_KEY, EMAIL, PASSWORD, USER_ID, FakeApis

from r6tracker.fetcher import StatsFetcher
from r6tracker.models import PlayerInfo
from r6tracker.sources import ArenyzeClient, AuthError, PlayerNotFound, UbisoftClient
from r6tracker.tracker import Tracker


def run_with_server(test):
    """Run `test(apis, make_ubisoft, make_arenyze)` with a live fake server."""

    async def main():
        apis = FakeApis()
        server = TestServer(apis.app())
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        try:
            async with ClientSession() as session:
                def ubi(password=PASSWORD, cache=None):
                    return UbisoftClient(EMAIL, password, session, cache_path=cache, base_url=base)

                def are(key=API_KEY):
                    return ArenyzeClient(key, session, base_url=base + "/r6/api")

                await test(apis, ubi, are)
        finally:
            await server.close()

    asyncio.run(main())


def test_full_tracking_flow(storage, tmp_path):
    async def test(apis, ubi, are):
        tracker = Tracker(storage, StatsFetcher(ubi(cache=tmp_path / "ubi.json"), are()))
        tp, first = await tracker.add_player("ludvig")
        assert tp.name == "Ludvig" and tp.info.user_id == USER_ID  # exact-case name from Ubisoft, PSN result ignored
        assert apis.logins == ["base", "advanced"]
        snap = first.snapshot
        assert snap.level == 287 and snap.battle_pass_level == 37 and snap.ranked.rank_points == 3755
        assert snap.operator_season == "Y11S3" and len(snap.operators) == 4

        back_in_time(storage, tp.id, 20)
        apis.play_ranked_win(kills=9, deaths=4, rp=27)
        apis.play_operator_rounds("Ash", rounds=5, won=4, kills=6, deaths=2)
        res = await tracker.poll(tp)
        assert [(b.board, b.result_text, b.kills, b.deaths, b.rp_change) for b in res.batches] == [
            ("ranked", "WIN", 9, 4, 27)
        ]
        assert [(d.name, d.rounds, d.rounds_won, d.kills) for d in res.operator_deltas] == [("Ash", 5, 4, 6)]
        assert apis.logins == ["base", "advanced"]  # tickets were reused

    run_with_server(test)


def test_ticket_cache_survives_restart(tmp_path):
    async def test(apis, ubi, are):
        player = PlayerInfo("Ludvig", user_id=USER_ID)
        await ubi(cache=tmp_path / "ubi.json").full_profiles(player)
        assert apis.logins == ["base", "advanced"]
        assert "hunter2" not in (tmp_path / "ubi.json").read_text()  # never store the password
        await ubi(cache=tmp_path / "ubi.json").full_profiles(player)  # "restarted" bot
        assert apis.logins == ["base", "advanced"]

    run_with_server(test)


def test_expired_session_logs_in_again():
    async def test(apis, ubi, are):
        client = ubi()
        player = await client.find_player("Ludvig")
        apis.expire_next_request = True
        boards = await client.full_profiles(player)
        assert boards["ranked"].wins == 34
        # The stale advanced ticket was dropped; logging in again found the base ticket stale too.
        assert apis.logins == ["base", "advanced", "base", "advanced"]

    run_with_server(test)


def test_bad_password_and_cooldown():
    async def test(apis, ubi, are):
        client = ubi(password="wrong")
        with pytest.raises(AuthError, match="UBISOFT_EMAIL"):
            await client.find_player("Ludvig")
        with pytest.raises(AuthError):
            await client.find_player("Ludvig")
        assert apis.logins == []  # second attempt didn't even hit the login endpoint

    run_with_server(test)


def test_two_factor_account():
    async def test(apis, ubi, are):
        apis.two_factor = True
        with pytest.raises(AuthError, match="2-step"):
            await ubi().find_player("Ludvig")

    run_with_server(test)


def test_unknown_player():
    async def test(apis, ubi, are):
        with pytest.raises(PlayerNotFound):
            await ubi().find_player("nobody")
        with pytest.raises(PlayerNotFound):
            await are().operators(PlayerInfo("nobody"))

    run_with_server(test)


def test_arenyze_bad_key():
    async def test(apis, ubi, are):
        with pytest.raises(AuthError, match="ARENYZE_API_KEY"):
            await are(key="nope").operators(PlayerInfo("Ludvig"))

    run_with_server(test)


def test_arenyze_only_setup():
    async def test(apis, ubi, are):
        fetcher = StatsFetcher(None, are())
        player = await fetcher.find_player("Ludvig")
        snap = await fetcher.profile(player)
        assert snap.sources == ["arenyze"] and snap.level == 287 and snap.banned is False
        assert snap.ranked.wins == 34 and snap.ranked.season_id == 43
        ops = await fetcher.operators(player, "Y11S3")
        assert {o.name for o in ops} >= {"Ash", "Jäger"}

    run_with_server(test)
