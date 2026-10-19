#!/usr/bin/env python3
"""
Read-only database access that works with both SQLite and PostgreSQL.

The check scripts in this folder need to read a database without caring which
engine it is: SQLite (a local file) during development, or PostgreSQL in a
server deployment. This module hides that difference so each check can just
call `conn.execute(sql, params)`.

Which database to use is resolved in this order:
    1. an explicit value passed in (from the --db command-line option)
    2. the DATABASE_URL environment variable
    3. a local SQLite file named "scrapai.db"

Both engines are opened strictly READ-ONLY, so a check can never modify data:
    * SQLite   — opened with a "mode=ro" URI (cannot write or create the file)
    * PostgreSQL — a read-only session

Rows come back as mapping objects, so callers use row["column"] with either
engine. SQL is written once using SQLite's "?" placeholders; for PostgreSQL the
"?" markers are translated to "%s" here, so no check has to know the difference.

PostgreSQL support needs the psycopg2 package. It is imported only when a
PostgreSQL URL is actually used, so SQLite-only users never need it installed.
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_SQLITE_FILE = "scrapai.db"


def resolve_url(explicit: str | None = None) -> str:
    """Pick the database URL: explicit arg → $DATABASE_URL → local sqlite file."""
    if explicit:
        return explicit
    env = os.getenv("DATABASE_URL")
    if env:
        return env
    return DEFAULT_SQLITE_FILE


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql://") or url.startswith("postgres://")


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite:///") or url.startswith("sqlite://")


def _sqlite_path(url: str) -> str:
    """Extract a filesystem path from a value that may be a bare path or a
    sqlite:/// URL (sqlite:///rel.db or sqlite:////abs.db)."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url


# --------------------------------------------------------------------------- #
# A tiny uniform surface: connect_ro(...).execute(sql, params) -> rows that
# support both iteration and .fetchall(), with r["column"] access on each row.
# --------------------------------------------------------------------------- #


class _Result:
    """Wraps a cursor's already-fetched rows; iterable and .fetchall()-able."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SqliteConn:
    def __init__(self, path: str):
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params=None) -> _Result:
        cur = self._conn.execute(sql, list(params) if params else [])
        return _Result(cur.fetchall())

    def close(self):
        self._conn.close()


class _PostgresConn:
    def __init__(self, url: str):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as e:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "PostgreSQL target requires psycopg2 (pip install psycopg2-binary). "
                f"DATABASE_URL points at Postgres but the driver is missing: {e}"
            ) from e
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._conn = psycopg2.connect(url)
        # Enforce read-only at the session level: the checker can never write.
        self._conn.set_session(readonly=True, autocommit=True)

    def execute(self, sql: str, params=None) -> _Result:
        # Checks are authored in SQLite's "?" placeholder dialect; psycopg2
        # wants "%s". Translate here so no check has to branch on backend.
        pg_sql = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=self._extras.RealDictCursor)
        cur.execute(pg_sql, list(params) if params else None)
        rows = cur.fetchall()  # list[RealDictRow]; r["column"] works
        cur.close()
        return _Result(rows)

    def close(self):
        self._conn.close()


def connect_ro(db: str | None = None):
    """Open a read-only connection to the resolved database.

    `db` may be: None (use $DATABASE_URL or the sqlite fallback), a sqlite URL,
    a bare sqlite file path, or a postgresql:// URL.
    """
    url = resolve_url(db)
    if _is_postgres(url):
        return _PostgresConn(url)
    # sqlite URL or bare path
    return _SqliteConn(_sqlite_path(url) if _is_sqlite_url(url) else url)
