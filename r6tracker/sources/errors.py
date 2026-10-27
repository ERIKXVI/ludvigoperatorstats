from typing import Any


class SourceError(Exception):
    """A stats source failed. The message is safe to show to users."""


class AuthError(SourceError):
    """Bad credentials / API key, or the account can't log in."""


class RateLimited(SourceError):
    """The API told us to slow down."""


class PlayerNotFound(SourceError):
    pass


def message_from(data: Any, fallback: str) -> str:
    """Best-effort error message out of an API's JSON error body."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return str(data.get("message") or err or fallback)
    return fallback
