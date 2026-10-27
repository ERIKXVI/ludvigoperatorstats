"""The terminal program end to end, against the fake API server running in a background thread."""

import asyncio
import threading

import pytest
from aiohttp import web
from fake_api import API_KEY, TRN_PREFIX, FakeApis

from r6tracker import cli
from r6tracker.fetcher import StatsFetcher
from r6tracker.sources import TrackerNetworkClient


@pytest.fixture
def fake_server():
    apis = FakeApis()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def start():
        runner = web.AppRunner(apis.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        state["runner"] = runner
        state["url"] = f"http://127.0.0.1:{runner.addresses[0][1]}"
        ready.set()

    thread = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()), daemon=True)
    thread.start()
    ready.wait(5)
    yield apis, state["url"]
    asyncio.run_coroutine_threadsafe(state["runner"].cleanup(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


@pytest.fixture
def env(monkeypatch, tmp_path, fake_server):
    apis, url = fake_server
    monkeypatch.setenv("TRN_API_KEY", API_KEY)
    monkeypatch.delenv("ARENYZE_API_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TIMEZONE", "Europe/Stockholm")

    def from_settings(settings, session):
        assert settings.trn_api_key == API_KEY
        return StatsFetcher(TrackerNetworkClient(settings.trn_api_key, session, base_url=url + TRN_PREFIX))

    monkeypatch.setattr(cli.StatsFetcher, "from_settings", staticmethod(from_settings))
    return apis, tmp_path


def run(capsys, *argv) -> str:
    assert cli.main(["--env", "does-not-exist.env", *argv]) == 0
    return capsys.readouterr().out


def test_cli_end_to_end(env, capsys):
    apis, tmp = env
    out = run(capsys, "add", "ludvig")
    assert "Now tracking Ludvig" in out and "Emerald II" in out and "Level 287" in out
    assert "Villa" in run(capsys, "matches")  # the recent match history was saved right away

    apis.play_match(won=True, kills=11, deaths=6, rp=25, map_name="Clubhouse", operators=("Jäger", "Ash"))
    out = run(capsys, "stats")
    assert "Ranked WIN on Clubhouse 4-2 — K/D/A 11/6/" in out and "Jäger, Ash" in out and "+25 RP" in out

    out = run(capsys, "operators", "--chart", str(tmp / "ops.png"))
    assert "Ash" in out and (tmp / "ops.png").stat().st_size > 10_000
    out = run(capsys, "operator", "jager")
    assert "Jäger" in out and "Rounds" in out

    run(capsys, "complain", "the map", "--category", "map", "--details", "Who designed this?")
    run(capsys, "complain", "who lost the round", "--kind", "argument")
    out = run(capsys, "complaints")
    assert "1 complaint, 1 argument" in out and "the map" in out and "The map / spawns" in out
    assert "2× after a win" in out  # both logged within 2 hours of that ranked win

    out = run(capsys, "history", "--days", "7", "--chart", str(tmp / "history.png"))
    assert (tmp / "history.png").stat().st_size > 10_000
    today = run(capsys, "today")
    assert "1 match ·" in today and "Operators: Ash ×1, Jäger ×1" in today and "Maps: Clubhouse ×1" in today
    assert "Clubhouse" in run(capsys, "matches")
    run(capsys, "export", "--out", str(tmp / "export"))
    csv_text = (tmp / "export" / "matches.csv").read_text()
    assert csv_text.count("\n") == 1 + 7 and "Clubhouse" in csv_text and "Jäger|Ash" in csv_text
    assert "Who designed this?" in (tmp / "export" / "complaints.csv").read_text()
    assert "Ludvig" in run(capsys, "players")


def test_cli_reports_config_errors(monkeypatch, tmp_path, capsys):
    for var in ("TRN_API_KEY", "ARENYZE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert cli.main(["--env", "nope.env", "add", "Ludvig"]) == 1
    assert "No stats source configured" in capsys.readouterr().err
