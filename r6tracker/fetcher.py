"""Picks the configured stats source and turns it into Snapshots, operator stats and match lists."""

from __future__ import annotations

import logging

import aiohttp

from .config import Settings
from .models import MatchBatch, OperatorStats, PlayerInfo, Snapshot
from .sources import ArenyzeClient, EndpointUnavailable, TrackerNetworkClient
from .sources.parse import parse_full_profiles

log = logging.getLogger(__name__)


class StatsFetcher:
    """R6 Tracker (Tracker Network) is the source. Arenyze is an optional backup used only when no
    TRN_API_KEY is set; it has no match history, so matches are then worked out from the totals."""

    def __init__(self, trn: TrackerNetworkClient | None = None, arenyze: ArenyzeClient | None = None):
        if not trn and not arenyze:
            raise ValueError("A stats source is needed")
        self.trn = trn
        self.arenyze = None if trn else arenyze
        self._no_match_history = False

    @classmethod
    def from_settings(cls, settings: Settings, session: aiohttp.ClientSession) -> "StatsFetcher":
        settings.check_sources()
        if settings.has_trn:
            return cls(trn=TrackerNetworkClient(settings.trn_api_key or "", session))
        return cls(arenyze=ArenyzeClient(settings.arenyze_api_key or "", session))

    @property
    def source_name(self) -> str:
        return (self.trn or self.arenyze).name  # type: ignore[union-attr]

    @property
    def has_operators(self) -> bool:
        return True

    @property
    def has_matches(self) -> bool:
        return self.trn is not None and not self._no_match_history

    async def find_player(self, name: str, platform: str = "uplay") -> PlayerInfo:
        if self.trn:
            return await self.trn.find_player(name, platform)
        assert self.arenyze
        return await self.arenyze.find_player(name, platform)

    async def profile(self, player: PlayerInfo) -> Snapshot:
        """Current stats (everything except operators). Raises SourceError on failure."""
        if self.trn:
            snap = await self.trn.profile(player)
            snap.sources.append(self.trn.name)
            return snap
        assert self.arenyze
        snap = Snapshot(player=player, sources=[self.arenyze.name])
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
        """season: "all" or a code like "Y11S3". mode: all, ranked, quick-match, standard, dual-front, siege-cup."""
        if self.trn:
            return await self.trn.operators(player, season, mode)
        assert self.arenyze
        return await self.arenyze.operators(player, season, mode)

    async def matches(self, player: PlayerInfo, cursor: int | None = None) -> tuple[list[MatchBatch], int | None] | None:
        """A page of real matches, or None when the source has no match history."""
        if not self.has_matches:
            return None
        assert self.trn
        try:
            return await self.trn.matches(player, cursor)
        except EndpointUnavailable as e:
            log.warning("No match history available (%s); working matches out from the totals instead.", e)
            self._no_match_history = True
            return None
