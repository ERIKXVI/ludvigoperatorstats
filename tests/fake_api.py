"""A tiny stand-in for the Ubisoft and Arenyze APIs, shaped like their real responses."""

from __future__ import annotations

import base64
import copy

from aiohttp import web

from conftest import load
from r6tracker.sources.ubisoft import APP_IDS, SPACE_ID

EMAIL, PASSWORD, API_KEY = "bot@example.com", "hunter2", "test-key"
USER_ID = "11111111-2222-3333-4444-555555555555"


class FakeApis:
    def __init__(self):
        self.full_profiles = load("ubisoft_full_profiles.json")
        self.operators = load("arenyze_operators.json")
        self.logins: list[str] = []
        self.expire_next_request = False
        self.two_factor = False
        self.tickets = {"base": "T-base-1", "advanced": "T-adv-1"}

    # helpers the tests use to "play a match"
    def play_ranked_win(self, kills: int, deaths: int, rp: int):
        fp = self.full_profiles["platform_families_full_profiles"][0]["board_ids_full_profiles"][0]["full_profiles"][0]
        fp["season_statistics"]["kills"] += kills
        fp["season_statistics"]["deaths"] += deaths
        fp["season_statistics"]["match_outcomes"]["wins"] += 1
        fp["profile"]["rank_points"] += rp

    def play_operator_rounds(self, name: str, rounds: int, won: int, kills: int, deaths: int):
        for op in self.operators["operators"]["operators"]:
            if op["operator"] == name:
                op["roundsPlayed"] += rounds
                op["wins"] += won
                op["losses"] += rounds - won
                op["kills"] += kills
                op["deaths"] += deaths

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v3/profiles/sessions", self.sessions)
        app.router.add_get("/v3/profiles", self.profiles)
        app.router.add_get("/v3/users/{uid}/profiles", self.profiles)
        app.router.add_get(f"/v2/spaces/{SPACE_ID}/title/r6s/skill/full_profiles", self.get_full_profiles)
        app.router.add_get("/v1/profiles/stats", self.profile_stats)
        app.router.add_get("/r6/api/v2/operators", self.arenyze_operators)
        app.router.add_get("/r6/api/v2/profile", self.arenyze_profile)
        return app

    async def sessions(self, request: web.Request):
        appid = request.headers.get("Ubi-AppId")
        auth = request.headers.get("Authorization", "")
        if appid == APP_IDS["base"]:
            expected = "Basic " + base64.b64encode(f"{EMAIL}:{PASSWORD}".encode()).decode()
            if auth != expected:
                return web.json_response({"message": "Invalid credentials", "httpCode": 401}, status=401)
            if self.two_factor:
                return web.json_response({"twoFactorAuthenticationTicket": "2fa", "maskedPhone": "x"})
            kind = "base"
        elif appid == APP_IDS["advanced"]:
            if auth != f"Ubi_v1 t={self.tickets['base']}":
                return web.json_response({"message": "Invalid ticket", "httpCode": 401}, status=401)
            kind = "advanced"
        else:
            return web.json_response({"message": "bad app"}, status=400)
        self.logins.append(kind)
        return web.json_response({
            "ticket": self.tickets[kind],
            "sessionId": f"S-{kind}",
            "expiration": "2099-01-01T00:00:00.0000000Z",
            "profileId": "bot-profile",
            "userId": "bot-user",
        })

    def _check(self, request: web.Request, kind: str):
        if self.expire_next_request:
            self.expire_next_request = False
            # Ubisoft rotated the tickets: old ones are dead, a new login gets new ones.
            self.tickets = {k: v[:-1] + str(int(v[-1]) + 1) for k, v in self.tickets.items()}
            return web.Response(status=401, text='{"message":"Invalid session","httpCode":401}')
        h = request.headers
        if h.get("Authorization") != f"Ubi_v1 t={self.tickets[kind]}" or h.get("Ubi-AppId") != APP_IDS[kind]:
            return web.Response(status=401, text='{"message":"Invalid session","httpCode":401}')
        if h.get("Ubi-SessionId") != f"S-{kind}":
            return web.json_response({"message": "missing session id"}, status=400)
        return None

    async def profiles(self, request: web.Request):
        if (err := self._check(request, "base")) is not None:
            return err
        name = request.query.get("nameOnPlatform", "Ludvig")
        if name.lower() == "nobody":
            return web.json_response({"profiles": []})
        return web.json_response({"profiles": [
            {"profileId": "p-other", "userId": "u-other", "platformType": "psn", "nameOnPlatform": name},
            {"profileId": USER_ID, "userId": USER_ID, "platformType": request.query["platformType"],
             "idOnPlatform": USER_ID, "nameOnPlatform": "Ludvig"},
        ]})

    async def get_full_profiles(self, request: web.Request):
        if (err := self._check(request, "advanced")) is not None:
            return err
        assert request.query["profile_ids"] == USER_ID
        assert request.query["platform_families"] == "pc"
        return web.json_response(copy.deepcopy(self.full_profiles))

    async def profile_stats(self, request: web.Request):
        if (err := self._check(request, "base")) is not None:
            return err
        assert "PClearanceLevel" in request.query["statNames"]
        return web.json_response(load("ubisoft_profile_stats.json"))

    async def arenyze_operators(self, request: web.Request):
        if request.headers.get("api-key") != API_KEY:
            return web.json_response({"error": "Invalid API key"}, status=401)
        if request.query.get("nameOnPlatform") != "Ludvig":
            return web.json_response({"error": "Player not found"}, status=404)
        assert request.query["seasonYear"] in ("Y11S3", "all")
        return web.json_response(copy.deepcopy(self.operators))

    async def arenyze_profile(self, request: web.Request):
        if request.headers.get("api-key") != API_KEY:
            return web.json_response({"error": "Invalid API key"}, status=401)
        return web.json_response(load("arenyze_profile.json"))
