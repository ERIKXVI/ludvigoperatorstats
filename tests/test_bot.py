"""Discord side: embeds stay inside Discord's limits, and the complain/argue buttons + pop-up work."""

import asyncio
import re
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import load

from r6tracker.bot import embeds
from r6tracker.bot.client import R6Bot
from r6tracker.bot.complaints import LogButton, LogModal, TallyButton, complaint_view
from r6tracker.config import Settings
from r6tracker.models import MatchBatch, OperatorDelta, PlayerInfo, Snapshot, utcnow
from r6tracker.sources.trn_parse import parse_matches, parse_operators, snapshot_from_profile
from r6tracker.tracker import PlaySummary, PollResult


@pytest.fixture
def bot(tmp_path):
    b = R6Bot(Settings(trn_api_key="x", data_dir=tmp_path, timezone="Europe/Stockholm"))
    yield b
    b.storage.close()


@pytest.fixture
def tp(bot):
    return bot.storage.add_player(PlayerInfo("Ludvig", user_id="u1"))


def full_snapshot() -> Snapshot:
    """A realistic snapshot: the (anonymized) real R6 Tracker profile plus operator stats."""
    snap = snapshot_from_profile(load("trn_profile.json"), "uplay")
    snap.sources = ["R6 Tracker"]
    snap.operators = parse_operators(load("trn_operators.json"))
    snap.operator_season = "Y11S3"
    return snap


def real_matches() -> list[MatchBatch]:
    return parse_matches(load("trn_matches.json"), PlayerInfo("Ludvig", user_id="11111111-2222-3333-4444-555555555555"))[0]


def assert_fits(embed: discord.Embed) -> None:
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert embed.title is None or len(embed.title) <= 256
    assert embed.description is None or len(embed.description) <= 4096
    for f in embed.fields:
        assert f.name and f.value and len(f.name) <= 256 and len(f.value) <= 1024


class FakeResponse:
    def __init__(self):
        self.sent: list[dict] = []
        self.modal = None

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    async def send_modal(self, modal):
        self.modal = modal


def interaction(bot):
    return SimpleNamespace(
        client=bot, user=SimpleNamespace(id=42, display_name="Erik"), guild_id=1, channel_id=2, message=None,
        response=FakeResponse(),
    )


def ranked_loss() -> MatchBatch:
    return MatchBatch(board="ranked", season_id=43, matches=1, wins=0, losses=1, abandons=0, kills=3, deaths=8,
                      rank_before=21, rank_after=20, rp_before=3010, rp_after=2988)


# ------------------------------------------------------------------- embeds


def test_embeds_fit_discord_limits(tp):
    tz = Settings(timezone="Europe/Stockholm").tz
    snap = full_snapshot()
    ops = snap.operators
    deltas = [OperatorDelta(name=o.name, side=o.side, rounds=3, rounds_won=2, kills=4, deaths=2) for o in ops * 4]
    result = PollResult(player=tp, snapshot=snap, previous=snap, batches=[ranked_loss()], operator_deltas=deltas)
    session = PlaySummary([ranked_loss()] * 6, deltas)

    built = [
        embeds.match_embed(result, tz),
        embeds.match_embed(PollResult(player=tp, snapshot=snap, batches=[ranked_loss(), ranked_loss()]), tz),
        embeds.operators_update_embed(result),
        embeds.summary_embed(tp.info, session, tz, "Session", ""),
        embeds.summary_embed(tp.info, PlaySummary([]), tz, "Today", "nothing"),
        embeds.stats_embed(tp, snap, session, 152, tz),
        embeds.ranked_embed(snap),
        embeds.operators_embed(tp.info, ops * 10, "Y11S3", "rounds", "operators.png"),
        embeds.operator_embed(tp.info, ops[0], "Y11S3"),
        embeds.matches_embed(tp.info, [ranked_loss()] * 40, tz),
        embeds.players_embed([(tp, snap)], tz, 5),
        *[embeds.real_match_embed(tp.info, m, snap) for m in real_matches()],
        embeds.summary_embed(tp.info, PlaySummary(real_matches() * 5), tz, "Session", ""),
        embeds.matches_embed(tp.info, real_matches() * 7, tz),
    ]
    for e in built:
        assert_fits(e)

    m = built[0]
    assert m.title == "💀 Ludvig lost a Ranked match"
    assert "Platinum V → **Gold I**" in m.fields[0].value and "−22 RP" in m.fields[0].value
    assert m.thumbnail.url == "https://r6operators.marcopixel.eu/icons/png/ash.png"
    assert built[1].title == "🎮 Ludvig played 2 matches (0W 2L)"
    stats = {f.name: f.value for f in built[5].fields}
    assert "Emerald II" in stats["🏅 Ranked"] and "peak Diamond V" in stats["🏅 Ranked"]
    assert "5,609 matches" in stats["📚 Lifetime"] and "37% headshots" in stats["📚 Lifetime"]
    assert built[5].author.url == "https://r6.tracker.network/r6siege/profile/ubi/Ludvig/overview"
    assert stats["🧠 Playstyle"] == "⚔️ Supporter · 🛡️ Trapper"
    assert stats["Formerly known as"] == "OldLudvig"


def test_real_match_embed():
    snap = full_snapshot()
    loss, win = real_matches()[:2]
    e = embeds.real_match_embed(snap.player, loss, snap)
    assert e.title == "💀 Ludvig lost a Ranked match on Villa"
    assert "**2–4** in rounds" in e.description and "**K/D/A** 1 / 5 / 1" in e.description
    assert "−19 RP, now 2,363" in e.description
    fields = {f.name: f.value for f in e.fields}
    assert fields["Operators"].startswith("🛡️ Kapkan · ⚔️ Gridlock · ⚔️ Ace")
    assert e.thumbnail.url.endswith("/kapkan.png")
    w = embeds.real_match_embed(snap.player, win, snap)
    assert w.title == "🏆 Ludvig won a Ranked match on Chalet"
    assert {f.name: f.value for f in w.fields}["Highlights"] == "1× 3K round · 🧊 1 clutch · ⚡ 1 opening kill"


def test_news_embeds(tp):
    snap = full_snapshot()
    ms = real_matches()
    prev = Snapshot(player=snap.player)
    one = PollResult(player=tp, snapshot=snap, previous=prev, batches=ms[:2], real_matches=True)
    assert [e.title.split(" on ")[1] for e in embeds.news_embeds(one, utcnow().tzinfo)] == ["Villa", "Chalet"]
    many = PollResult(player=tp, snapshot=snap, previous=prev, batches=ms * 2, real_matches=True)
    [combined] = embeds.news_embeds(many, utcnow().tzinfo)
    assert combined.title.startswith("🎮 Ludvig played 12 matches")
    # A long catch-up (e.g. after downtime) still fits in one postable message: one field per playlist.
    quick = [replace(m, board="quick_match", board_name="Quick Match") for m in ms]
    flood = PollResult(player=tp, snapshot=snap, previous=prev, batches=ms * 7 + quick, real_matches=True)
    [big] = embeds.news_embeds(flood, utcnow().tzinfo)
    assert_fits(big)
    assert [f.name for f in big.fields] == ["Ranked", "Quick Match"] and "Ranked" in big.fields[0].name
    first = PollResult(player=tp, snapshot=snap, previous=None, backfilled=ms, real_matches=True)
    assert embeds.news_embeds(first, utcnow().tzinfo) == [] and not first.has_news


# --------------------------------------------------------------- complaints


def test_buttons_carry_player_id_and_survive_restart(tp):
    view = complaint_view(tp)
    ids = [item.item.custom_id for item in view.children]
    assert ids == [f"r6t:log:complaint:{tp.id}", f"r6t:log:argument:{tp.id}", f"r6t:tally:{tp.id}"]
    labels = [item.item.label for item in view.children]
    assert labels == ["Ludvig complained", "Ludvig argued", "Tally"]

    # After a restart discord.py rebuilds the button from its custom_id via the template.
    m = re.fullmatch(LogButton.__discord_ui_compiled_template__, ids[1])
    rebuilt = asyncio.run(LogButton.from_custom_id(None, None, m))
    assert (rebuilt.kind, rebuilt.player_id) == ("argument", tp.id)
    assert re.fullmatch(TallyButton.__discord_ui_compiled_template__, ids[2])


def test_pressing_complained_opens_popup_and_logs_it(bot, tp):
    # Ludvig just lost a match 10 minutes ago.
    loss = ranked_loss()
    loss.detected_at = utcnow() - timedelta(minutes=10)
    bot.storage.add_match_batch(tp.id, loss)

    it = interaction(bot)
    asyncio.run(LogButton("complaint", tp.id, tp.name).callback(it))
    modal = it.response.modal
    assert isinstance(modal, LogModal)
    assert modal.title == "Ludvig complained again"
    labels = [item.text for item in modal.children]
    assert labels == ["What did Ludvig complain about?", "Who or what got the blame?", "Exact quote or details"]
    assert len(modal.category.options) == 8

    # Fill in the pop-up the way Discord does, then submit.
    modal.topic._refresh_state(None, {"value": "  the   spawn peekers "})
    modal.details._refresh_state(None, {"value": "How was he even there?!"})
    modal.category._refresh_state(None, {"values": ["enemies"]})
    it2 = interaction(bot)
    asyncio.run(modal.on_submit(it2))

    [c] = bot.storage.recent_complaints(tp.id)
    assert (c.kind, c.topic, c.category, c.details, c.reporter_name, c.match_event_id) == (
        "complaint", "the spawn peekers", "enemies", "How was he even there?!", "Erik", loss.id
    )
    sent = it2.response.sent[0]
    embed = sent["embed"]
    assert embed.title == "😤 Ludvig complained about: the spawn peekers"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Complaint #"] == "**1**" and fields["Blamed"] == "🎯 Enemies / cheaters"
    assert "LOSS" in {f.name: f.value for f in embed.fields}["Right after"]
    assert sent["view"] is not None
    assert_fits(embed)


def test_argued_and_tally(bot, tp):
    for topic in ("whose fault the loss was", "Whose fault the loss was", "the map"):
        bot.storage.add_complaint(tp.id, "argument", topic, category="teammates", reporter_id=1, reporter_name="Erik")
    bot.storage.add_complaint(tp.id, "complaint", "ubisoft servers", category="ubisoft", reporter_id=2,
                              reporter_name="Bob")

    it = interaction(bot)
    asyncio.run(TallyButton(tp.id).callback(it))
    sent = it.response.sent[0]
    assert sent["ephemeral"] is True
    e = sent["embed"]
    assert "**1 complaint**" in e.description and "**3 arguments**" in e.description
    fields = {f.name: f.value for f in e.fields}
    assert "whose fault the loss was — **2×**" in fields["🗣️ Top arguments"].lower()
    assert fields["Blamed most"].startswith("👥 Teammates — **3×**")
    assert e.footer.text.startswith("Most logged by: Erik (3)")
    assert_fits(e)


def test_button_for_untracked_player(bot):
    it = interaction(bot)
    asyncio.run(LogButton("complaint", 999).callback(it))
    assert it.response.modal is None and "isn't tracked" in it.response.sent[0]["content"]
