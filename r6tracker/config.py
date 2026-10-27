"""Settings, read from environment variables (and a .env file if present)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PLATFORM_ALIASES = {
    "pc": "uplay", "uplay": "uplay", "ubisoft": "uplay", "ubi": "uplay",
    "psn": "psn", "ps": "psn", "ps4": "psn", "ps5": "psn", "playstation": "psn",
    "xbl": "xbl", "xbox": "xbl",
}


class ConfigError(Exception):
    pass


def parse_platform(value: str | None) -> str:
    key = (value or "uplay").strip().lower()
    if key not in PLATFORM_ALIASES:
        raise ConfigError(f"Unknown platform '{value}'. Use pc, psn or xbox.")
    return PLATFORM_ALIASES[key]


def parse_players(value: str | None) -> list[tuple[str, str]]:
    """'Ludvig, SomeoneElse:psn' -> [('Ludvig', 'uplay'), ('SomeoneElse', 'psn')]"""
    players = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, platform = item.partition(":")
        players.append((name.strip(), parse_platform(platform or None)))
    return players


def _int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got '{raw}'") from e


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got '{raw}'") from e


@dataclass
class Settings:
    discord_token: str | None = None
    discord_guild_id: int | None = None
    notify_channel_id: int | None = None
    trn_api_key: str | None = None
    arenyze_api_key: str | None = None
    track_players: list[tuple[str, str]] = field(default_factory=list)
    poll_interval_minutes: float = 5.0
    session_gap_minutes: int = 45
    operator_refresh_minutes: int = 60
    data_dir: Path = Path("data")
    timezone: str = "UTC"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        s = cls(
            discord_token=os.getenv("DISCORD_TOKEN") or None,
            discord_guild_id=_int("DISCORD_GUILD_ID", None),
            notify_channel_id=_int("NOTIFY_CHANNEL_ID", None),
            trn_api_key=os.getenv("TRN_API_KEY") or None,
            arenyze_api_key=os.getenv("ARENYZE_API_KEY") or None,
            track_players=parse_players(os.getenv("TRACK_PLAYERS")),
            poll_interval_minutes=max(_float("POLL_INTERVAL_MINUTES", 5.0), 1.0),
            session_gap_minutes=_int("SESSION_GAP_MINUTES", 45) or 45,
            operator_refresh_minutes=_int("OPERATOR_REFRESH_MINUTES", 60) or 60,
            data_dir=Path(os.getenv("DATA_DIR") or "data"),
            timezone=os.getenv("TIMEZONE") or "UTC",
        )
        s.tz  # validate early
        if os.getenv("UBISOFT_EMAIL") or os.getenv("UBISOFT_PASSWORD"):
            logging.getLogger(__name__).warning(
                "UBISOFT_EMAIL / UBISOFT_PASSWORD aren't used any more (stats come from R6 Tracker). "
                "Remove them from .env, and delete %s if it exists.", s.data_dir / "ubisoft_session.json"
            )
        return s

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"Unknown TIMEZONE '{self.timezone}' (use e.g. Europe/Stockholm)") from e

    @property
    def db_path(self) -> Path:
        return self.data_dir / "r6tracker.sqlite3"

    @property
    def has_trn(self) -> bool:
        return bool(self.trn_api_key)

    @property
    def has_arenyze(self) -> bool:
        return bool(self.arenyze_api_key)

    def check_sources(self) -> None:
        if not (self.has_trn or self.has_arenyze):
            raise ConfigError(
                "No stats source configured. Set TRN_API_KEY (R6 Tracker) in your .env file "
                "(see .env.example)."
            )
