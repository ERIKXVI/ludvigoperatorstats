"""The Discord bot: polls stats in the background, posts new matches, answers slash commands."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks

from ..config import ConfigError, Settings
from ..fetcher import StatsFetcher
from ..sources import PlayerNotFound, SourceError
from ..storage import Storage, TrackedPlayer
from ..tracker import PollResult, Tracker
from . import embeds
from .complaints import LogButton, TallyButton, complaint_view

log = logging.getLogger(__name__)


class R6Bot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.http_session: aiohttp.ClientSession | None = None
        self.fetcher: StatsFetcher
        self.tracker: Tracker
        self.chart_lock = asyncio.Lock()  # matplotlib's pyplot isn't thread-safe
        self._configured_players_added = False
        self._warned_no_channel = False

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.fetcher = StatsFetcher.from_settings(self.settings, self.http_session)
        self.tracker = Tracker(
            self.storage,
            self.fetcher,
            session_gap=timedelta(minutes=self.settings.session_gap_minutes),
            operator_refresh=timedelta(minutes=self.settings.operator_refresh_minutes),
        )
        self.add_dynamic_items(LogButton, TallyButton)

        from .commands import StatsCog

        await self.add_cog(StatsCog(self))
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

        self.poll_loop.change_interval(minutes=self.settings.poll_interval_minutes)
        self.poll_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        self.poll_loop.cancel()
        await super().close()
        if self.http_session:
            await self.http_session.close()
        self.storage.close()

    # ------------------------------------------------------------- helpers

    def local_midnight(self) -> datetime:
        return datetime.now(self.settings.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    async def chart_file(self, func, *args, filename: str) -> discord.File:
        async with self.chart_lock:
            png = await asyncio.to_thread(func, *args)
        return discord.File(io.BytesIO(png), filename=filename)

    def notify_channel_id(self) -> int | None:
        stored = self.storage.get_setting("notify_channel_id")
        return int(stored) if stored else self.settings.notify_channel_id

    async def notify_channel(self) -> discord.abc.Messageable | None:
        cid = self.notify_channel_id()
        if not cid:
            return None
        channel = self.get_channel(cid)
        if channel is None:
            try:
                channel = await self.fetch_channel(cid)
            except discord.HTTPException as e:
                log.warning("Can't open notification channel %s: %s", cid, e)
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    # ------------------------------------------------------------- polling

    @tasks.loop(minutes=5)
    async def poll_loop(self) -> None:
        try:
            await self._add_configured_players()
            for result in await self.tracker.poll_all():
                await self.announce(result)
            for tp in self.storage.list_players():
                session = self.tracker.finished_session(tp)
                if session:
                    await self._send(
                        tp,
                        embed=embeds.summary_embed(
                            tp.info, session, self.settings.tz,
                            f"📋 Session over — {tp.name} played {session.matches} matches", "",
                        ),
                    )
        except Exception:
            log.exception("Poll loop iteration failed")  # keep the loop alive

    @poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def _add_configured_players(self) -> None:
        if self._configured_players_added:
            return
        for name, platform in self.settings.track_players:
            if self.storage.find_player(name, platform):
                continue
            try:
                tp, _ = await self.tracker.add_player(name, platform)
                log.info("Now tracking %s (%s)", tp.name, platform)
            except PlayerNotFound as e:
                log.error("TRACK_PLAYERS: %s", e)  # a typo won't fix itself; don't retry
            except SourceError as e:
                log.error("Couldn't start tracking %s yet: %s", name, e)
                return  # API trouble: try again next loop
        self._configured_players_added = True

    async def announce(self, result: PollResult) -> None:
        if not result.has_news or result.first_poll:
            return
        if result.batches:
            embed = embeds.match_embed(result, self.settings.tz, self.fetcher.has_operators)
        else:
            embed = embeds.operators_update_embed(result)
        await self._send(result.player, embed=embed)

    async def _send(self, tp: TrackedPlayer, **kwargs) -> None:
        channel = await self.notify_channel()
        if channel is None:
            if not self._warned_no_channel:
                log.warning("New matches found but no channel to post in: run /setchannel in Discord.")
                self._warned_no_channel = True
            return
        try:
            await channel.send(view=complaint_view(tp), **kwargs)
        except discord.HTTPException as e:
            log.warning("Couldn't post to the notification channel: %s", e)


def run_bot(settings: Settings) -> None:
    if not settings.discord_token:
        raise ConfigError("DISCORD_TOKEN is not set (see .env.example).")
    settings.check_sources()
    bot = R6Bot(settings)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure as e:
        raise ConfigError("Discord rejected DISCORD_TOKEN. Copy a fresh token from the developer portal.") from e
