"""Client for the Tracker Network (R6 Tracker) API: https://tracker.gg/developers

Needs a TRN API key (create an app on tracker.gg/developers), sent in the TRN-Api-Key header.
Tracker Network decides which games a key may read; their public docs don't list Rainbow Six
Siege, so a key has to have R6 access for this to work. Errors say so plainly when it doesn't.

Only the official, keyed API is used. The tracker.gg website and its internal API are off limits
under Tracker Network's terms, and this client never touches them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from .. import __version__
from ..models import MatchBatch, OperatorStats, PlayerInfo, Snapshot, season_id_from_code
from .errors import AuthError, EndpointUnavailable, PlayerNotFound, RateLimited, SourceError, message_from
from .trn_parse import (
    PLATFORM_SLUGS,
    parse_matches,
    parse_operators,
    player_from_profile,
    requesting_player_id,
    snapshot_from_profile,
)

log = logging.getLogger(__name__)

TRN_BASE = "https://public-api.tracker.gg/v2/r6siege/standard"
TIMEOUT = aiohttp.ClientTimeout(total=30)
# Our playlist filter names -> Tracker Network's sessionType values for operator stats.
SESSION_TYPES = {
    "all": "all",
    "ranked": "ranked",
    "quick-match": "quick-match",
    "standard": "standard",
    "dual-front": "dual-front",
    "siege-cup": "r6cup",
}


def _error_codes(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        return " ".join(str(e.get("code", "")) for e in data["errors"] if isinstance(e, dict))
    return ""


class TrackerNetworkClient:
    name = "R6 Tracker"

    def __init__(self, api_key: str, session: aiohttp.ClientSession, *, base_url: str = TRN_BASE):
        self._key = api_key
        self._session = session
        self._base_url = base_url.rstrip("/")
        # Whether Tracker Network accepts a Ubisoft id in place of a name (learned on first use).
        self._ids_work: bool | None = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        headers = {
            "TRN-Api-Key": self._key,
            "Accept": "application/json",
            "User-Agent": f"r6tracker/{__version__}",
        }
        for attempt in range(2):
            try:
                # No redirects: the API key header would be forwarded to wherever they point.
                async with self._session.get(
                    f"{self._base_url}{path}", params=params, headers=headers, timeout=TIMEOUT, allow_redirects=False
                ) as resp:
                    status = resp.status
                    text = await resp.text()
                    retry_after = resp.headers.get("Retry-After")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise SourceError(f"Couldn't reach Tracker Network: {e}") from e
            if status >= 500 and attempt == 0:
                await asyncio.sleep(3)
                continue
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = None
            return self._check(path, status, data, retry_after)
        raise SourceError("Tracker Network is unavailable right now.")

    @staticmethod
    def _check(path: str, status: int, data: Any, retry_after: str | None) -> Any:
        msg = message_from(data, f"HTTP {status}")
        codes = _error_codes(data)
        if status == 429:
            wait = f" (retry after {retry_after}s)" if retry_after else ""
            raise RateLimited(f"Tracker Network is rate limiting us{wait}; will retry later.")
        if 300 <= status < 400:
            raise SourceError(f"Tracker Network redirected {path} (HTTP {status}); not following it with the API key.")
        if data is None:
            # An HTML page (e.g. a Cloudflare block) instead of API JSON.
            raise SourceError(f"Tracker Network answered {path} with a web page instead of data (HTTP {status}).")
        if status == 401:
            # A valid key without R6 access gets the same 401 as a wrong key.
            raise AuthError(
                f"Tracker Network rejected TRN_API_KEY for Rainbow Six Siege ({msg}). Either the key is wrong, or "
                "it isn't allowed to read R6 data: Tracker Network's public API officially covers only Apex "
                "Legends and The Division 2 (tracker.gg/developers)."
            )
        if status == 403:
            raise AuthError(
                f"This TRN API key isn't allowed to read Rainbow Six Siege data ({msg}). Tracker Network has "
                "to enable R6 for your app (tracker.gg/developers)."
            )
        if status == 404 or codes:
            lowered = codes.lower()
            if "notfound" in lowered or "nodata" in lowered:
                raise PlayerNotFound(f"R6 Tracker has no player by that name ({msg}).")
            if status == 404:
                raise EndpointUnavailable(f"Tracker Network doesn't offer {path} ({msg}).")
            raise SourceError(f"Tracker Network error on {path}: {codes or msg}")
        if status >= 400:
            raise SourceError(f"Tracker Network error on {path}: {msg}")
        return data

    @staticmethod
    def _who(player: PlayerInfo, by_id: bool = False) -> str:
        slug = PLATFORM_SLUGS.get(player.platform, "ubi")
        ident = player.user_id if by_id and player.user_id else player.name
        return f"{slug}/{quote(ident, safe='')}"

    async def _lookup(self, player: PlayerInfo, path: str, params: dict[str, str] | None = None) -> Any:
        """Look the player up by their Ubisoft id (stable across renames) and fall back to the name.

        Callers that get an id back check it, so a name now used by someone else isn't silently followed.
        """
        if player.user_id and self._ids_work is not False:
            try:
                data = await self._get(f"/{path.format(who=self._who(player, by_id=True))}", params)
                self._ids_work = True
                return data
            except PlayerNotFound:
                if self._ids_work is None:
                    self._ids_work = False  # this API only takes names; stop trying ids
        return await self._get(f"/{path.format(who=self._who(player))}", params)

    @staticmethod
    def _same_account(player: PlayerInfo, found_id: str | None) -> None:
        if player.user_id and found_id and found_id != player.user_id:
            raise PlayerNotFound(
                f"The name '{player.name}' now belongs to a different Ubisoft account on R6 Tracker; "
                f"still tracking the original account ({player.user_id}). Use /track with their new name."
            )

    # ------------------------------------------------------------------- api

    async def find_player(self, name: str, platform: str = "uplay") -> PlayerInfo:
        data = await self._get(f"/profile/{self._who(PlayerInfo(name=name, platform=platform))}")
        info = player_from_profile(data, platform)
        if not info.name:
            raise PlayerNotFound(f"R6 Tracker has no player called '{name}'.")
        return info

    async def profile(self, player: PlayerInfo) -> Snapshot:
        data = await self._lookup(player, "profile/{who}")
        snap = snapshot_from_profile(data, player.platform)
        self._same_account(player, snap.player.user_id)
        snap.player.name = snap.player.name or player.name
        snap.player.user_id = snap.player.user_id or player.user_id
        snap.player.profile_id = snap.player.profile_id or player.profile_id
        return snap

    async def operators(self, player: PlayerInfo, season: str = "all", mode: str = "all") -> list[OperatorStats]:
        """season is "all" or a code like "Y11S3"; mode is one of SESSION_TYPES."""
        season_id = season_id_from_code(season) if season != "all" else None
        if season != "all" and season_id is None:
            raise SourceError(f"Unknown season '{season}'. Use current, all, or a season like Y11S2.")
        params = {"sessionType": SESSION_TYPES.get(mode, mode), "season": str(season_id) if season_id else "all"}
        return parse_operators(await self._lookup(player, "profile/{who}/segments/operator", params))

    async def matches(self, player: PlayerInfo, cursor: int | None = None) -> tuple[list[MatchBatch], int | None]:
        """One page of recent matches (newest first) and the cursor for the next page."""
        params = {"next": str(cursor)} if cursor is not None else None
        data = await self._lookup(player, "matches/{who}", params)
        self._same_account(player, requesting_player_id(data))
        return parse_matches(data, player)
