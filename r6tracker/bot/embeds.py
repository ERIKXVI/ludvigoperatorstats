"""Builders for every Discord embed the bot sends. Pure functions, so they're easy to test."""

from __future__ import annotations

from datetime import tzinfo
from urllib.parse import quote

import discord

from .. import operators as opmeta
from ..formatting import (
    board_line,
    clip,
    duration,
    hours,
    local,
    plural,
    operator_table,
    rank_change,
    rank_line,
    side_emoji,
    signed,
    verdict,
    wl,
)
from ..models import BOARD_LABELS, MatchBatch, OperatorDelta, OperatorStats, PlayerInfo, Snapshot, pct, ratio, season_code
from ..ranks import rank_color, rank_name
from ..storage import COMPLAINT_CATEGORIES, Complaint, TrackedPlayer
from ..tracker import PlaySummary, PollResult

WIN = 0x0CA30C
LOSS = 0xD03B3B
ABANDON = 0xFAB219
NEUTRAL = 0x3987E5
FIELD_MAX = 1024


TRACKER_SLUGS = {"uplay": "ubi", "psn": "psn", "xbl": "xbl"}


def tracker_url(player: PlayerInfo) -> str:
    """The player's R6 Tracker page (linking to it is fine; the bot never fetches it)."""
    return (f"https://r6.tracker.network/r6siege/profile/{TRACKER_SLUGS.get(player.platform, 'ubi')}/"
            f"{quote(player.name, safe='')}/overview")


def _author(embed: discord.Embed, player: PlayerInfo) -> None:
    embed.set_author(name=player.name, icon_url=player.avatar_url, url=tracker_url(player))


def _thumb(embed: discord.Embed, op_names: list[str], player: PlayerInfo) -> None:
    for name in op_names:
        url = opmeta.icon_url(name)
        if url:
            embed.set_thumbnail(url=url)
            return
    if player.avatar_url:
        embed.set_thumbnail(url=player.avatar_url)


def _op_delta_lines(deltas: list[OperatorDelta], limit: int = 8) -> str:
    lines = [
        f"{side_emoji(d.side)} **{d.name}** — {d.rounds} rd{'s' * (d.rounds != 1)}, "
        f"{d.kills}/{d.deaths} K/D, {d.rounds_won} won"
        for d in deltas[:limit]
    ]
    if len(deltas) > limit:
        lines.append(f"…and {len(deltas) - limit} more")
    return clip("\n".join(lines), FIELD_MAX)


def _color_for(wins: int, losses: int, abandons: int = 0) -> int:
    if wins > losses:
        return WIN
    if losses > wins:
        return LOSS
    return ABANDON if abandons and not wins else NEUTRAL


# ---------------------------------------------------------------- live news


def match_embed(result: PollResult, tz: tzinfo, operators_enabled: bool = True) -> discord.Embed:
    """Posted when new matches are detected."""
    snap = result.snapshot
    assert snap is not None
    player = snap.player
    batches = result.batches
    wins = sum(b.wins for b in batches)
    losses = sum(b.losses for b in batches)
    abandons = sum(b.abandons for b in batches)
    total = sum(b.matches for b in batches)

    if len(batches) == 1 and total == 1:
        b = batches[0]
        title = {
            "WIN": f"🏆 {player.name} won a {b.label} match",
            "LOSS": f"💀 {player.name} lost a {b.label} match",
        }.get(b.result_text, f"🏃 {player.name} abandoned a {b.label} match")
    elif len(batches) == 1:
        title = f"🎮 {player.name} played {total} {batches[0].label} matches ({batches[0].result_text})"
    else:
        title = f"🎮 {player.name} played {total} matches ({wl(wins, losses, abandons)})"

    embed = discord.Embed(title=title, color=_color_for(wins, losses, abandons), timestamp=snap.taken_at)
    _author(embed, player)
    by_board: dict[str, list[MatchBatch]] = {}
    for b in batches:
        by_board.setdefault(b.board, []).append(b)
    for board, group in list(by_board.items())[:20]:  # one field per playlist keeps big catch-ups postable
        first = group[0]
        k, d = sum(b.kills for b in group), sum(b.deaths for b in group)
        lines = [f"**K/D** {k} / {d} ({ratio(k, d):.2f})"]
        if len(batches) > 1 or first.matches > 1:
            gw, gl, ga = (sum(b.wins for b in group), sum(b.losses for b in group), sum(b.abandons for b in group))
            lines.insert(0, f"**Result** {first.result_text if len(group) == 1 else wl(gw, gl, ga)}")
        if board == "ranked":
            if len(group) == 1:
                if first.rp_before or first.rp_after:
                    lines.append(f"**Rank** {rank_change(first)}")
            else:
                part = PlaySummary(group)
                if part.rank_after is not None:
                    move = f"**{rank_name(part.rank_after)}**"
                    if part.rank_before is not None and part.rank_before != part.rank_after:
                        move = f"{rank_name(part.rank_before)} → {move}"
                    rp = f"{signed(part.rp_change)} RP{' (incomplete)' if part.rp_incomplete else ''}, " \
                        if part.rp_known else ""
                    lines.append(f"**Rank** {move} ({rp}now {part.rated[-1].rp_after:,})")
        season_board = snap.boards.get(board)
        if season_board:
            lines.append(f"**Season** {board_line(season_board)}")
        embed.add_field(name=first.label, value=clip("\n".join(lines), FIELD_MAX), inline=False)

    if result.operator_deltas:
        embed.add_field(name="Operators played", value=_op_delta_lines(result.operator_deltas), inline=False)
    elif operators_enabled:
        embed.add_field(name="Operators played", value="_Waiting for the operator stats to update…_", inline=False)
    _thumb(embed, [d.name for d in result.operator_deltas], player)
    embed.set_footer(text=f"{season_code(snap.season_id) or ''} · press a button below to complain or argue".lstrip(" ·"))
    return embed


RESULT_ICONS = {"WIN": "🏆", "LOSS": "💀", "ABANDON": "🏃"}
PLAYSTYLES = {
    "breacher": "Breacher", "entry_fragger": "Entry Fragger", "intel_provider": "Intel Provider",
    "roam_clearer": "Roam Clearer", "supporter": "Supporter", "utility_clearer": "Utility Clearer",
    "debuffer": "Debuffer", "entry_denier": "Entry Denier", "trapper": "Trapper", "utility_denier": "Utility Denier",
}


def _highlights(b: MatchBatch) -> str:
    x = b.extra
    bits = []
    if x.get("aces"):
        bits.append(f"🔥 {plural(x['aces'], 'ace')}")
    for n in (5, 4, 3):
        if x.get(f"kills_{n}k"):
            bits.append(f"{x[f'kills_{n}k']}× {n}K round")
    if x.get("clutches"):
        bits.append(f"🧊 {plural(x['clutches'], 'clutch', 'clutches')}")
    if x.get("first_bloods"):
        bits.append(f"⚡ {plural(x['first_bloods'], 'opening kill')}")
    if x.get("first_deaths"):
        bits.append(f"☠️ died first {plural(x['first_deaths'], 'time')}")
    if x.get("team_kills"):
        bits.append(f"🙃 {plural(x['team_kills'], 'team kill')}")
    return " · ".join(bits)


def real_match_embed(player: PlayerInfo, b: MatchBatch, snap: Snapshot | None = None) -> discord.Embed:
    """One real match from match history: map, score, K/D/A, RP and operators."""
    where = f" on {b.map_name}" if b.map_name else ""
    title = {
        "WIN": f"🏆 {player.name} won a {b.label} match{where}",
        "LOSS": f"💀 {player.name} lost a {b.label} match{where}",
    }.get(b.result_text, f"🏃 {player.name} left a {b.label} match{where}")
    lines = []
    score = []
    if b.rounds_won or b.rounds_lost:
        score.append(f"**{b.rounds_won}–{b.rounds_lost}** in rounds")
    if b.extra.get("surrender"):
        score.append("ended by surrender")
    if b.duration_seconds:
        score.append(f"{b.duration_seconds // 60} min")
    if score:
        lines.append(" · ".join(score))
    lines.append(f"**K/D/A** {b.kills} / {b.deaths} / {b.assists} ({b.kd:.2f})")
    if b.kills:
        lines.append(f"**Headshots** {b.headshots} ({pct(b.headshots, b.kills):.0f}% of kills)")
    if b.board == "ranked" and (b.rp_before or b.rp_after):
        lines.append(f"**Rank** {rank_change(b)}")
    embed = discord.Embed(
        title=title, description="\n".join(lines), color=_color_for(b.wins, b.losses, b.abandons), timestamp=b.detected_at
    )
    _author(embed, player)
    if b.operators:
        embed.add_field(
            name="Operators",
            value=clip(" · ".join(f"{side_emoji(opmeta.side_of(o))} {o}" for o in b.operators), FIELD_MAX),
            inline=False,
        )
    h = _highlights(b)
    if h:
        embed.add_field(name="Highlights", value=h, inline=False)
    board = snap.boards.get(b.board) if snap else None
    if board and board.matches:
        embed.add_field(name=f"{board.label} this season", value=board_line(board), inline=False)
    _thumb(embed, b.operators, player)
    embed.set_footer(text="Press a button below to complain or argue")
    return embed


def operators_update_embed(result: PollResult) -> discord.Embed:
    """Posted when operator numbers move after the match itself was already announced."""
    snap = result.snapshot
    assert snap is not None
    embed = discord.Embed(
        title=f"🎯 Operators {snap.player.name} just played",
        description=_op_delta_lines(result.operator_deltas, limit=12),
        color=NEUTRAL,
        timestamp=snap.taken_at,
    )
    _author(embed, snap.player)
    _thumb(embed, [d.name for d in result.operator_deltas], snap.player)
    return embed


def news_embeds(result: PollResult, tz: tzinfo, operators_enabled: bool = True) -> list[discord.Embed]:
    """What to post about a poll: one embed per real match (or one combined embed)."""
    snap = result.snapshot
    if snap is None or result.first_poll:
        return []
    if result.real_matches and result.batches:
        if len(result.batches) <= 5:
            return [real_match_embed(snap.player, b, snap) for b in result.batches]
        return [match_embed(result, tz, operators_enabled=False)]
    if result.batches:
        return [match_embed(result, tz, operators_enabled)]
    if result.operator_deltas and not result.real_matches:
        return [operators_update_embed(result)]
    return []


def summary_embed(player: PlayerInfo, s: PlaySummary, tz: tzinfo, title: str, empty: str) -> discord.Embed:
    """A session, a day... any stretch of tracked matches."""
    if not s.events:
        embed = discord.Embed(title=title, description=empty, color=NEUTRAL)
        _author(embed, player)
        return embed
    assert s.start and s.end
    desc = [
        f"**{plural(s.matches, 'match', 'matches')}** · {wl(s.wins, s.losses, s.abandons)} ({s.win_rate:.0f}% won)",
        f"**K/D** {s.kd:.2f} ({s.kills} kills / {s.deaths} deaths)",
        f"🕒 {local(s.start, tz, '%a %d %b %H:%M')} – {local(s.end, tz, '%H:%M')} ({duration(s.end - s.start)})",
    ]
    v = verdict(s.kd, s.win_rate, s.matches)
    if v:
        desc.append(f"\n{v}")
    embed = discord.Embed(title=title, description="\n".join(desc), color=_color_for(s.wins, s.losses, s.abandons))
    _author(embed, player)
    for board, part in s.by_board.items():
        line = f"{part.matches} · {wl(part.wins, part.losses, part.abandons)} · K/D {part.kd:.2f}"
        if board == "ranked" and s.rank_after is not None:
            move = f"**{rank_name(s.rank_after)}**"
            if s.rank_before is not None and s.rank_before != s.rank_after:
                move = f"{rank_name(s.rank_before)} → {move}"
            if s.rp_known:
                move += f" ({signed(s.rp_change)} RP{', incomplete' if s.rp_incomplete else ''})"
            line += f"\n{move}"
        embed.add_field(name=part.events[0].label, value=line, inline=True)
    counts = s.operator_counts
    if counts:
        lines = [f"{side_emoji(opmeta.side_of(o))} **{o}** ×{n}" for o, n in counts[:10]]
        embed.add_field(name="Operators played (matches)", value=clip("\n".join(lines), FIELD_MAX), inline=True)
    elif s.operators:
        embed.add_field(name="Operators", value=_op_delta_lines(s.operators), inline=False)
    if s.maps:
        embed.add_field(name="Maps", value=clip("\n".join(f"{m} ×{n}" for m, n in s.maps[:8]), FIELD_MAX), inline=True)
    _thumb(embed, [o for o, _ in counts] or [o.name for o in s.operators], player)
    return embed


# ---------------------------------------------------------------- commands


def stats_embed(
    tp: TrackedPlayer, snap: Snapshot, today: PlaySummary, tracked_matches: int, tz: tzinfo
) -> discord.Embed:
    player = snap.player
    ranked = snap.ranked
    color = rank_color(ranked.rank) if ranked and ranked.rank else NEUTRAL
    bits = []
    if snap.level is not None:
        bits.append(f"Level **{snap.level:,}**")
    if snap.playtime_seconds:
        bits.append(f"**{hours(snap.playtime_seconds)}** played")
    if snap.season_id:
        bits.append(f"Season **{season_code(snap.season_id)}**")
    if snap.banned:
        bits.append("⛔ **banned**")
    embed = discord.Embed(
        title=f"{player.name} — Rainbow Six Siege", description=" · ".join(bits) or None, color=color,
        timestamp=snap.taken_at,
    )
    _author(embed, player)
    if player.avatar_url:
        embed.set_thumbnail(url=player.avatar_url)

    if ranked and ranked.matches:
        embed.add_field(name="🏅 Ranked", value=f"{rank_line(ranked)}\n{board_line(ranked)}", inline=False)
    elif ranked:
        embed.add_field(name="🏅 Ranked", value=f"{rank_line(ranked)}\nNo ranked matches this season yet.", inline=False)
    for board_id in BOARD_LABELS:
        b = snap.boards.get(board_id)
        if board_id != "ranked" and b and b.matches:
            embed.add_field(name=b.label, value=board_line(b), inline=False)
    others = [b for k, b in snap.boards.items() if k not in BOARD_LABELS and b.matches]
    for b in others:
        embed.add_field(name=b.label, value=board_line(b), inline=False)
    total = snap.total
    if total.matches:
        embed.add_field(name="📊 Season total (all playlists)", value=board_line(total), inline=False)
    if snap.lifetime and snap.lifetime.matches:
        life = [board_line(snap.lifetime)]
        x = snap.extras
        more = []
        if x.get("headshot_pct"):
            more.append(f"{x['headshot_pct']:.0f}% headshots")
        if x.get("assists"):
            more.append(f"{int(x['assists']):,} assists")
        if x.get("wallbangs"):
            more.append(f"{int(x['wallbangs']):,} wallbangs")
        if x.get("team_kills"):
            more.append(f"{int(x['team_kills']):,} team kills 🙃")
        if more:
            life.append(" · ".join(more))
        embed.add_field(name="📚 Lifetime", value="\n".join(life), inline=False)
    style = _playstyle(snap.playstyles)
    if style:
        embed.add_field(name="🧠 Playstyle", value=style, inline=False)

    tracked = f"Today: **{plural(today.matches, 'match', 'matches')}**"
    if today.matches:
        tracked += f" ({wl(today.wins, today.losses, today.abandons)}, K/D {today.kd:.2f})"
    tracked += f"\nSince tracking began ({local(tp.added_at, tz, '%d %b %Y')}): **{plural(tracked_matches, 'match', 'matches')}**"
    embed.add_field(name="🛰️ Tracked by the bot", value=tracked, inline=False)

    if snap.operators:
        top = sorted(snap.operators, key=lambda o: -o.rounds_played)[:5]
        lines = [f"{side_emoji(o.side)} **{o.name}** — {o.rounds_played} rds, K/D {o.kd:.2f}, {o.win_rate:.0f}% won" for o in top]
        embed.add_field(name=f"🎯 Most played operators ({snap.operator_season})", value="\n".join(lines), inline=False)
        embed.set_thumbnail(url=opmeta.icon_url(top[0].name) or player.avatar_url)

    extras = []
    if snap.commendations.get("total"):
        extras.append(f"👏 {snap.commendations['total']:,} commendations")
    if snap.battle_pass_level:
        extras.append(f"🎟️ Battle pass level {snap.battle_pass_level}")
    if snap.pvp_seconds:
        extras.append(f"⏱️ {hours(snap.pvp_seconds)} in PvP")
    if extras:
        embed.add_field(name="Other", value=" · ".join(extras), inline=False)
    if snap.name_changes:
        embed.add_field(name="Formerly known as", value=clip(", ".join(snap.name_changes[:5]), 200), inline=False)
    embed.set_footer(text="Stats from " + " + ".join(snap.sources) if snap.sources else "")
    return embed


def _playstyle(styles: dict[str, int]) -> str:
    """Most frequent attacker and defender playstyle, e.g. "⚔️ Entry Fragger · 🛡️ Trapper"."""
    out = []
    for side in ("attacker", "defender"):
        mine = {k.split("_", 1)[1]: v for k, v in styles.items() if k.startswith(side + "_")}
        if mine:
            top = max(mine, key=lambda k: mine[k])
            out.append(f"{side_emoji(side)} {PLAYSTYLES.get(top, top.replace('_', ' ').title())}")
    return " · ".join(out)


def ranked_embed(snap: Snapshot) -> discord.Embed:
    r = snap.ranked
    player = snap.player
    if not r:
        embed = discord.Embed(title=f"{player.name} — Ranked", description="No ranked data this season.", color=NEUTRAL)
        _author(embed, player)
        return embed
    embed = discord.Embed(title=f"{player.name} — Ranked {season_code(r.season_id) or ''}", color=rank_color(r.rank))
    _author(embed, player)
    embed.description = rank_line(r)
    embed.add_field(name="Matches", value=f"{r.matches:,}", inline=True)
    embed.add_field(name="W / L", value=f"{r.wins} / {r.losses} ({r.win_rate:.0f}%)", inline=True)
    embed.add_field(name="Abandons", value=str(r.abandons), inline=True)
    embed.add_field(name="Kills / Deaths", value=f"{r.kills:,} / {r.deaths:,}", inline=True)
    embed.add_field(name="K/D", value=f"{r.kd:.2f}", inline=True)
    embed.add_field(name="Kills per match", value=f"{r.kills / r.matches:.1f}" if r.matches else "—", inline=True)
    if player.avatar_url:
        embed.set_thumbnail(url=player.avatar_url)
    return embed


def operators_embed(
    player: PlayerInfo, ops: list[OperatorStats], scope: str, sort: str, image_name: str | None = None
) -> discord.Embed:
    embed = discord.Embed(title=f"{player.name} — operators", color=NEUTRAL)
    _author(embed, player)
    if not ops:
        embed.description = f"No operator stats for {scope}."
        return embed
    rounds = sum(o.rounds_played for o in ops)
    embed.description = (
        f"{scope} · sorted by {sort} · {len(ops)} operators, {rounds:,} rounds\n"
        f"```\n{operator_table(ops, limit=15)}\n```"
    )
    atk = [o for o in ops if o.side == "attacker"]
    dfn = [o for o in ops if o.side == "defender"]
    for label, group in (("⚔️ Best attacker", atk), ("🛡️ Best defender", dfn)):
        qualified = [o for o in group if o.rounds_played >= 10] or group
        if qualified:
            best = max(qualified, key=lambda o: (o.kd, o.rounds_played))
            embed.add_field(name=label, value=f"**{best.name}** — K/D {best.kd:.2f} over {best.rounds_played} rds")
    _thumb(embed, [o.name for o in ops], player)
    if image_name:
        embed.set_image(url=f"attachment://{image_name}")
    return embed


def operator_embed(player: PlayerInfo, op: OperatorStats, scope: str) -> discord.Embed:
    info = opmeta.find(op.name)
    embed = discord.Embed(
        title=f"{side_emoji(op.side)} {op.name}",
        description=f"{player.name} · {scope}",
        color=NEUTRAL,
    )
    _author(embed, player)
    url = opmeta.icon_url(op.name)
    if url:
        embed.set_thumbnail(url=url)
    embed.add_field(name="Rounds", value=f"{op.rounds_played:,} ({op.rounds_won}W {op.rounds_lost}L, {op.win_rate:.0f}%)")
    if op.matches_played:
        embed.add_field(name="Matches", value=f"{op.matches_played:,} ({op.matches_won}W {op.matches_lost}L)")
    embed.add_field(name="K/D", value=f"{op.kd:.2f} ({op.kills:,} / {op.deaths:,})")
    embed.add_field(name="Headshots", value=f"{op.headshots:,} ({op.headshot_pct:.0f}% of kills)")
    embed.add_field(name="Kills per round", value=f"{op.kills_per_round:.2f}")
    embed.add_field(name="Survived", value=f"{op.survival_pct:.0f}% of rounds")
    if op.first_bloods or op.first_deaths:
        embed.add_field(name="Opening kills / deaths", value=f"{op.first_bloods} / {op.first_deaths}")
    if op.clutches or op.clutches_lost:
        embed.add_field(name="Clutches won / lost", value=f"{op.clutches} / {op.clutches_lost}")
    if op.aces:
        embed.add_field(name="Aces", value=str(op.aces))
    multi = {k: v for k, v in op.multi_kills.items() if k != "1k" and v}
    if multi:
        embed.add_field(name="Multi-kill rounds", value=" · ".join(f"{k.upper()}: {v}" for k, v in multi.items()))
    if op.assists:
        embed.add_field(name="Assists", value=f"{op.assists:,}")
    if op.time_played_ms:
        embed.add_field(name="Time played", value=hours(op.time_played_ms // 1000) if op.time_played_ms >= 3_600_000
                        else f"{op.time_played_ms // 60000} min")
    if op.team_kills:
        embed.add_field(name="Team kills 🙃", value=str(op.team_kills))
    if info:
        meta = [x for x in (info.org, info.squad and f"squad {info.squad}", info.season and f"since {info.season}") if x]
        embed.set_footer(text=" · ".join(meta))
    return embed


def matches_embed(player: PlayerInfo, events: list[MatchBatch], tz: tzinfo) -> discord.Embed:
    embed = discord.Embed(title=f"{player.name} — recent matches", color=NEUTRAL)
    _author(embed, player)
    if not events:
        embed.description = "No matches tracked yet. They show up here once the bot sees new games."
        return embed
    lines = []
    for e in reversed(events):
        where = f" on {e.map_name}" if e.map_name else ""
        score = f" {e.rounds_won}–{e.rounds_lost}" if e.rounds_won or e.rounds_lost else ""
        line = (f"`{local(e.detected_at, tz)}` {RESULT_ICONS.get(e.result_text, '🎮')} **{e.label}**{where}"
                f"{score} — {e.kills}/{e.deaths}")
        if e.board == "ranked" and e.rp_change and e.rp_known:
            line += f" ({signed(e.rp_change)} RP)"
        lines.append(line)
    embed.description = clip("\n".join(lines), 4000)
    if any(not e.match_id for e in events):
        embed.set_footer(text="A line without a map can cover several matches that finished between two checks.")
    return embed


def players_embed(players: list[tuple[TrackedPlayer, Snapshot | None]], tz: tzinfo, interval: float) -> discord.Embed:
    embed = discord.Embed(title="Tracked players", color=NEUTRAL)
    if not players:
        embed.description = "Nobody yet. Use `/track` to add someone."
        return embed
    for tp, snap in players[:25]:
        lines = [f"Platform: {tp.info.platform}"]
        if snap and snap.ranked:
            lines.append(rank_line(snap.ranked))
        lines.append(f"Last check: {local(tp.last_checked_at, tz) if tp.last_checked_at else 'never'}")
        if tp.last_error:
            lines.append(f"⚠️ {clip(tp.last_error, 300)}")
        embed.add_field(name=tp.name, value=clip("\n".join(lines), FIELD_MAX), inline=False)
    embed.set_footer(text=f"Checking every {interval:g} min")
    return embed


# --------------------------------------------------------------- complaints

KIND_EMOJI = {"complaint": "😤", "argument": "🗣️"}


def complaint_logged_embed(
    player: PlayerInfo, c: Complaint, number: int, today: int, last_match: MatchBatch | None, tz: tzinfo
) -> discord.Embed:
    """Posted when someone logs a complaint/argument with the buttons."""
    verb = "complained" if c.kind == "complaint" else "argued"
    embed = discord.Embed(
        title=f"{KIND_EMOJI[c.kind]} {player.name} {verb} about: {clip(c.topic, 150)}",
        description=f"> {clip(c.details, 1000)}" if c.details else None,
        color=LOSS if c.kind == "complaint" else ABANDON,
        timestamp=c.created_at,
    )
    _author(embed, player)
    if c.category:
        embed.add_field(name="Blamed", value=COMPLAINT_CATEGORIES.get(c.category, c.category), inline=True)
    embed.add_field(name=f"{c.kind.title()} #", value=f"**{number:,}**", inline=True)
    embed.add_field(name="Today", value=f"**{today}** {c.kind}{'s' * (today != 1)}", inline=True)
    if last_match and c.match_event_id == last_match.id:
        icon = {"WIN": "🏆", "LOSS": "💀"}.get(last_match.result_text, "🎮")
        embed.add_field(
            name="Right after",
            value=f"{icon} {last_match.label} {last_match.result_text} ({last_match.kills}/{last_match.deaths})",
            inline=True,
        )
    embed.set_footer(text=f"Logged by {c.reporter_name}" if c.reporter_name else "")
    return embed


def complaints_embed(
    player: PlayerInfo,
    total: dict[str, int],
    today: dict[str, int],
    topics: dict[str, list[tuple[str, int]]],
    categories: list[tuple[str, int]],
    recent: list[Complaint],
    reporters: list[tuple[str, int]],
    by_result: dict[str, int],
    tz: tzinfo,
) -> discord.Embed:
    embed = discord.Embed(title=f"😤 {player.name}'s complaint & argument tally", color=LOSS)
    _author(embed, player)
    if not any(total.values()):
        embed.description = (
            f"Nothing logged yet. Press **{player.name} complained** or **{player.name} argued** "
            "under any stats message when it happens."
        )
        return embed
    embed.description = (
        f"😤 **{plural(total['complaint'], 'complaint')}** · 🗣️ **{plural(total['argument'], 'argument')}**\n"
        f"Today: {plural(today['complaint'], 'complaint')}, {plural(today['argument'], 'argument')}"
    )
    for kind, label in (("complaint", "😤 Top complaints"), ("argument", "🗣️ Top arguments")):
        if topics.get(kind):
            embed.add_field(
                name=label,
                value=clip("\n".join(f"{i}. {clip(t, 80)} — **{n}×**" for i, (t, n) in enumerate(topics[kind], 1)),
                           FIELD_MAX),
                inline=True,
            )
    if categories:
        embed.add_field(
            name="Blamed most",
            value="\n".join(f"{COMPLAINT_CATEGORIES.get(k, k)} — **{n}×**" for k, n in categories[:6]),
            inline=False,
        )
    if by_result:
        embed.add_field(
            name="When it happens",
            value="\n".join(f"{k}: **{v}**" for k, v in by_result.items()),
            inline=False,
        )
    if recent:
        lines = []
        for c in recent:
            line = f"{KIND_EMOJI[c.kind]} `{local(c.created_at, tz)}` **{clip(c.topic, 80)}**"
            if c.details:
                line += f" — “{clip(c.details.replace(chr(10), ' '), 90)}”"
            lines.append(line)
        embed.add_field(name="Latest", value=clip("\n".join(lines), FIELD_MAX), inline=False)
    if reporters:
        embed.set_footer(text="Most logged by: " + ", ".join(f"{n} ({c})" for n, c in reporters))
    return embed
