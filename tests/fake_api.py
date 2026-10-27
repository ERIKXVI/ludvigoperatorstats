"""A tiny stand-in for the Tracker Network (R6 Tracker) and Arenyze APIs, shaped like their real responses."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from aiohttp import web

from conftest import load

API_KEY = "test-trn-key"
ARENYZE_KEY = "test-arenyze-key"
USER_ID = "11111111-2222-3333-4444-555555555555"
TRN_PREFIX = "/v2/r6siege/standard"


def _stat(stats: dict, key: str, delta: float) -> None:
    node = stats.setdefault(key, {"displayName": key, "metadata": {}, "value": 0, "displayType": "Number"})
    node["value"] = (node.get("value") or 0) + delta


class FakeApis:
    def __init__(self):
        self.profile = load("trn_profile.json")
        self.matches = load("trn_matches.json")
        self.operators = load("trn_operators.json")
        self.name = "Ludvig"  # current name; lookups by any other name 404 (like after a rename)
        self.requests: list[str] = []
        # failure switches
        self.invalid_key = False
        self.r6_forbidden = False
        self.cloudflare_block = False
        self.no_match_history = False
        self.matches_down = False
        self.missing_page: int | None = None  # this page of match history answers 404
        self.redirect = False  # answer with a redirect to another path that records what it receives
        self.leaked: list[str | None] = []
        self.next_id = 100

    # ---- helpers the tests use to "play"
    def play_match(self, *, won: bool, kills: int, deaths: int, rp: int, map_name: str = "Clubhouse",
                   operators: tuple[str, ...] = ("Ash", "Jäger"), minutes_ago: int = 0) -> str:
        """Add a new ranked match on top of the match history and bump the season stats like the game would."""
        tmpl = copy.deepcopy(self.matches["data"]["matches"][0])
        self.next_id += 1
        match_id = f"00000000-0000-0000-0000-{self.next_id:012d}"
        end = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        tmpl["attributes"]["id"] = match_id
        tmpl["metadata"]["timestamp"] = end.isoformat(timespec="milliseconds")  # R6 Tracker: when it ended
        tmpl["metadata"]["duration"] = 24 * 60 * 1000
        tmpl["metadata"]["sessionMapName"] = map_name
        seg = tmpl["segments"][0]
        st = seg["stats"]
        prev_rp = self._ranked_season()["stats"]["rankPoints"]["value"]
        for key, value in (("kills", kills), ("deaths", deaths), ("wins", int(won)), ("losses", int(not won)),
                           ("abandons", 0), ("roundsWon", 4 if won else 2), ("roundsLost", 2 if won else 4),
                           ("rankPointsPrevious", prev_rp), ("rankPoints", prev_rp + rp), ("rankPointsDelta", rp)):
            st[key]["value"] = value
        seg["metadata"]["result"] = "win" if won else "loss"
        seg["metadata"]["hasWon"] = won
        seg["metadata"]["operators"] = [{"name": o, "imageUrl": ""} for o in operators]
        self.matches["data"]["matches"].insert(0, tmpl)
        season = self._ranked_season()["stats"]
        for key, value in (("kills", kills), ("deaths", deaths), ("matchesPlayed", 1),
                           ("matchesWon" if won else "matchesLost", 1), ("rankPoints", rp)):
            _stat(season, key, value)
        return match_id

    def _ranked_season(self) -> dict:
        d = self.profile["data"]
        return next(s for s in d["segments"] if s["type"] == "season"
                    and s["attributes"]["season"] == d["metadata"]["currentSeason"]
                    and s["attributes"]["gamemode"] == "pvp_ranked")

    def rename(self, new_name: str) -> None:
        self.name = new_name
        self.profile["data"]["platformInfo"]["platformUserHandle"] = new_name
        self.profile["data"]["platformInfo"]["platformUserIdentifier"] = new_name

    # ---- server
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(TRN_PREFIX + "/profile/{slug}/{who}", self.trn_profile)
        app.router.add_get(TRN_PREFIX + "/profile/{slug}/{who}/segments/operator", self.trn_operators)
        app.router.add_get(TRN_PREFIX + "/matches/{slug}/{who}", self.trn_matches)
        app.router.add_get("/elsewhere", self.elsewhere)
        app.router.add_get("/r6/api/v2/operators", self.arenyze_operators)
        app.router.add_get("/r6/api/v2/profile", self.arenyze_profile)
        return app

    def _gate(self, request: web.Request):
        self.requests.append(f"{request.path}?{request.query_string}")
        if self.redirect:
            return web.Response(status=301, headers={"Location": "/elsewhere"})
        if self.cloudflare_block:
            return web.Response(status=403, text="<html><body>Just a moment...</body></html>", content_type="text/html")
        key = request.headers.get("TRN-Api-Key")
        if not key:
            return web.json_response({"message": "No API key found in request"}, status=401)
        if key != API_KEY or self.invalid_key:
            return web.json_response({"message": "Invalid authentication credentials"}, status=401)
        if self.r6_forbidden:
            return web.json_response({"message": "You cannot consume this service"}, status=403)
        who = request.match_info["who"]
        if request.match_info["slug"] not in ("ubi", "psn", "xbl"):
            return web.json_response({"errors": [{"code": "CollectorResultStatus::InvalidPlatform"}]}, status=400)
        current_id = self.profile["data"]["platformInfo"]["platformUserId"]
        if who.lower() != self.name.lower() and who != current_id:
            return web.json_response({"errors": [{"code": "CollectorResultStatus::NotFound",
                                                  "message": "This profile could not be found."}]}, status=404)
        return None

    async def elsewhere(self, request: web.Request):
        self.leaked.append(request.headers.get("TRN-Api-Key"))
        return web.json_response({"data": {}})

    def give_name_to(self, other_id: str) -> None:
        """The name 'Ludvig' now belongs to a different Ubisoft account (with different matches)."""
        self.profile["data"]["platformInfo"]["platformUserId"] = other_id
        self.matches["data"]["requestingPlayerAttributes"]["playerId"] = other_id
        for i, m in enumerate(self.matches["data"]["matches"]):
            m["attributes"]["id"] = f"other-{i}"

    async def trn_profile(self, request: web.Request):
        return self._gate(request) or web.json_response(copy.deepcopy(self.profile))

    async def trn_operators(self, request: web.Request):
        if (err := self._gate(request)) is not None:
            return err
        assert request.query["sessionType"] in ("all", "ranked", "quick-match", "standard", "dual-front", "r6cup")
        assert request.query["season"] == "all" or request.query["season"].isdigit()
        return web.json_response(copy.deepcopy(self.operators))

    async def trn_matches(self, request: web.Request):
        if (err := self._gate(request)) is not None:
            return err
        if self.no_match_history:
            return web.json_response({"message": "Not Found"}, status=404)
        if self.matches_down:
            return web.json_response({"errors": [{"code": "Internal", "message": "boom"}]}, status=500)
        page = int(request.query.get("next", 0))
        if page == self.missing_page:
            return web.json_response({"message": "Not Found"}, status=404)
        all_matches = self.matches["data"]["matches"]
        body = copy.deepcopy(self.matches)
        body["data"]["matches"] = all_matches[page * 20:(page + 1) * 20]
        body["data"]["metadata"] = {"next": page + 1} if (page + 1) * 20 < len(all_matches) else {}
        return web.json_response(body)

    async def arenyze_operators(self, request: web.Request):
        if request.headers.get("api-key") != ARENYZE_KEY:
            return web.json_response({"error": "Invalid API key"}, status=401)
        if request.query.get("nameOnPlatform") != "Ludvig":
            return web.json_response({"error": "Player not found"}, status=404)
        return web.json_response(load("arenyze_operators.json"))

    async def arenyze_profile(self, request: web.Request):
        if request.headers.get("api-key") != ARENYZE_KEY:
            return web.json_response({"error": "Invalid API key"}, status=401)
        return web.json_response(load("arenyze_profile.json"))
