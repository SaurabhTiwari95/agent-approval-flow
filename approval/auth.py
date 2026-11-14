"""Approver sign-in: argon2id password hashes, server-side sessions, CSRF tokens, login throttling.

- Accounts come from the APPROVERS setting (username -> name, argon2 hash, role, optional limit).
- A session is a random token in an HttpOnly cookie; only its SHA-256 is stored, with an expiry.
- Each session has its own CSRF token, required on every POST.
- Failed sign-ins are counted per IP and per username; past LOGIN_MAX_FAILURES in
  LOGIN_WINDOW_MINUTES, sign-in is refused without even checking the password.
- Passwords are never logged or stored; unknown usernames take as long as known ones.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from approval.config import Settings
from approval.db import Database, iso, utc_now
from approval.drafts import User

_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))  # equal work for unknown usernames
MAX_PASSWORD_CHARS = 256


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottled(Exception):
    pass


@dataclass(frozen=True)
class Session:
    user: User
    csrf_token: str

    def csrf_ok(self, submitted: str | None) -> bool:
        return bool(submitted) and hmac.compare_digest(self.csrf_token, submitted)


class Auth:
    def __init__(self, settings: Settings, db: Database, clock=utc_now):
        self.s, self.db, self.clock = settings, db, clock

    def user(self, username: str) -> User | None:
        a = self.s.approvers.get(username)
        return User(username, a.name, a.role, a.limit) if a else None

    def _window_start(self, now: dt.datetime) -> str:
        return iso(now - dt.timedelta(minutes=self.s.login_window_minutes))

    def throttled(self, ip: str, username: str) -> bool:
        by_ip, by_user = f"ip:{ip}", f"user:{username}"
        row = self.db.one(
            "SELECT SUM(key = ?) AS by_ip, SUM(key = ?) AS by_user FROM login_failures WHERE key IN (?, ?) AND at > ?",
            (by_ip, by_user, by_ip, by_user, self._window_start(self.clock())),
        )
        limit = self.s.login_max_failures
        return (row["by_ip"] or 0) >= limit or (row["by_user"] or 0) >= limit

    def login(self, username: str, password: str, ip: str) -> tuple[str, Session] | None:
        """Returns (cookie token, session) or None. Raises LoginThrottled when over the limit."""
        username = username.strip().lower()[:64]
        if self.throttled(ip, username):
            raise LoginThrottled()
        account = self.s.approvers.get(username)
        ok = verify_password(account.hash if account else _DUMMY_HASH, password[:MAX_PASSWORD_CHARS])
        now = self.clock()
        if not (ok and account):
            with self.db.tx() as conn:
                conn.executemany(
                    "INSERT INTO login_failures (key, at) VALUES (?, ?)",
                    [(f"ip:{ip}", iso(now)), (f"user:{username}", iso(now))],
                )
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, username, csrf_token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _token_hash(token),
                    username,
                    csrf,
                    iso(now),
                    iso(now + dt.timedelta(minutes=self.s.session_ttl_minutes)),
                ),
            )
            conn.execute("DELETE FROM login_failures WHERE key = ?", (f"user:{username}",))
        return token, Session(self.user(username), csrf)

    def session(self, token: str | None) -> Session | None:
        if not token or len(token) > 100:
            return None
        row = self.db.one(
            "SELECT username, csrf_token FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (_token_hash(token), iso(self.clock())),
        )
        if row is None:
            return None
        user = self.user(row["username"])  # removed from APPROVERS: the session stops working at once
        return Session(user, row["csrf_token"]) if user else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def cleanup(self) -> int:
        """Delete expired sessions and old login failures."""
        now = self.clock()
        with self.db.tx() as conn:
            a = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(now),)).rowcount
            b = conn.execute("DELETE FROM login_failures WHERE at <= ?", (self._window_start(now),)).rowcount
        return a + b
