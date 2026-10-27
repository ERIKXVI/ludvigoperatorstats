"""Client for Ubisoft's own (unofficial, undocumented) R6 Siege API.

Gives: account lookup, level, playtime, commendations and per-playlist season stats
(rank, RP, kills, deaths, wins, losses, abandons). Needs a Ubisoft account login;
a spare account without 2-step verification is best.

Endpoints and app ids follow the siegeapi Python library (github.com/CNDRD/siegeapi).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from ..models import BoardStats, PlayerInfo
from .errors import AuthError, PlayerNotFound, RateLimited, SourceError, message_from
from .parse import parse_full_profiles, parse_profile_stats

log = logging.getLogger(__name__)

UBI_BASE = "https://public-ubiservices.ubi.com"
SPACE_ID = "0d2ae42d-4c27-4cb7-af6c-2099062302bb"  # R6 Siege cross-play space
APP_IDS = {
    "base": "4391c956-8943-48eb-8859-07b0778f47b9",
    "advanced": "e3d5ea9e-50bd-43b7-88bf-39794f4e3d40",  # needed for full_profiles (ranks)
}
USER_AGENT = "UbiServices_SDK_2020.Release.58_PC64_ansi_static"
STAT_NAMES = (
    "PPvPTimePlayed,PPvETimePlayed,PTotalTimePlayed,PClearanceLevel,"
    "Commendations,CommendationsType,PlayerBPCurrentLevelPerSeason"
)
PLATFORMS = ("uplay", "psn", "xbl")
LOGIN_COOLDOWN = 600  # after a failed login, wait this long before trying again (avoids account lockouts)
TIMEOUT = aiohttp.ClientTimeout(total=25)


@dataclass
class _Ticket:
    ticket: str
    session_id: str
    expiration: str

    def valid(self) -> bool:
        try:
            exp = datetime.strptime(self.expiration[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return exp - timedelta(minutes=5) > datetime.now(timezone.utc)


class UbisoftClient:
    name = "ubisoft"

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
        *,
        cache_path: Path | None = None,
        base_url: str = UBI_BASE,
    ):
        self._basic = base64.b64encode(f"{email}:{password}".encode()).decode()
        self._account = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
        self._session = session
        self._cache_path = cache_path
        self._base_url = base_url.rstrip("/")
        self._tickets: dict[str, _Ticket] = {}
        self._locks = {"base": asyncio.Lock(), "advanced": asyncio.Lock()}
        self._login_error: SourceError | None = None
        self._login_blocked_until = 0.0
        self._load_cache()

    # ------------------------------------------------------------------ auth

    def _load_cache(self) -> None:
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text())
            if data.get("account") == self._account:
                self._tickets = {k: _Ticket(**v) for k, v in data.get("tickets", {}).items()}
        except (ValueError, TypeError, OSError) as e:
            log.warning("Ignoring unreadable Ubisoft session cache: %s", e)

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"account": self._account, "tickets": {k: asdict(v) for k, v in self._tickets.items()}}
            fd = os.open(self._cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
        except OSError as e:
            log.warning("Couldn't save Ubisoft session cache: %s", e)

    async def _ticket(self, kind: str) -> _Ticket:
        async with self._locks[kind]:
            t = self._tickets.get(kind)
            if t and t.valid():
                return t
            if time.monotonic() < self._login_blocked_until and self._login_error:
                raise self._login_error
            try:
                t = await self._login(kind)
            except (AuthError, RateLimited) as e:
                self._login_error = e
                self._login_blocked_until = time.monotonic() + LOGIN_COOLDOWN
                raise
            self._login_error = None
            self._tickets[kind] = t
            self._save_cache()
            return t

    async def _login(self, kind: str, retried: bool = False) -> _Ticket:
        if kind == "advanced":
            authorization = f"Ubi_v1 t={(await self._ticket('base')).ticket}"
        else:
            authorization = f"Basic {self._basic}"
        headers = {
            "Authorization": authorization,
            "Ubi-AppId": APP_IDS[kind],
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        log.info("Logging in to Ubisoft (%s)", kind)
        try:
            async with self._session.post(
                f"{self._base_url}/v3/profiles/sessions",
                headers=headers,
                json={"rememberMe": True},
                timeout=TIMEOUT,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SourceError(f"Couldn't reach Ubisoft to log in: {e}") from e
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}

        if status == 429:
            raise RateLimited("Ubisoft is rate limiting logins; will retry in a few minutes.")
        if status in (401, 403):
            if kind == "advanced" and not retried:
                self._tickets.pop("base", None)  # base ticket went stale; get a fresh one
                return await self._login(kind, retried=True)
            raise AuthError(
                "Ubisoft rejected the login. Check UBISOFT_EMAIL / UBISOFT_PASSWORD "
                f"({message_from(data, f'HTTP {status}')})."
            )
        if status >= 400:
            raise SourceError(f"Ubisoft login failed: {message_from(data, f'HTTP {status}')}")
        if not data.get("ticket"):
            if data.get("twoFactorAuthenticationTicket"):
                raise AuthError(
                    "This Ubisoft account has 2-step verification turned on, which the API can't "
                    "get past. Use a spare Ubisoft account without 2FA."
                )
            raise AuthError("Ubisoft login didn't return a session ticket.")
        return _Ticket(
            ticket=data["ticket"],
            session_id=data.get("sessionId", ""),
            expiration=data.get("expiration", ""),
        )

    # -------------------------------------------------------------- requests

    async def _get(self, path: str, params: dict[str, str], kind: str = "base") -> Any:
        url = f"{self._base_url}{path}"
        for attempt in range(3):
            t = await self._ticket(kind)
            headers = {
                "Authorization": f"Ubi_v1 t={t.ticket}",
                "Ubi-AppId": APP_IDS[kind],
                "Ubi-SessionId": t.session_id,
                "Ubi-LocaleCode": "en-US",
                "User-Agent": USER_AGENT,
                "expiration": t.expiration,
            }
            try:
                async with self._session.get(url, params=params, headers=headers, timeout=TIMEOUT) as resp:
                    status = resp.status
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise SourceError(f"Couldn't reach Ubisoft: {e}") from e

            if status == 401 or "Invalid session" in text[:300]:
                self._tickets.pop(kind, None)  # ticket expired early; log in again
                continue
            if status >= 500 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = None
            if status == 429:
                raise RateLimited("Ubisoft is rate limiting requests; will retry later.")
            if status >= 400:
                raise SourceError(f"Ubisoft API error on {path}: {message_from(data, f'HTTP {status}')}")
            if data is None:
                raise SourceError(f"Ubisoft returned something that isn't JSON for {path}.")
            return data
        raise SourceError("Ubisoft keeps rejecting the session; giving up for now.")

    # ------------------------------------------------------------------- api

    async def find_player(self, name: str, platform: str = "uplay") -> PlayerInfo:
        data = await self._get("/v3/profiles", {"nameOnPlatform": name, "platformType": platform})
        return self._pick_profile(data, platform, name)

    async def player_by_user_id(self, user_id: str, platform: str = "uplay") -> PlayerInfo:
        data = await self._get(f"/v3/users/{user_id}/profiles", {"platformType": platform})
        return self._pick_profile(data, platform, None)

    @staticmethod
    def _pick_profile(data: Any, platform: str, name: str | None) -> PlayerInfo:
        profiles = [
            p
            for p in (data or {}).get("profiles", [])
            if p.get("platformType") == platform and p.get("userId")
        ]
        if not profiles:
            raise PlayerNotFound(f"No {platform} account found for '{name}'." if name else "Account not found.")
        if name:
            exact = [p for p in profiles if str(p.get("nameOnPlatform", "")).lower() == name.lower()]
            profiles = exact or profiles
        p = profiles[0]
        return PlayerInfo(
            name=p.get("nameOnPlatform") or name or "",
            platform=platform,
            profile_id=p.get("profileId"),
            user_id=p.get("userId"),
        )

    async def full_profiles(self, player: PlayerInfo) -> dict[str, BoardStats]:
        """Current-season stats per playlist (ranked, quick match, ...)."""
        family = "pc" if player.platform == "uplay" else "console"
        data = await self._get(
            f"/v2/spaces/{SPACE_ID}/title/r6s/skill/full_profiles",
            {"profile_ids": player.user_id or "", "platform_families": family},
            kind="advanced",
        )
        return parse_full_profiles(data)

    async def profile_stats(self, player: PlayerInfo) -> dict[str, Any]:
        """Level, playtime, commendations, battle pass level."""
        data = await self._get(
            "/v1/profiles/stats",
            {"profileIds": player.user_id or "", "spaceId": SPACE_ID, "statNames": STAT_NAMES},
        )
        return parse_profile_stats(data)
