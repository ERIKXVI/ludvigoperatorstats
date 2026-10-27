"""Parsing. The trn_* fixtures are real R6 Tracker responses with the player's identity replaced,
except trn_operators.json, which is shaped after how three independent clients parse that endpoint."""

from datetime import datetime, timezone

from conftest import load

from r6tracker import operators
from r6tracker.models import PlayerInfo, Snapshot, season_code, season_id_from_code
from r6tracker.ranks import rank_name
from r6tracker.sources.parse import parse_full_profiles, parse_operator_list
from r6tracker.sources.trn_parse import parse_matches, parse_operators, parse_time, snapshot_from_profile

UID = "11111111-2222-3333-4444-555555555555"


def trn_snapshot() -> Snapshot:
    return snapshot_from_profile(load("trn_profile.json"), "uplay")


# ------------------------------------------------------------- R6 Tracker


def test_trn_profile_player_and_lifetime():
    snap = trn_snapshot()
    assert snap.player == PlayerInfo(name="Ludvig", platform="uplay", profile_id=UID, user_id=UID)
    assert (snap.level, snap.battle_pass_level) == (287, 37)
    assert snap.playtime_seconds == 7_316_390 and snap.pvp_seconds == 6_657_913  # TimeSeconds
    assert snap.name_changes == ["OldLudvig"]
    life = snap.lifetime
    assert (life.matches, life.wins, life.losses, life.abandons, life.kills, life.deaths) == (
        5609, 2826, 2606, 177, 20461, 21605
    )
    # % of kills (7637 / 20461), not R6 Tracker's headshot accuracy (48.57%)
    assert snap.extras["headshot_pct"] == 37.32 and snap.extras["assists"] == 3092
    assert snap.playstyles["attacker_supporter"] == 5924 and snap.playstyles["defender_trapper"] == 5577


def test_trn_profile_current_season_boards():
    snap = trn_snapshot()
    assert snap.season_id == 43
    assert set(snap.boards) == {"ranked", "quick_match", "standard", "arcade", "siege_cup", "dual_front"}
    r = snap.boards["ranked"]
    assert (r.label, r.matches, r.wins, r.losses, r.abandons, r.kills, r.deaths) == ("Ranked", 177, 79, 97, 1, 603, 820)
    assert (r.rank, r.rank_points, r.max_rank, r.max_rank_points) == (29, 3839, 31, 4027)
    # R6 Tracker labels these "EMERALD II" / "DIAMOND V" in rankPoints.metadata.name; our id mapping agrees.
    assert (rank_name(r.rank), rank_name(r.max_rank)) == ("Emerald II", "Diamond V")
    assert snap.boards["quick_match"].label == "Quick Match" and snap.boards["standard"].label == "Standard"


def test_trn_profile_prefers_unsplit_region_and_ignores_old_seasons():
    data = load("trn_profile.json")
    meta = data["data"]["metadata"]
    meta["currentSeason"] = 36  # the fixture's season-36 ranked segment is split into emea + ncsa (zeroed)
    r = snapshot_from_profile(data, "uplay").boards["ranked"]
    assert r.season_id == 36 and r.matches > 0  # the populated region, not the zeroed one
    meta["currentSeason"] = None  # missing -> newest season present
    assert snapshot_from_profile(data, "uplay").season_id == 43


def test_trn_matches():
    matches, cursor = parse_matches(load("trn_matches.json"), PlayerInfo("Ludvig", user_id=UID))
    assert cursor == 1 and len(matches) == 6
    m = matches[0]  # newest first
    assert (m.match_id, m.board, m.label, m.map_name, m.result_text) == (
        "00000000-0000-0000-0000-000000000001", "ranked", "Ranked", "Villa", "LOSS"
    )
    assert (m.rounds_won, m.rounds_lost, m.kills, m.deaths, m.assists, m.headshots) == (2, 4, 1, 5, 1, 0)
    assert (m.rp_before, m.rp_after, m.rp_change) == (2382, 2363, -19)
    assert m.duration_seconds == 1574  # 26m 14s, from milliseconds
    assert m.detected_at == datetime(2024, 11, 10, 5, 56, 20, 879000, tzinfo=timezone.utc)  # timestamp = end
    assert (m.detected_at - m.window_start).total_seconds() == 1574
    # Back-to-back matches don't overlap when the timestamp is read as the end.
    assert all(newer.window_start >= older.detected_at for newer, older in zip(matches, matches[1:]))
    assert m.operators == ["Kapkan", "Gridlock", "Ace", "Melusi", "Frost"]
    win = matches[1]
    assert (win.result_text, win.map_name, win.rp_change, win.extra) == (
        "WIN", "Chalet", 41, {"first_bloods": 1, "clutches": 1, "kills_3k": 1}
    )
    assert "Jäger" in matches[2].operators and "Tubarão" in matches[4].operators
    assert (matches[5].rank_before, matches[5].rank_after) == (13, 14)  # ranked up


def test_trn_matches_edge_cases():
    data = load("trn_matches.json")
    ms = data["data"]["matches"]
    ms[0]["metadata"]["isRollback"] = True  # rolled back -> not a match
    ms[1]["segments"][0]["attributes"]["playerId"] = "someone-else"  # still found by name
    del ms[2]["segments"][0]["stats"]["wins"]  # no per-match W/L counters -> use metadata.result
    del ms[2]["segments"][0]["stats"]["losses"]
    del ms[2]["segments"][0]["stats"]["abandons"]
    data["data"]["metadata"] = {}
    matches, cursor = parse_matches(data, PlayerInfo("Ludvig", user_id=UID))
    assert cursor is None and len(matches) == 5
    assert matches[0].map_name == "Chalet" and matches[1].result_text == "WIN"
    assert parse_matches({"data": None}, PlayerInfo("x")) == ([], None)
    assert parse_matches({"data": {"matches": [{"nope": 1}, "x"]}}, PlayerInfo("x")) == ([], None)


def test_trn_operators():
    ops = {o.name: o for o in parse_operators(load("trn_operators.json"))}
    assert set(ops) == {"Ash", "Jäger", "Nøkk", "Brandnew"}
    ash = ops["Ash"]
    assert (ash.side, ash.rounds_played, ash.rounds_won, ash.kills, ash.deaths) == ("attacker", 120, 66, 104, 80)
    assert ash.headshots == 50  # from headshotPct (48.08% of 104)
    assert ash.time_played_ms == 11_400_000 and ash.aces == 1 and ash.multi_kills["5k"] == 1
    assert parse_operators({"data": None}) == [] and parse_operators({"data": {"segments": "x"}}) == []


def test_parse_time():
    assert parse_time("2024-11-10T05:56:20.879+00:00") == datetime(2024, 11, 10, 5, 56, 20, 879000, tzinfo=timezone.utc)
    assert parse_time("2024-11-10T05:56:20Z").tzinfo is not None
    assert parse_time("2024-11-10T05:56:20.1234567+00:00").microsecond == 123456
    assert parse_time("nonsense") is None and parse_time(None) is None


# ---------------------------------------------------- Arenyze (backup source)


def test_full_profiles_format():
    boards = parse_full_profiles(load("full_profiles.json"))
    assert set(boards) == {"ranked", "standard", "dual_front"}  # empty warmup skipped, living_game_mode renamed
    r = boards["ranked"]
    assert (r.season_id, r.kills, r.deaths, r.wins, r.losses, r.abandons) == (43, 468, 410, 34, 30, 1)
    assert (r.rank, r.rank_points) == (27, 3755) and rank_name(r.rank) == "Emerald IV"


def test_arenyze_profile_picks_newest_season():
    r = parse_full_profiles(load("arenyze_profile.json")["stats"])["ranked"]
    assert (r.season_id, r.kills, r.deaths, r.wins, r.losses, r.abandons, r.rank) == (43, 468, 410, 34, 30, 1, 27)


def test_arenyze_operator_list():
    ops = {o.name: o for o in parse_operator_list(load("arenyze_operators.json"))}
    assert set(ops) == {"Ash", "Jäger", "Nøkk", "Brandnew"}
    assert ops["Nøkk"].side == "attacker" and ops["Ash"].multi_kills["5k"] == 1
    assert parse_operator_list(None) == [] and parse_operator_list([{"nope": 1}, "x"]) == []


# ------------------------------------------------------------------- misc


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


def test_season_codes():
    assert (season_code(1), season_code(43), season_code(44), season_code(0)) == ("Y1S1", "Y11S3", "Y11S4", None)
    assert (season_id_from_code("Y11S3"), season_id_from_code("y10s1"), season_id_from_code("all")) == (43, 37, None)


def test_snapshot_roundtrip():
    snap = trn_snapshot()
    snap.operators = parse_operators(load("trn_operators.json"))
    back = Snapshot.from_dict(snap.to_dict())
    assert back == snap
    assert back.total.matches == sum(b.matches for b in snap.boards.values())


def test_null_previous_rp_is_filled_from_the_older_match_or_marked_unknown():
    from r6tracker.formatting import rank_change
    from r6tracker.tracker import PlaySummary

    data = load("trn_matches.json")
    ms = data["data"]["matches"]
    for m in (ms[1], ms[5]):  # a middle match and the oldest one on the page
        st = m["segments"][0]["stats"]
        for key in ("rankPointsPrevious", "rankPointsDelta", "rankPrevious"):
            st[key]["value"] = None
    matches, _ = parse_matches(data, PlayerInfo("Ludvig", user_id=UID))
    # Chalet win: previous RP comes from the next older ranked match (Oregon, ended at 2341).
    assert (matches[1].rp_before, matches[1].rp_after, matches[1].rp_change, matches[1].rp_known) == (2341, 2382, 41, True)
    assert matches[1].rank_before == matches[2].rank_after
    # The oldest match on the page has nothing older to look at: unknown, not "±0".
    oldest = matches[5]
    assert not oldest.rp_known and "RP, now" not in rank_change(oldest) and "±0" not in rank_change(oldest)
    assert PlaySummary([oldest]).rp_change == 0 and PlaySummary(matches[:2]).rp_change == -19 + 41


def test_name_changes_newest_first():
    data = load("trn_profile.json")
    data["data"]["metadata"]["nameChanges"] = [
        {"name": "First", "timestamp": "2019-01-01T00:00:00+00:00"},
        {"name": "Second", "timestamp": "2022-05-01T00:00:00+00:00"},
        {"name": "NoDate"},
    ]
    assert snapshot_from_profile(data, "uplay").name_changes == ["Second", "First", "NoDate"]
