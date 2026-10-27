"""PNG charts (matplotlib) for Discord and the CLI. Dark theme, since that's what Discord mostly is."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, tzinfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.offsetbox import AnnotationBbox, OffsetImage  # noqa: E402

from . import operators as opmeta  # noqa: E402
from .models import OperatorStats  # noqa: E402
from .tracker import DayStats  # noqa: E402

# Validated dark palette (categorical slots 1-3) and chart chrome.
SURFACE = "#1a1a19"
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
BASELINE = "#383835"
BLUE, ORANGE, AQUA = "#3987e5", "#d95926", "#199e70"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def history_chart(
    days: list[DayStats], rp: list[tuple[datetime, int]], player: str, tz: tzinfo
) -> bytes:
    """Matches per day (W/L/abandoned), K/D per day, and ranked RP over time, as stacked panels."""
    panels = 3 if rp else 2
    fig, axes = plt.subplots(
        panels, 1, figsize=(9, 2.6 * panels + 0.6), facecolor=SURFACE,
        gridspec_kw={"height_ratios": [1.3, 1] + ([1] if rp else [])},
    )
    fig.suptitle(f"{player} — last {len(days)} days", color=INK, fontsize=13, x=0.01, ha="left")
    x = [datetime(d.day.year, d.day.month, d.day.day, tzinfo=tz) for d in days]
    width = 0.8

    ax = axes[0]
    _style(ax)
    wins = [d.wins for d in days]
    losses = [d.losses for d in days]
    aband = [d.abandons for d in days]
    ax.bar(x, wins, width, color=BLUE, label="Wins", edgecolor=SURFACE, linewidth=1)
    ax.bar(x, losses, width, bottom=wins, color=ORANGE, label="Losses", edgecolor=SURFACE, linewidth=1)
    if any(aband):
        ax.bar(x, aband, width, bottom=[w + l for w, l in zip(wins, losses)], color=AQUA, label="Abandoned",
               edgecolor=SURFACE, linewidth=1)
    for xi, d in zip(x, days):
        if d.matches:
            ax.annotate(str(d.matches), (xi, d.matches), xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", color=INK_2, fontsize=8)
    ax.set_title("Matches per day", loc="left", fontsize=10, color=INK_2)
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.set_ylim(0, max([d.matches for d in days] + [1]) * 1.2)
    ax.legend(loc="upper left", frameon=False, fontsize=8, ncol=3, labelcolor=INK_2)

    ax = axes[1]
    _style(ax)
    kd = [d.kd if d.matches else float("nan") for d in days]
    ax.axhline(1.0, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.plot(x, kd, color=BLUE, linewidth=2, marker="o", markersize=5, markeredgecolor=SURFACE)
    ax.set_title("K/D per day (dashed line = 1.0)", loc="left", fontsize=10, color=INK_2)
    ax.set_ylim(bottom=0)

    for a in axes[:2]:
        a.set_xlim(x[0] - timedelta(hours=14), x[-1] + timedelta(hours=14))
        a.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=tz))
        a.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=10, tz=tz))

    if rp:
        ax = axes[2]
        _style(ax)
        t, v = zip(*rp)
        ax.plot(t, v, color=BLUE, linewidth=2, marker="o", markersize=4, markeredgecolor=SURFACE)
        ax.set_title("Ranked RP after each tracked match", loc="left", fontsize=10, color=INK_2)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=tz))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8, tz=tz))
        ax.annotate(f"{v[-1]:,} RP", (t[-1], v[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", color=INK_2, fontsize=8)

    fig.tight_layout()
    return _png(fig)


def operators_chart(ops: list[OperatorStats], title: str, limit: int = 10) -> bytes:
    """Horizontal bars of rounds played per operator, with the operator icons."""
    ops = sorted(ops, key=lambda o: -o.rounds_played)[:limit]
    n = max(len(ops), 1)
    fig, ax = plt.subplots(figsize=(8, 0.55 * n + 1.1), facecolor=SURFACE)
    _style(ax)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_color(BASELINE)
    ax.spines["left"].set_visible(True)

    y = list(range(len(ops)))[::-1]
    rounds = [o.rounds_played for o in ops]
    ax.barh(y, rounds, height=0.62, color=BLUE)
    top = max(rounds + [1])
    for yi, o in zip(y, ops):
        ax.annotate(
            f"{o.rounds_played} rds · K/D {o.kd:.2f} · {o.win_rate:.0f}% won",
            (o.rounds_played, yi), xytext=(6, 0), textcoords="offset points", va="center", color=INK_2, fontsize=8,
        )
        icon = opmeta.icon_path(o.name)
        if icon:
            img = plt.imread(str(icon))
            ab = AnnotationBbox(OffsetImage(img, zoom=0.17), (0, yi), xybox=(-16, 0), boxcoords="offset points",
                                frameon=False, annotation_clip=False)
            ax.add_artist(ab)
    ax.set_yticks(y, [o.name for o in ops], color=INK, fontsize=9)
    ax.tick_params(axis="y", pad=34)
    ax.set_xlim(0, top * 1.55)
    ax.set_title(title, loc="left", fontsize=12, color=INK)
    ax.set_xlabel("Rounds played", color=MUTED, fontsize=9)
    fig.tight_layout()
    return _png(fig)
