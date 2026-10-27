"""Combines the configured stats sources into Snapshots."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .config import Settings
from .models import OperatorStats, PlayerInfo, Snapshot
from .sources import ArenyzeClient, PlayerNotFound, SourceError, UbisoftClient
from .sources.parse import parse_full_profiles

log = logging.getLogger(__name__)


class StatsFetcher:
    """Ubisoft (if configured) is the source for profile/rank/K-D numbers; Arenyze fills in
    operators, and the profile numbers too when there's no Ubisoft login."""

    def __init__(self, ubisoft: UbisoftClient | None = None, arenyze: ArenyzeClient | None = None):
        if not ubisoft and not arenyze:
            raise ValueError("At least one stats source is needed")
        self.ubisoft = ubisoft
        self.arenyze = arenyze

    @classmethod
    def from_settings(cls, settings: Settings, session: aiohttp.ClientSession) -> "StatsFetcher":
        settings.check_sources()
        ubisoft = arenyze = None
        if settings.has_ubisoft:
            ubisoft = UbisoftClient(
                settings.ubisoft_email or "",
                settings.ubisoft_password or "",
                session,
                cache_path=settings.data_dir / "ubisoft_session.json",
            )
        if settings.has_arenyze:
            arenyze = ArenyzeClient(settings.arenyze_api_key or "", session)
        return cls(ubisoft, arenyze)

    @property
    def has_operators(self) -> bool:
        return self.arenyze is not None

    async def find_player(self, name: str, platform: str = "uplay") -> PlayerInfo:
        if self.ubisoft:
            return await self.ubisoft.find_player(name, platform)
        assert self.arenyze
        return await self.arenyze.find_player(name, platform)

    async def profile(self, player: PlayerInfo) -> Snapshot:
        """Everything except operators. Raises SourceError if no playlist stats could be fetched."""
        snap = Snapshot(player=player)
        if self.ubisoft:
            snap.sources.append(self.ubisoft.name)
            boards, stats = await asyncio.gather(
                self.ubisoft.full_profiles(player), self.ubisoft.profile_stats(player), return_exceptions=True
            )
            if isinstance(boards, BaseException):
                raise boards
            snap.boards = boards
            if isinstance(stats, BaseException):
                _reraise_unexpected(stats)
                snap.errors["ubisoft_stats"] = str(stats)
            else:
                for key in ("level", "playtime_seconds", "pvp_seconds", "pve_seconds", "battle_pass_level"):
                    setattr(snap, key, stats.get(key))
                snap.commendations = stats.get("commendations") or {}
        else:
            assert self.arenyze
            snap.sources.append(self.arenyze.name)
            data = await self.arenyze.profile(player)
            snap.boards = parse_full_profiles(data.get("stats"))
            account = data.get("account") or {}
            snap.level = account.get("level") or (account.get("profile") or {}).get("level")
            snap.xp = account.get("xp")
            banned = data.get("banned")
            if isinstance(banned, dict) and "isBanned" in banned:
                snap.banned = bool(banned["isBanned"])
        return snap

    async def operators(self, player: PlayerInfo, season: str = "all", mode: str = "all") -> list[OperatorStats]:
        if not self.arenyze:
            raise SourceError("Operator stats need an Arenyze API key (ARENYZE_API_KEY in .env).")
        try:
            return await self.arenyze.operators(player, season, mode)
        except PlayerNotFound:
            # Arenyze looks players up by name; if they renamed, Ubisoft knows the new one.
            if not (self.ubisoft and player.user_id):
                raise
            fresh = await self.ubisoft.player_by_user_id(player.user_id, player.platform)
            if fresh.name == player.name:
                raise
            log.info("%s is now called %s", player.name, fresh.name)
            player.name = fresh.name
            return await self.arenyze.operators(player, season, mode)


def _reraise_unexpected(exc: BaseException) -> None:
    """gather(return_exceptions=True) also swallows bugs and cancellation; don't hide those."""
    if not isinstance(exc, SourceError):
        raise exc
