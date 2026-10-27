"""Client for the Arenyze R6 API (https://r6.arenyze.com, the backend of the r6-data.js package).

Ubisoft no longer serves per-operator stats from its own API, so this is where operator
stats come from. It can also provide rank/K/D/W-L when no Ubisoft login is configured.
A free API key is needed: sign up on r6.arenyze.com and create one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from .. import __version__
from ..models import BoardStats, OperatorStats, PlayerInfo
from .errors import AuthError, PlayerNotFound, RateLimited, SourceError, message_from
from .parse import parse_full_profiles, parse_operator_list

log = logging.getLogger(__name__)

ARENYZE_BASE = "https://public-api.arenyze.com/r6/api"
FAMILIES = {"uplay": "pc", "psn": "psn", "xbl": "xbl"}
# Playlist filters the operators endpoint understands.
OPERATOR_MODES = ("all", "ranked", "standard", "unranked", "quick-match", "casual", "dual-front", "siege-cup")
TIMEOUT = aiohttp.ClientTimeout(total=30)


class ArenyzeClient:
    name = "arenyze"

    def __init__(self, api_key: str, session: aiohttp.ClientSession, *, base_url: str = ARENYZE_BASE):
        self._key = api_key
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        headers = {
            "api-key": self._key,
            "Accept": "application/json",
            "User-Agent": f"r6tracker/{__version__}",
        }
        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{self._base_url}{path}", params=params, headers=headers, timeout=TIMEOUT
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise SourceError(f"Couldn't reach the Arenyze API: {e}") from e
            if status >= 500 and attempt == 0:
                await asyncio.sleep(3)
                continue
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = None
            msg = message_from(data, f"HTTP {status}")
            if status in (401, 403):
                raise AuthError(f"Arenyze rejected the API key (ARENYZE_API_KEY): {msg}")
            if status == 404:
                raise PlayerNotFound(f"Arenyze couldn't find that player: {msg}")
            if status == 429:
                raise RateLimited("Arenyze is rate limiting us; will retry later.")
            if status >= 400:
                raise SourceError(f"Arenyze API error on {path}: {msg}")
            if data is None:
                raise SourceError(f"Arenyze returned something that isn't JSON for {path}.")
            return data
        raise SourceError("Arenyze API unavailable.")

    async def profile(self, player: PlayerInfo) -> dict[str, Any]:
        """Raw /v2/profile payload: stats (full_profiles), account, banned, seasons, history."""
        return await self._get(
            "/v2/profile",
            {
                "nameOnPlatform": player.name,
                "platformType": player.platform,
                "platform_families": FAMILIES.get(player.platform, "pc"),
            },
        )

    async def find_player(self, name: str, platform: str = "uplay") -> PlayerInfo:
        data = await self.profile(PlayerInfo(name=name, platform=platform))
        if not parse_full_profiles(data.get("stats")):
            raise PlayerNotFound(f"No stats found for '{name}' on {platform}.")
        p = data.get("player") or {}
        user_id = None
        for fam in (data.get("stats") or {}).get("platform_families_full_profiles") or []:
            user_id = user_id or fam.get("profile_id")
        return PlayerInfo(name=p.get("nameOnPlatform") or name, platform=platform, user_id=user_id)

    @staticmethod
    def boards_from_profile(data: dict[str, Any]) -> dict[str, BoardStats]:
        return parse_full_profiles(data.get("stats"))

    async def operators(self, player: PlayerInfo, season: str = "all", mode: str = "all") -> list[OperatorStats]:
        """Per-operator stats. season is like "Y11S3" or "all"; mode is one of OPERATOR_MODES."""
        data = await self._get(
            "/v2/operators",
            {"nameOnPlatform": player.name, "platformType": player.platform, "seasonYear": season, "modes": mode},
        )
        return parse_operator_list(data)
