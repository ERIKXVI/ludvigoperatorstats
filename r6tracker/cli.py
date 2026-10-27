"""Command-line program: run the bot, run the tracker without Discord, or look at stats in the terminal."""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import aiohttp

from . import __version__
from .config import ConfigError, Settings, parse_platform
from .fetcher import StatsFetcher
from .formatting import (
    board_line,
    hours,
    local,
    plural,
    operator_table,
    rank_change,
    rank_line,
    side_emoji,
    sort_operators,
    verdict,
    wl,
)
from .models import BOARD_LABELS, season_code, utcnow
from .sources import SourceError
from .storage import COMPLAINT_CATEGORIES, Storage, TrackedPlayer
from .tracker import PlaySummary, PollResult, Tracker

log = logging.getLogger("r6tracker")


def plain(s: str) -> str:
    return s.replace("**", "")


# ------------------------------------------------------------------ plumbing


class App:
    def __init__(self, settings: Settings, storage: Storage, tracker: Tracker):
        self.settings = settings
        self.storage = storage
        self.tracker = tracker

    def player(self, name: str | None) -> TrackedPlayer:
        players = self.storage.list_players()
        if not players:
            raise ConfigError("Nobody is tracked yet. Run: r6tracker add <name>")
        if not name:
            return players[0]
        tp = self.storage.find_player(name)
        if not tp:
            raise ConfigError(f"Not tracking '{name}'. Tracked: {', '.join(p.name for p in players)}")
        return tp


@asynccontextmanager
async def open_app(settings: Settings, online: bool = True):
    storage = Storage(settings.db_path)
    try:
        if not online:
            yield App(settings, storage, Tracker(storage, None))
            return
        async with aiohttp.ClientSession() as session:
            fetcher = StatsFetcher.from_settings(settings, session)
            tracker = Tracker(
                storage,
                fetcher,
                session_gap=timedelta(minutes=settings.session_gap_minutes),
                operator_refresh=timedelta(minutes=settings.operator_refresh_minutes),
            )
            yield App(settings, storage, tracker)
    finally:
        storage.close()


# ------------------------------------------------------------------ printing


def print_snapshot(app: App, tp: TrackedPlayer) -> None:
    snap = app.storage.latest_snapshot(tp.id)
    if not snap:
        print(f"No stats stored for {tp.name} yet.")
        return
    tz = app.settings.tz
    head = [f"{snap.player.name} ({snap.player.platform})"]
    if snap.level is not None:
        head.append(f"Level {snap.level:,}")
    if snap.playtime_seconds:
        head.append(f"{hours(snap.playtime_seconds)} played")
    if snap.season_id:
        head.append(f"Season {season_code(snap.season_id)}")
    print(" · ".join(head))
    print(f"(as of {local(snap.taken_at, tz)})\n")
    if snap.ranked:
        print("Ranked:      " + plain(rank_line(snap.ranked)))
        print("             " + board_line(snap.ranked))
    for board_id, b in snap.boards.items():
        if board_id != "ranked" and b.matches:
            print(f"{b.label + ':':<13}{board_line(b)}")
    if snap.total.matches:
        print(f"{'All:':<13}{board_line(snap.total)}")
    today = app.tracker.today(tp, tz)
    print(f"\nTracked today: {plural(today.matches, 'match', 'matches')}" + (f" ({wl(today.wins, today.losses, today.abandons)}, K/D {today.kd:.2f})" if today.matches else ""))
    print(f"Tracked since {local(tp.added_at, tz, '%d %b %Y')}: "
          f"{plural(app.storage.count_tracked_matches(tp.id, since=tp.added_at), 'match', 'matches')}")
    counts = app.storage.complaint_counts(tp.id)
    if any(counts.values()):
        print(f"Complaints logged: {counts['complaint']} · arguments: {counts['argument']}")
    if snap.operators:
        print(f"\nMost played operators ({snap.operator_season}):")
        print(operator_table(sort_operators(snap.operators), limit=10))
    if tp.last_error:
        print(f"\n⚠ last check had a problem: {tp.last_error}")


def print_poll(result: PollResult, app: App) -> None:
    tz = app.settings.tz
    stamp = local(utcnow(), tz, "%H:%M")
    name = result.player.name
    if result.error:
        print(f"[{stamp}] {name}: couldn't fetch stats: {result.error}")
        return
    if result.first_poll:
        extra = f" (and {plural(len(result.backfilled), 'recent match', 'recent matches')} saved as history)" \
            if result.backfilled else ""
        print(f"[{stamp}] {name}: baseline saved{extra}; new matches will be detected from now on.")
        return
    for b in result.batches:
        if b.match_id:
            where = f" on {b.map_name}" if b.map_name else ""
            score = f" {b.rounds_won}-{b.rounds_lost}" if b.rounds_won or b.rounds_lost else ""
            line = f"[{stamp}] {name}: {b.label} {b.result_text}{where}{score} — K/D/A {b.kills}/{b.deaths}/{b.assists}"
            if b.operators:
                line += f" · {', '.join(b.operators)}"
        else:
            line = f"[{stamp}] {name}: {b.label} {b.result_text} ({plural(b.matches, 'match', 'matches')}) — K/D {b.kills}/{b.deaths}"
        if b.board == "ranked" and (b.rp_before or b.rp_after):
            line += " · " + plain(rank_change(b))
        print(line)
    if result.operator_deltas and not result.real_matches:
        ops = ", ".join(f"{d.name} ({d.rounds} rds, {d.kills}/{d.deaths})" for d in result.operator_deltas[:8])
        print(f"[{stamp}] {name}: operators played: {ops}")
    if not result.has_news:
        print(f"[{stamp}] {name}: no new matches")


def print_summary(title: str, s: PlaySummary, app: App) -> None:
    print(title)
    if not s.events:
        print("  nothing tracked")
        return
    tz = app.settings.tz
    assert s.start and s.end
    print(f"  {local(s.start, tz)} – {local(s.end, tz, '%H:%M')}")
    print(f"  {plural(s.matches, 'match', 'matches')} · {wl(s.wins, s.losses, s.abandons)} ({s.win_rate:.0f}%) · K/D {s.kd:.2f} ({s.kills}/{s.deaths})")
    if s.ranked_events:
        print(f"  Ranked RP: {s.rp_change:+d}")
    for board, part in s.by_board.items():
        print(f"  {BOARD_LABELS.get(board, board)}: {part.matches} · {wl(part.wins, part.losses, part.abandons)} · K/D {part.kd:.2f}")
    if s.operator_counts:
        print("  Operators: " + ", ".join(f"{o} ×{n}" for o, n in s.operator_counts[:8]))
    elif s.operators:
        print("  Operators: " + ", ".join(f"{d.name} ({d.rounds} rds)" for d in s.operators[:8]))
    if s.maps:
        print("  Maps: " + ", ".join(f"{m} ×{n}" for m, n in s.maps[:8]))
    v = verdict(s.kd, s.win_rate, s.matches)
    if v:
        print(f"  {v}")


# ------------------------------------------------------------------ commands


async def cmd_add(settings: Settings, args) -> None:
    async with open_app(settings) as app:
        tp, result = await app.tracker.add_player(args.name, parse_platform(args.platform))
        print(f"Now tracking {tp.name} ({tp.info.platform}).")
        if result.error:
            print(f"First fetch failed: {result.error}")
        else:
            print()
            print_snapshot(app, tp)


async def cmd_remove(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        app.storage.deactivate_player(tp.id)
        print(f"Stopped tracking {tp.name} (history kept).")


async def cmd_players(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        players = app.storage.list_players()
        if not players:
            print("Nobody tracked yet. Run: r6tracker add <name>")
        for tp in players:
            checked = local(tp.last_checked_at, app.settings.tz) if tp.last_checked_at else "never"
            print(f"{tp.name:<20} {tp.info.platform:<6} last checked {checked}" + (f"  ⚠ {tp.last_error}" if tp.last_error else ""))


async def cmd_stats(settings: Settings, args) -> None:
    async with open_app(settings, online=not args.offline) as app:
        tp = app.player(args.name)
        if not args.offline:
            print_poll(await app.tracker.poll(tp), app)
            print()
        print_snapshot(app, tp)


async def cmd_operators(settings: Settings, args) -> None:
    async with open_app(settings) as app:
        tp = app.player(args.name)
        snap = app.storage.latest_snapshot(tp.id)
        season = args.season
        if season == "current":
            season = season_code(snap.season_id) if snap and snap.season_id else "all"
        ops = await app.tracker.fetcher.operators(tp.info, season, args.playlist)
        if args.side != "all":
            ops = [o for o in ops if o.side == args.side]
        ops = sort_operators(ops, args.sort, min_rounds=5 if args.sort != "rounds" else 0)
        print(f"{tp.name} — operators ({season}, {args.playlist}, sorted by {args.sort})\n")
        print(operator_table(ops, limit=args.limit) if ops else "no operator stats")
        if args.chart and ops:
            from .charts import operators_chart

            Path(args.chart).write_bytes(operators_chart(ops, f"{tp.name} — operators ({season})", limit=args.limit))
            print(f"\nChart saved to {args.chart}")


async def cmd_operator(settings: Settings, args) -> None:
    from . import operators as opmeta

    async with open_app(settings) as app:
        tp = app.player(args.player)
        snap = app.storage.latest_snapshot(tp.id)
        season = args.season
        if season == "current":
            season = season_code(snap.season_id) if snap and snap.season_id else "all"
        ops = [o for o in await app.tracker.fetcher.operators(tp.info, season, "all")
               if opmeta.normalize(o.name) == opmeta.normalize(args.operator)]
        if not ops:
            print(f"{tp.name} has no rounds on {args.operator} in {season}.")
            return
        o = max(ops, key=lambda x: x.rounds_played)
        print(f"{side_emoji(o.side)} {o.name} — {tp.name}, {season}")
        rows = [
            ("Rounds", f"{o.rounds_played} ({o.rounds_won}W {o.rounds_lost}L, {o.win_rate:.0f}%)"),
            ("Matches", f"{o.matches_played} ({o.matches_won}W {o.matches_lost}L)"),
            ("K/D", f"{o.kd:.2f} ({o.kills}/{o.deaths})"),
            ("Headshots", f"{o.headshots} ({o.headshot_pct:.0f}% of kills)"),
            ("Kills/round", f"{o.kills_per_round:.2f}"),
            ("Opening K/D", f"{o.first_bloods}/{o.first_deaths}"),
            ("Clutches", f"{o.clutches} won, {o.clutches_lost} lost"),
            ("Aces", str(o.aces)),
            ("Multi-kills", " ".join(f"{k}:{v}" for k, v in o.multi_kills.items()) or "—"),
        ]
        for k, v in rows:
            print(f"  {k:<12} {v}")


async def cmd_today(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        print_summary(f"{tp.name} today", app.tracker.today(tp, app.settings.tz), app)
        s, ongoing = app.tracker.last_session(tp)
        print()
        print_summary(f"{tp.name}'s {'current' if ongoing else 'last'} session", s, app)


async def cmd_history(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        tz = app.settings.tz
        days = app.tracker.daily(tp, args.days, tz)
        print(f"{tp.name} — last {args.days} days\n")
        print(f"{'Day':<12}{'Matches':>8}{'W':>4}{'L':>4}{'A':>4}{'K':>6}{'D':>6}{'K/D':>7}")
        for d in days:
            kd = f"{d.kd:.2f}" if d.matches else "-"
            print(f"{d.day.strftime('%a %d %b'):<12}{d.matches:>8}{d.wins:>4}{d.losses:>4}{d.abandons:>4}{d.kills:>6}{d.deaths:>6}{kd:>7}")
        if args.chart:
            from .charts import history_chart

            since = days[0].day
            rp = [(t, v) for t, v in app.tracker.rp_history(tp) if t.astimezone(tz).date() >= since]
            Path(args.chart).write_bytes(history_chart(days, rp, tp.name, tz))
            print(f"\nChart saved to {args.chart}")


async def cmd_matches(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        events = app.storage.match_events(tp.id)[-args.count:]
        if not events:
            print("No matches tracked yet.")
        for e in events:
            score = f"{e.rounds_won}-{e.rounds_lost}" if e.rounds_won or e.rounds_lost else ""
            line = (f"{local(e.detected_at, app.settings.tz)}  {e.label:<12} {e.result_text:<8} {(e.map_name or ''):<14}"
                    f"{score:>5} {e.kills:>3}/{e.deaths:<3}")
            if e.board == "ranked" and (e.rp_before or e.rp_after) and e.rp_known:
                line += f"  {e.rp_change:+d} RP"
            if e.operators:
                line += f"  {', '.join(e.operators)}"
            print(line)


async def cmd_complaints(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        s = app.storage
        counts = s.complaint_counts(tp.id)
        print(f"{tp.name}: {plural(counts['complaint'], 'complaint')}, {plural(counts['argument'], 'argument')}\n")
        for kind in ("complaint", "argument"):
            topics = s.complaint_topics(tp.id, kind)
            if topics:
                print(f"Top {kind}s:")
                for t, n in topics:
                    print(f"  {n:>3}× {t}")
        categories = s.complaint_categories(tp.id)
        if categories:
            print("Blamed most:")
            for k, n in categories:
                print(f"  {n:>3}× {COMPLAINT_CATEGORIES.get(k, k)}")
        by_result = s.complaints_by_result(tp.id)
        if by_result:
            print("When:")
        for k, v in by_result.items():
            print(f"  {v:>3}× {k}")
        recent = s.recent_complaints(tp.id, 10)
        if recent:
            print("\nLatest:")
        for c in recent:
            quote = f' — "{c.details}"' if c.details else ""
            print(f"  {local(c.created_at, app.settings.tz)} [{c.kind}] {c.topic}{quote} (logged by {c.reporter_name})")


async def cmd_complain(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.player)
        last = app.storage.last_match_event(tp.id)
        recent = last if last and utcnow() - last.detected_at <= timedelta(hours=2) else None
        c = app.storage.add_complaint(tp.id, args.kind, args.topic, args.details, category=args.category,
                                      reporter_name="terminal", match_event_id=recent.id if recent else None)
        print(f"Logged {c.kind} #{app.storage.complaint_counts(tp.id)[c.kind]} for {tp.name}: {c.topic}")


async def cmd_export(settings: Settings, args) -> None:
    async with open_app(settings, online=False) as app:
        tp = app.player(args.name)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "matches.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ended_at", "playlist", "season", "matches", "wins", "losses", "abandons", "kills",
                        "deaths", "assists", "headshots", "rounds_won", "rounds_lost", "map", "operators",
                        "rank_before", "rank_after", "rp_before", "rp_after", "match_id"])
            for e in app.storage.match_events(tp.id):
                w.writerow([e.detected_at.isoformat(), e.board, season_code(e.season_id), e.matches, e.wins, e.losses,
                            e.abandons, e.kills, e.deaths, e.assists, e.headshots, e.rounds_won, e.rounds_lost,
                            e.map_name or "", "|".join(e.operators), e.rank_before, e.rank_after, e.rp_before,
                            e.rp_after, e.match_id or ""])
        with open(out / "operators_played.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["detected_at", "operator", "side", "rounds", "rounds_won", "kills", "deaths", "headshots",
                        "aces", "clutches"])
            for ev in app.storage.operator_events(tp.id):
                d = ev.delta
                w.writerow([ev.detected_at.isoformat(), d.name, d.side, d.rounds, d.rounds_won, d.kills, d.deaths,
                            d.headshots, d.aces, d.clutches])
        with open(out / "complaints.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["created_at", "kind", "topic", "blamed", "details", "logged_by"])
            for c in reversed(app.storage.recent_complaints(tp.id, limit=1_000_000)):
                w.writerow([c.created_at.isoformat(), c.kind, c.topic, c.category or "", c.details or "",
                            c.reporter_name or ""])
        print(f"Wrote matches.csv, operators_played.csv and complaints.csv to {out}/")


async def cmd_track(settings: Settings, args) -> None:
    """Poll forever and print new matches (the tracker without Discord)."""
    async with open_app(settings) as app:
        for name, platform in settings.track_players:
            if not app.storage.find_player(name, platform):
                try:
                    tp, result = await app.tracker.add_player(name, platform)
                    print_poll(result, app)
                except SourceError as e:
                    print(f"Couldn't add {name}: {e}")
        if not app.storage.list_players():
            raise ConfigError("Nobody to track. Set TRACK_PLAYERS in .env or run: r6tracker add <name>")
        print(f"Checking {', '.join(p.name for p in app.storage.list_players())} every "
              f"{settings.poll_interval_minutes:g} min. Ctrl+C to stop.")
        while True:
            for result in await app.tracker.poll_all():
                print_poll(result, app)
            for tp in app.storage.list_players():
                session = app.tracker.finished_session(tp)
                if session:
                    print_summary(f"\n=== Session over: {tp.name} ===", session, app)
            await asyncio.sleep(settings.poll_interval_minutes * 60)


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="r6tracker", description="Rainbow Six Siege stats tracker + Discord bot")
    p.add_argument("--env", default=".env", help="path to the .env file (default: .env)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"r6tracker {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("bot", help="run the Discord bot (also does the tracking)")
    sub.add_parser("track", help="run the tracker in this terminal, without Discord")

    a = sub.add_parser("add", help="start tracking a player")
    a.add_argument("name")
    a.add_argument("--platform", default="pc", help="pc (default), psn or xbox")
    a = sub.add_parser("remove", help="stop tracking a player")
    a.add_argument("name")
    sub.add_parser("players", help="list tracked players")

    a = sub.add_parser("stats", help="fetch and show current stats")
    a.add_argument("name", nargs="?")
    a.add_argument("--offline", action="store_true", help="show the last stored stats without fetching")

    a = sub.add_parser("operators", help="operator table")
    a.add_argument("name", nargs="?")
    a.add_argument("--season", default="current", help="current (default), all, or e.g. Y11S2")
    a.add_argument("--playlist", default="all",
                   choices=["all", "ranked", "quick-match", "standard", "dual-front", "siege-cup"])
    a.add_argument("--side", default="all", choices=["all", "attacker", "defender"])
    a.add_argument("--sort", default="rounds", choices=["rounds", "kd", "winrate", "kills", "headshots"])
    a.add_argument("--limit", type=int, default=20)
    a.add_argument("--chart", metavar="FILE.png", help="also save a chart")

    a = sub.add_parser("operator", help="details for one operator")
    a.add_argument("operator")
    a.add_argument("--player")
    a.add_argument("--season", default="current")

    a = sub.add_parser("today", help="today's tracked matches and the last session")
    a.add_argument("name", nargs="?")
    a = sub.add_parser("history", help="matches and K/D per day")
    a.add_argument("name", nargs="?")
    a.add_argument("--days", type=int, default=14)
    a.add_argument("--chart", metavar="FILE.png", help="also save a chart")
    a = sub.add_parser("matches", help="latest detected matches")
    a.add_argument("name", nargs="?")
    a.add_argument("-n", "--count", type=int, default=20)

    a = sub.add_parser("complaints", help="the complaint & argument tally")
    a.add_argument("name", nargs="?")
    a = sub.add_parser("complain", help="log a complaint/argument from the terminal")
    a.add_argument("topic", help="what it was about")
    a.add_argument("--details", help="quote or details")
    a.add_argument("--kind", default="complaint", choices=["complaint", "argument"])
    a.add_argument("--category", choices=list(COMPLAINT_CATEGORIES), help="who/what got the blame")
    a.add_argument("--player")

    a = sub.add_parser("export", help="export tracked matches, operators and complaints to CSV")
    a.add_argument("name", nargs="?")
    a.add_argument("--out", default="export")
    return p


COMMANDS = {
    "add": cmd_add, "remove": cmd_remove, "players": cmd_players, "stats": cmd_stats, "operators": cmd_operators,
    "operator": cmd_operator, "today": cmd_today, "history": cmd_history, "matches": cmd_matches,
    "complaints": cmd_complaints, "complain": cmd_complain, "export": cmd_export, "track": cmd_track,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # "voice will NOT be supported": we don't do voice
    try:
        settings = Settings.from_env(args.env)
        if args.command == "bot":  # discord.py runs its own event loop
            from .bot import run_bot

            run_bot(settings)
            return 0
        asyncio.run(COMMANDS[args.command](settings, args))
        return 0
    except (ConfigError, SourceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
