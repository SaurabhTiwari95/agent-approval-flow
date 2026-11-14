"""SQLite access: one connection per thread, explicit transactions, migrations, the audit log.

Every query in this project uses `?` placeholders. Nothing user-supplied is ever put into SQL text.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(moment: dt.datetime) -> str:
    """UTC, second precision, fixed width: text comparison orders these correctly."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        """This thread's connection (opened once: opening files is slow on Windows)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; we BEGIN ourselves
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
                conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. IMMEDIATE takes the write lock up front, so check-then-write is atomic.

        SQLite allows one writer at a time. Threads of this process queue on a Python lock first: a
        profile showed SQLite's own busy-wait (sleeps of up to 100 ms) caused p99 latencies over 1 s.
        Other processes are still kept out by SQLite itself (busy_timeout)."""
        conn = self.conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.ProgrammingError:  # pragma: no cover - closed from another thread
                    pass
            self._all.clear()
        self._local = threading.local()

    # --- migrations -----------------------------------------------------------------------------

    def migrate(self) -> list[str]:
        """Apply new files from approval/migrations in order, each in its own transaction."""
        conn = self.conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = _MIGRATION_NAME.match(path.name)
            if not m or path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            # executescript can't take parameters; the name is checked against a strict pattern above.
            record = f"INSERT INTO schema_migrations VALUES ('{path.name}', '{iso(utc_now())}');"  # noqa: S608
            try:
                conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n{record}\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            applied.append(path.name)
        return applied

    def ready(self) -> bool:
        try:
            return self.one("SELECT count(*) AS n FROM schema_migrations")["n"] > 0
        except sqlite3.Error:
            return False


def audit(conn: sqlite3.Connection, now: dt.datetime, draft_id: int | None, event: str, actor: str, **detail) -> None:
    """Append one row to the audit log. The table refuses UPDATE and DELETE (triggers)."""
    conn.execute(
        "INSERT INTO audit_log (at, draft_id, event, actor, detail_json) VALUES (?, ?, ?, ?, ?)",
        (iso(now), draft_id, event, actor, json.dumps(detail, default=str, sort_keys=True)),
    )


def loads(text: str | None, default: Any = None) -> Any:
    return json.loads(text) if text else default
