"""SQLite access: connections, transactions and migrations.

We use the standard library driver with no ORM. The schema lives in
plain SQL migration files so it can be read, reviewed and defended on
its own (see DATA-MODEL.md). Connections run in autocommit mode and every
multi-statement write goes through ``transaction()``, which nests using
savepoints.
"""

from __future__ import annotations

import itertools
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import clock

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_savepoints = itertools.count()


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        timeout=10,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        check_same_thread=False,  # a request may hop between loop and threadpool
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically. Nested calls become savepoints."""
    if conn.in_transaction:
        name = f"sp_{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return

    # IMMEDIATE takes the write lock up front, so read-check-write sequences
    # (vote budgets, deadline checks) cannot interleave with another writer.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def fetch_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def fetch_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] | dict = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def fetch_value(conn: sqlite3.Connection, sql: str, params: Iterable[Any] | dict = (), default: Any = None) -> Any:
    row = conn.execute(sql, params).fetchone()
    return default if row is None else row[0]


def new_id(prefix: str) -> str:
    """Opaque, URL-safe identifiers: ``prj_3f9a1c2b7e``.

    Identifiers imported from a bundle (``prj_07``) are kept verbatim, so
    both shapes coexist and nothing parses meaning out of an id.
    """
    return f"{prefix}_{secrets.token_hex(5)}"


def placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in filename order. Returns applied versions."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        script = path.read_text(encoding="utf-8")
        try:
            conn.executescript(
                "BEGIN;\n"
                + script
                + "\nINSERT INTO schema_migrations (version, applied_at)"
                + f" VALUES ('{version}', '{clock.now_iso()}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
