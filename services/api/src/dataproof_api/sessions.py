"""Signed, expiring reviewer sessions and sign-in state, held in cookies.

A session names the signed-in GitHub login and carries a random CSRF token; the server keeps no
session table. Its value is ``base64url(payload).base64url(HMAC-SHA256(secret, payload))``,
checked in constant time, and it expires after a fixed lifetime. Authorization is never taken
from the session: every request re-checks the login's repository permission (``permissions``),
so a collaborator who loses access loses it within that cache's lifetime, not the session's.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

SESSION_COOKIE = "dataproof_session"
STATE_COOKIE = "dataproof_signin"
CSRF_HEADER = "X-CSRF-Token"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Session:
    login: str
    csrf: str
    expires_at: datetime


class Signer:
    """Signs small JSON payloads with an expiry, for one purpose each."""

    def __init__(self, secret: bytes, purpose: str) -> None:
        if len(secret) < 32:
            raise ValueError("the session secret must be at least 32 bytes")
        self._key = hmac.new(secret, purpose.encode(), hashlib.sha256).digest()

    def sign(self, payload: dict[str, Any], expires_at: datetime) -> str:
        body = json.dumps(
            {**payload, "exp": int(expires_at.timestamp())}, separators=(",", ":"), sort_keys=True
        ).encode()
        return f"{_b64(body)}.{_b64(hmac.new(self._key, body, hashlib.sha256).digest())}"

    def verify(self, value: str | None, now: datetime) -> dict[str, Any] | None:
        if not value or value.count(".") != 1:
            return None
        encoded, signature = value.split(".")
        try:
            body = _unb64(encoded)
            expected = hmac.new(self._key, body, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return None
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= now.timestamp():
            return None
        return payload


class Sessions:
    def __init__(self, secret: bytes, *, lifetime: timedelta = timedelta(hours=8)) -> None:
        self._sessions = Signer(secret, "session")
        self._states = Signer(secret, "sign-in state")
        self.lifetime = lifetime

    def issue(self, login: str, now: datetime) -> tuple[str, Session]:
        session = Session(login, secrets.token_urlsafe(32), now + self.lifetime)
        value = self._sessions.sign(
            {"login": session.login, "csrf": session.csrf}, session.expires_at
        )
        return value, session

    def read(self, value: str | None, now: datetime) -> Session | None:
        payload = self._sessions.verify(value, now)
        if payload is None:
            return None
        return Session(
            str(payload["login"]),
            str(payload["csrf"]),
            datetime.fromtimestamp(int(payload["exp"]), now.tzinfo),
        )

    def new_state(self, now: datetime) -> tuple[str, str]:
        """A random sign-in state and the cookie that remembers it for ten minutes."""
        state = secrets.token_urlsafe(32)
        return state, self._states.sign({"state": state}, now + timedelta(minutes=10))

    def check_state(self, cookie: str | None, state: str | None, now: datetime) -> bool:
        payload = self._states.verify(cookie, now)
        return (
            payload is not None
            and state is not None
            and hmac.compare_digest(str(payload["state"]).encode(), state.encode())
        )
