"""The "<player> complained" / "<player> argued" buttons and the pop-up that asks what it was about.

The buttons carry the tracked player's id in their custom_id, so they keep working on old
messages after the bot restarts (discord.py DynamicItem).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from ..formatting import clip
from ..models import utcnow
from ..storage import COMPLAINT_CATEGORIES, TrackedPlayer
from . import embeds

if TYPE_CHECKING:
    from .client import R6Bot

# A complaint is linked to the last match if that match ended at most this long ago.
RECENT_MATCH = timedelta(hours=2)

LABELS = {"complaint": ("complained", "😤", discord.ButtonStyle.danger),
          "argument": ("argued", "🗣️", discord.ButtonStyle.primary)}


def complaint_view(tp: TrackedPlayer) -> discord.ui.View:
    """The row of buttons attached under the bot's messages about a player."""
    view = discord.ui.View(timeout=None)
    view.add_item(LogButton("complaint", tp.id, tp.name))
    view.add_item(LogButton("argument", tp.id, tp.name))
    view.add_item(TallyButton(tp.id))
    return view


class LogButton(discord.ui.DynamicItem[discord.ui.Button], template=r"r6t:log:(?P<kind>complaint|argument):(?P<pid>\d+)"):
    def __init__(self, kind: str, player_id: int, player_name: str = "They"):
        verb, emoji, style = LABELS[kind]
        super().__init__(
            discord.ui.Button(
                label=clip(f"{player_name} {verb}", 80),
                emoji=emoji,
                style=style,
                custom_id=f"r6t:log:{kind}:{player_id}",
            )
        )
        self.kind = kind
        self.player_id = player_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["kind"], int(match["pid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: R6Bot = interaction.client  # type: ignore[assignment]
        tp = bot.storage.get_player(self.player_id)
        if tp is None:
            await interaction.response.send_message("That player isn't tracked anymore.", ephemeral=True)
            return
        await interaction.response.send_modal(LogModal(self.kind, tp))


class TallyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"r6t:tally:(?P<pid>\d+)"):
    def __init__(self, player_id: int):
        super().__init__(
            discord.ui.Button(
                label="Tally", emoji="📊", style=discord.ButtonStyle.secondary, custom_id=f"r6t:tally:{player_id}"
            )
        )
        self.player_id = player_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["pid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: R6Bot = interaction.client  # type: ignore[assignment]
        tp = bot.storage.get_player(self.player_id)
        if tp is None:
            await interaction.response.send_message("That player isn't tracked anymore.", ephemeral=True)
            return
        await interaction.response.send_message(embed=tally_embed(bot, tp), ephemeral=True)


class LogModal(discord.ui.Modal):
    """The pop-up: what did they complain / argue about, who got the blame, and the quote."""

    def __init__(self, kind: str, tp: TrackedPlayer):
        verb = "complain" if kind == "complaint" else "argue"
        super().__init__(title=clip(f"{tp.name} {LABELS[kind][0]} again", 45), timeout=600)
        self.kind = kind
        self.tp = tp
        self.topic = discord.ui.TextInput(
            placeholder="e.g. spawn peekers, the teammates, Ubisoft servers, the map",
            max_length=150,
        )
        self.category = discord.ui.Select(
            placeholder="Pick one (optional)",
            required=False,
            options=[discord.SelectOption(label=text, value=key, emoji=emoji)
                     for key, (emoji, _, text) in ((k, v.partition(" ")) for k, v in COMPLAINT_CATEGORIES.items())],
        )
        self.details = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder='"How did he hit me through two walls?!"',
            required=False,
            max_length=1000,
        )
        self.add_item(discord.ui.Label(text=clip(f"What did {tp.name} {verb} about?", 45), component=self.topic))
        self.add_item(discord.ui.Label(text="Who or what got the blame?", component=self.category))
        self.add_item(discord.ui.Label(text="Exact quote or details", component=self.details,
                                       description="Optional, but quotes make the tally funnier."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: R6Bot = interaction.client  # type: ignore[assignment]
        tp = bot.storage.get_player(self.tp.id) or self.tp
        last = bot.storage.last_match_event(tp.id)
        recent = last if last and utcnow() - last.detected_at <= RECENT_MATCH else None
        c = bot.storage.add_complaint(
            tp.id,
            self.kind,
            self.topic.value,
            self.details.value,
            category=(self.category.values or [None])[0],
            reporter_id=interaction.user.id,
            reporter_name=interaction.user.display_name,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id if interaction.message else None,
            match_event_id=recent.id if recent else None,
        )
        total = bot.storage.complaint_counts(tp.id)[self.kind]
        today = bot.storage.complaint_counts(tp.id, since=bot.local_midnight())[self.kind]
        embed = embeds.complaint_logged_embed(tp.info, c, total, today, recent, bot.settings.tz)
        await interaction.response.send_message(embed=embed, view=complaint_view(tp))


def tally_embed(bot: "R6Bot", tp: TrackedPlayer) -> discord.Embed:
    s = bot.storage
    return embeds.complaints_embed(
        tp.info,
        total=s.complaint_counts(tp.id),
        today=s.complaint_counts(tp.id, since=bot.local_midnight()),
        topics={k: s.complaint_topics(tp.id, k) for k in ("complaint", "argument")},
        categories=s.complaint_categories(tp.id),
        recent=s.recent_complaints(tp.id, 5),
        reporters=s.complaint_reporters(tp.id),
        by_result=s.complaints_by_result(tp.id),
        tz=bot.settings.tz,
    )
