"""Slash commands."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import charts
from .. import operators as opmeta
from ..config import ConfigError, parse_platform
from ..formatting import sort_operators
from ..models import OperatorStats, Snapshot, season_code, utcnow
from ..sources import PlayerNotFound, SourceError
from ..storage import TrackedPlayer
from . import embeds
from .complaints import complaint_view, tally_embed

if TYPE_CHECKING:
    from .client import R6Bot

log = logging.getLogger(__name__)

PLAYLISTS = {
    "all": "all playlists",
    "ranked": "Ranked",
    "quick-match": "Quick Match",
    "standard": "Standard",
    "dual-front": "Dual Front",
    "siege-cup": "Siege Cup",
}
SORT_CHOICES = [
    app_commands.Choice(name="Rounds played", value="rounds"),
    app_commands.Choice(name="K/D", value="kd"),
    app_commands.Choice(name="Win %", value="winrate"),
    app_commands.Choice(name="Kills", value="kills"),
    app_commands.Choice(name="Headshot %", value="headshots"),
]
SIDE_CHOICES = [
    app_commands.Choice(name="Both", value="all"),
    app_commands.Choice(name="Attack", value="attacker"),
    app_commands.Choice(name="Defense", value="defender"),
]
PLAYLIST_CHOICES = [app_commands.Choice(name=label[0].upper() + label[1:], value=key) for key, label in PLAYLISTS.items()]
OPERATOR_CACHE_SECONDS = 300


class UserError(Exception):
    """Something to tell the user (shown privately), not a bug."""


class StatsCog(commands.Cog):
    def __init__(self, bot: R6Bot):
        self.bot = bot
        self._op_cache: dict[tuple[int, str, str], tuple[float, list[OperatorStats]]] = {}

    @property
    def tz(self):
        return self.bot.settings.tz

    # ------------------------------------------------------------- helpers

    def _player(self, name: str | None) -> TrackedPlayer:
        players = self.bot.storage.list_players()
        if not players:
            raise UserError("Nobody is being tracked yet. Use `/track` to add someone.")
        if not name:
            return players[0]
        tp = self.bot.storage.find_player(name)
        if not tp:
            raise UserError(f"I'm not tracking **{name}**. Tracked: {', '.join(p.name for p in players)}.")
        return tp

    def _latest(self, tp: TrackedPlayer) -> Snapshot:
        snap = self.bot.storage.latest_snapshot(tp.id)
        if not snap:
            raise UserError(f"No stats for {tp.name} yet. Try `/refresh` in a moment.")
        return snap

    async def _operators(self, tp: TrackedPlayer, season: str | None, playlist: str) -> tuple[list[OperatorStats], str]:
        snap = self._latest(tp)
        season = (season or "current").strip()
        if season.lower() == "current":
            season = season_code(snap.season_id) or "all"
        season = "all" if season.lower() in ("all", "lifetime") else season.upper()
        scope = f"{'all seasons' if season == 'all' else season} · {PLAYLISTS.get(playlist, playlist)}"

        fresh = snap.operators_fetched_at and utcnow() - snap.operators_fetched_at < timedelta(minutes=15)
        if fresh and season == snap.operator_season and playlist == snap.operator_mode:
            return snap.operators, scope
        key = (tp.id, season, playlist)
        cached = self._op_cache.get(key)
        if cached and time.monotonic() - cached[0] < OPERATOR_CACHE_SECONDS:
            return cached[1], scope
        ops = await self.bot.fetcher.operators(tp.info, season, playlist)
        self._op_cache[key] = (time.monotonic(), ops)
        return ops, scope

    async def player_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=p.name, value=p.name)
            for p in self.bot.storage.list_players()
            if current.lower() in p.name.lower()
        ][:25]

    async def operator_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = opmeta.normalize(current)
        return [
            app_commands.Choice(name=o.name, value=o.name)
            for o in opmeta.all_operators()
            if cur in opmeta.normalize(o.name)
        ][:25]

    async def season_autocomplete(self, interaction: discord.Interaction, current: str):
        options = ["current", "all"]
        players = self.bot.storage.list_players()
        snap = self.bot.storage.latest_snapshot(players[0].id) if players else None
        if snap and snap.season_id:
            options += [c for c in (season_code(snap.season_id - i) for i in range(8)) if c]
        return [app_commands.Choice(name=o, value=o) for o in options if current.lower() in o.lower()][:25]

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        err = getattr(error, "original", error)
        if isinstance(err, (UserError, SourceError, ConfigError)):
            msg = f"⚠️ {err}"
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "⚠️ You need the Manage Server permission for that."
        else:
            log.error("Command failed", exc_info=err)
            msg = "⚠️ Something went wrong. Check the bot's console for details."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------ commands

    @app_commands.command(description="Overview: rank, K/D, wins/losses, playtime, top operators")
    @app_commands.describe(player="Tracked player (defaults to the first one)")
    @app_commands.autocomplete(player=player_autocomplete)
    async def stats(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        snap = self._latest(tp)
        embed = embeds.stats_embed(
            tp, snap, self.bot.tracker.today(tp, self.tz), self.bot.storage.count_tracked_matches(tp.id), self.tz
        )
        await interaction.response.send_message(embed=embed, view=complaint_view(tp))

    @app_commands.command(description="Ranked details: rank, RP, peak, W/L, K/D")
    @app_commands.autocomplete(player=player_autocomplete)
    async def ranked(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        await interaction.response.send_message(embed=embeds.ranked_embed(self._latest(tp)), view=complaint_view(tp))

    @app_commands.command(description="Operator table + chart (rounds, win %, K/D, headshot %)")
    @app_commands.describe(
        season="current, all, or a season like Y11S2", playlist="Which playlist", side="Attack or defense",
        sort="What to sort by",
    )
    @app_commands.autocomplete(player=player_autocomplete, season=season_autocomplete)
    @app_commands.choices(playlist=PLAYLIST_CHOICES, side=SIDE_CHOICES, sort=SORT_CHOICES)
    async def operators(
        self, interaction: discord.Interaction, player: str | None = None, season: str | None = None,
        playlist: str = "all", side: str = "all", sort: str = "rounds",
    ):
        tp = self._player(player)
        await interaction.response.defer(thinking=True)
        ops, scope = await self._operators(tp, season, playlist)
        if side != "all":
            ops = [o for o in ops if o.side == side]
            scope += f" · {'attack' if side == 'attacker' else 'defense'}"
        ops = sort_operators(ops, sort, min_rounds=5 if sort != "rounds" else 0)
        sort_label = next(c.name for c in SORT_CHOICES if c.value == sort)
        kwargs = {}
        if ops:
            kwargs["file"] = await self.bot.chart_file(
                charts.operators_chart, ops, f"{tp.name} — most played operators ({scope})", filename="operators.png"
            )
        embed = embeds.operators_embed(tp.info, ops, scope, sort_label, "operators.png" if ops else None)
        await interaction.followup.send(embed=embed, **kwargs)

    @app_commands.command(description="Everything about one operator")
    @app_commands.describe(name="Operator name", season="current, all, or a season like Y11S2")
    @app_commands.autocomplete(name=operator_autocomplete, player=player_autocomplete, season=season_autocomplete)
    async def operator(
        self, interaction: discord.Interaction, name: str, player: str | None = None, season: str | None = None
    ):
        tp = self._player(player)
        await interaction.response.defer(thinking=True)
        ops, scope = await self._operators(tp, season, "all")
        wanted = opmeta.normalize(name)
        matches = [o for o in ops if opmeta.normalize(o.name) == wanted]
        if not matches:
            raise UserError(f"{tp.name} has no rounds on **{opmeta.display_name(name)}** in {scope}.")
        op = max(matches, key=lambda o: o.rounds_played)
        await interaction.followup.send(embed=embeds.operator_embed(tp.info, op, scope))

    @app_commands.command(description="How today has gone so far")
    @app_commands.autocomplete(player=player_autocomplete)
    async def today(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        s = self.bot.tracker.today(tp, self.tz)
        embed = embeds.summary_embed(tp.info, s, self.tz, f"📅 {tp.name} today", "No matches tracked today yet.")
        await interaction.response.send_message(embed=embed, view=complaint_view(tp))

    @app_commands.command(description="The current (or last) play session")
    @app_commands.autocomplete(player=player_autocomplete)
    async def session(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        s, ongoing = self.bot.tracker.last_session(tp)
        title = f"🎮 {tp.name} is playing right now" if ongoing else f"📋 {tp.name}'s last session"
        embed = embeds.summary_embed(tp.info, s, self.tz, title, "No sessions tracked in the last 30 days.")
        await interaction.response.send_message(embed=embed, view=complaint_view(tp))

    @app_commands.command(description="Chart of matches, K/D and RP over the last days")
    @app_commands.describe(days="How many days back (default 14)")
    @app_commands.autocomplete(player=player_autocomplete)
    async def history(
        self, interaction: discord.Interaction, player: str | None = None, days: app_commands.Range[int, 2, 90] = 14
    ):
        tp = self._player(player)
        await interaction.response.defer(thinking=True)
        daily = self.bot.tracker.daily(tp, days, self.tz)
        since = self.bot.local_midnight() - timedelta(days=days - 1)
        file = await self.bot.chart_file(
            charts.history_chart, daily, self.bot.tracker.rp_history(tp, since), tp.name, self.tz,
            filename="history.png",
        )
        s = self.bot.tracker.summary(tp, since=since)
        embed = embeds.summary_embed(
            tp.info, s, self.tz, f"📈 {tp.name} — last {days} days", "No matches tracked in this period yet."
        )
        embed.set_image(url="attachment://history.png")
        await interaction.followup.send(embed=embed, file=file)

    @app_commands.command(description="List the latest matches the bot detected")
    @app_commands.autocomplete(player=player_autocomplete)
    async def matches(
        self, interaction: discord.Interaction, player: str | None = None, count: app_commands.Range[int, 1, 40] = 15
    ):
        tp = self._player(player)
        events = self.bot.storage.match_events(tp.id, since=utcnow() - timedelta(days=60))[-count:]
        await interaction.response.send_message(embed=embeds.matches_embed(tp.info, events, self.tz))

    @app_commands.command(description="How many times they complained & argued, and about what")
    @app_commands.autocomplete(player=player_autocomplete)
    async def complaints(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        await interaction.response.send_message(embed=tally_embed(self.bot, tp), view=complaint_view(tp))

    @app_commands.command(description="Check for new matches right now")
    @app_commands.autocomplete(player=player_autocomplete)
    async def refresh(self, interaction: discord.Interaction, player: str | None = None):
        tp = self._player(player)
        await interaction.response.defer(thinking=True)
        result = await self.bot.tracker.poll(tp, force_operators=True)
        if result.error:
            raise UserError(f"Couldn't fetch stats: {result.error}")
        news = embeds.news_embeds(result, self.tz, self.bot.fetcher.has_operators) if result.has_news else []
        if news:
            # Post it where it normally goes too, if that's another channel.
            if self.bot.notify_channel_id() not in (None, interaction.channel_id):
                await self.bot.announce(result)
            await interaction.followup.send(embeds=news[:10], view=complaint_view(tp))
            return
        snap = result.snapshot
        assert snap is not None
        embed = embeds.stats_embed(
            tp, snap, self.bot.tracker.today(tp, self.tz), self.bot.storage.count_tracked_matches(tp.id), self.tz
        )
        await interaction.followup.send("No new matches since the last check.", embed=embed, view=complaint_view(tp))

    @app_commands.command(description="List tracked players and when they were last checked")
    async def players(self, interaction: discord.Interaction):
        rows = [(tp, self.bot.storage.latest_snapshot(tp.id)) for tp in self.bot.storage.list_players()]
        embed = embeds.players_embed(rows, self.tz, self.bot.settings.poll_interval_minutes)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Start tracking a player")
    @app_commands.describe(name="Their Ubisoft / PSN / Xbox name", platform="pc (default), psn or xbox")
    @app_commands.choices(platform=[
        app_commands.Choice(name="PC (Ubisoft Connect)", value="uplay"),
        app_commands.Choice(name="PlayStation", value="psn"),
        app_commands.Choice(name="Xbox", value="xbl"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def track(self, interaction: discord.Interaction, name: str, platform: str = "uplay"):
        await interaction.response.defer(thinking=True)
        try:
            tp, result = await self.bot.tracker.add_player(name.strip(), parse_platform(platform))
        except PlayerNotFound as e:
            raise UserError(str(e)) from e
        if result.error or not result.snapshot:
            await interaction.followup.send(
                f"Tracking **{tp.name}**, but the first stats fetch failed: {result.error}. I'll keep trying."
            )
            return
        embed = embeds.stats_embed(tp, result.snapshot, self.bot.tracker.today(tp, self.tz), 0, self.tz)
        await interaction.followup.send(
            f"👀 Now tracking **{tp.name}**. New matches show up within ~{self.bot.settings.poll_interval_minutes:g} min.",
            embed=embed, view=complaint_view(tp),
        )

    @app_commands.command(description="Stop tracking a player (their history is kept)")
    @app_commands.autocomplete(name=player_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def untrack(self, interaction: discord.Interaction, name: str):
        tp = self._player(name)
        self.bot.storage.deactivate_player(tp.id)
        await interaction.response.send_message(f"Stopped tracking **{tp.name}**. `/track` brings them back.")

    @app_commands.command(description="Post match updates in this channel (or the one you pick)")
    @app_commands.default_permissions(manage_guild=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        if target is None:
            raise UserError("Pick a channel.")
        self.bot.storage.set_setting("notify_channel_id", str(target.id))
        mention = getattr(target, "mention", f"<#{target.id}>")
        await interaction.response.send_message(f"✅ Match updates will be posted in {mention}.")
