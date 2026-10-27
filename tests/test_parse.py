from conftest import load

from r6tracker import operators
from r6tracker.models import Snapshot, PlayerInfo, season_code
from r6tracker.ranks import rank_name
from r6tracker.sources.parse import parse_full_profiles, parse_operator_list, parse_profile_stats


def test_ubisoft_full_profiles():
    boards = parse_full_profiles(load("ubisoft_full_profiles.json"))
    assert set(boards) == {"ranked", "standard", "dual_front"}  # empty warmup skipped, living_game_mode renamed
    r = boards["ranked"]
    assert (r.season_id, r.kills, r.deaths, r.wins, r.losses, r.abandons) == (43, 468, 410, 34, 30, 1)
    assert (r.rank, r.rank_points, r.max_rank, r.max_rank_points) == (27, 3755, 28, 3812)
    assert r.matches == 65
    assert r.kd == 1.14
    assert r.win_rate == 52.3
    assert rank_name(r.rank) == "Emerald IV"


def test_arenyze_profile_picks_newest_season_and_reads_profile_fallbacks():
    boards = parse_full_profiles(load("arenyze_profile.json")["stats"])
    r = boards["ranked"]
    # Season 43 entry has no season_statistics; numbers come from `profile`.
    assert (r.season_id, r.kills, r.deaths, r.wins, r.losses, r.abandons, r.rank) == (43, 468, 410, 34, 30, 1, 27)


def test_profile_stats():
    s = parse_profile_stats(load("ubisoft_profile_stats.json"))
    assert s["level"] == 287
    assert s["playtime_seconds"] == 3_600_000
    assert s["battle_pass_level"] == 37
    assert s["commendations"] == {"total": 42, "teamwork": 20, "leadership": 22}


def test_operator_list():
    ops = {o.name: o for o in parse_operator_list(load("arenyze_operators.json"))}
    assert set(ops) == {"Ash", "Jäger", "Nøkk", "Brandnew"}
    ash = ops["Ash"]
    assert ash.side == "attacker"
    assert (ash.rounds_played, ash.rounds_won, ash.kills, ash.deaths, ash.headshots) == (120, 66, 104, 80, 50)
    assert ash.multi_kills == {"1k": 40, "2k": 20, "3k": 6, "4k": 1, "5k": 1}
    assert ash.kd == 1.3 and ash.win_rate == 55.0 and ash.headshot_pct == 48.1
    assert ops["Nøkk"].side == "attacker"  # side missing in API -> from operator metadata
    assert ops["Brandnew"].side == "attacker"  # unknown operator keeps API side


def test_operator_list_tolerates_junk():
    assert parse_operator_list(None) == []
    assert parse_operator_list({"operators": None}) == []
    assert parse_operator_list([{"nope": 1}, "x"]) == []


def test_operator_lookup_and_icons():
    for spelling in ("Jäger", "jager", "JAGER"):
        assert operators.find(spelling).id == "jager"
    assert operators.find("Nøkk").id == "nokk"
    assert operators.find("Capitão").id == "capitao"
    assert operators.find("Skopós").id == "skopos"
    assert operators.find("nobody") is None
    assert operators.icon_path("Ash").name == "ash.png"
    assert operators.icon_url("Tubarão") == "https://r6operators.marcopixel.eu/icons/png/tubarao.png"
    assert all(o.icon_path for o in operators.all_operators())


def test_season_code():
    assert season_code(1) == "Y1S1"
    assert season_code(43) == "Y11S3"
    assert season_code(44) == "Y11S4"
    assert season_code(0) is None


def test_snapshot_roundtrip():
    snap = Snapshot(player=PlayerInfo(name="Ludvig", user_id="u"), level=5)
    snap.boards = parse_full_profiles(load("ubisoft_full_profiles.json"))
    snap.operators = parse_operator_list(load("arenyze_operators.json"))
    back = Snapshot.from_dict(snap.to_dict())
    assert back == snap
    assert back.total.matches == 65 + 20 + 2
