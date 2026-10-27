"""Text helpers shared by the CLI and the Discord bot."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from .models import BoardStats, MatchBatch, OperatorStats
from .ranks import rank_name

SIDE_EMOJI = {"attacker": "⚔️", "defender": "🛡️"}


def num(n: int | float | None) -> str:
    return "—" if n is None else f"{n:,}"


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


def hours(seconds: int | None) -> str:
    return "—" if not seconds else f"{seconds / 3600:,.0f} h"


def signed(n: int) -> str:
    return f"+{n:,}" if n > 0 else (f"−{-n:,}" if n < 0 else "±0")


def duration(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def side_emoji(side: str | None) -> str:
    return SIDE_EMOJI.get(side or "", "▫️")


def local(dt: datetime, tz: tzinfo, fmt: str = "%d %b %H:%M") -> str:
    return dt.astimezone(tz).strftime(fmt)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def wl(wins: int, losses: int, abandons: int = 0) -> str:
    s = f"{wins:,}W {losses:,}L"
    return s + (f" {abandons:,}A" if abandons else "")


def board_line(b: BoardStats) -> str:
    return (f"{plural(b.matches, 'match', 'matches')} · {wl(b.wins, b.losses, b.abandons)} ({b.win_rate:.0f}%) · "
            f"K/D {b.kd:.2f} ({b.kills:,}/{b.deaths:,})")


def rank_line(b: BoardStats) -> str:
    s = f"**{rank_name(b.rank)}** · {b.rank_points:,} RP"
    if b.max_rank and (b.max_rank != b.rank or b.max_rank_points != b.rank_points):
        s += f" (peak {rank_name(b.max_rank)}, {b.max_rank_points:,} RP)"
    if b.top_rank_position:
        s += f" · #{b.top_rank_position:,} on the leaderboard"
    return s


def rank_change(b: MatchBatch) -> str:
    before, after = rank_name(b.rank_before), rank_name(b.rank_after)
    move = f"{before} → **{after}**" if before != after else f"**{after}**"
    if not b.rp_known:
        return f"{move} ({b.rp_after:,} RP)"
    return f"{move} ({signed(b.rp_change)} RP, now {b.rp_after:,})"


def verdict(kd: float, win_rate: float, matches: int) -> str:
    if matches == 0:
        return ""
    if kd >= 1.5 and win_rate >= 60:
        return "🔥 Absolutely cooking."
    if kd >= 1.1 and win_rate >= 50:
        return "💪 Solid."
    if kd < 0.8 and win_rate < 40:
        return "💀 Rough one. Complaints department is open."
    if kd < 1.0 and win_rate < 50:
        return "📉 Not the finest hour."
    return "😐 Mid."


def table(headers: list[str], rows: list[list[str]], align: str) -> str:
    """Monospace table; align is one char per column: l or r."""
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]

    def fmt(cells: list[str]) -> str:
        out = []
        for c, w, a in zip(cells, widths, align):
            out.append(str(c).rjust(w) if a == "r" else str(c).ljust(w))
        return "  ".join(out).rstrip()

    return "\n".join([fmt(headers), *(fmt(r) for r in rows)])


OPERATOR_SORTS = {
    "rounds": lambda o: o.rounds_played,
    "kd": lambda o: o.kd,
    "winrate": lambda o: o.win_rate,
    "kills": lambda o: o.kills,
    "headshots": lambda o: o.headshot_pct,
}


def sort_operators(ops: list[OperatorStats], by: str = "rounds", min_rounds: int = 0) -> list[OperatorStats]:
    key = OPERATOR_SORTS.get(by, OPERATOR_SORTS["rounds"])
    pool = [o for o in ops if o.rounds_played >= min_rounds] or ops
    return sorted(pool, key=lambda o: (key(o), o.rounds_played), reverse=True)


def operator_table(ops: list[OperatorStats], limit: int = 15) -> str:
    rows = [
        [o.name[:11], str(o.rounds_played), f"{o.win_rate:.0f}", f"{o.kd:.2f}", f"{o.headshot_pct:.0f}"]
        for o in ops[:limit]
    ]
    return table(["Operator", "Rds", "Win%", "K/D", "HS%"], rows, "lrrrr")
