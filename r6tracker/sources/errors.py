from typing import Any


class SourceError(Exception):
    """A stats source failed. The message is safe to show to users."""


class AuthError(SourceError):
    """Bad credentials / API key, or the account can't log in."""


class RateLimited(SourceError):
    """The API told us to slow down."""


class PlayerNotFound(SourceError):
    pass


class EndpointUnavailable(SourceError):
    """This source doesn't offer that kind of data (e.g. no match history); use a fallback."""


def message_from(data: Any, fallback: str) -> str:
    """Best-effort error message out of an API's JSON error body."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        errors = data.get("errors")  # Tracker Network: {"errors": [{"code": ..., "message": ...}]}
        if not err and isinstance(errors, list) and errors and isinstance(errors[0], dict):
            err = errors[0].get("message") or errors[0].get("code")
        return str(data.get("message") or err or fallback)
    return fallback
