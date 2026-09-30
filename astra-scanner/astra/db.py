"""SQLite connection, migrations and transaction helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

from .core import AstraError, now_str, sha256_text

VALID_MODES = ("sample", "replay", "live")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception. Not re-entrant."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files() -> list[tuple[str, str]]:
    pkg = resources.files("astra") / "migrations"
    out = []
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".sql"):
            out.append((entry.name.split("_", 1)[0], entry.read_text(encoding="utf-8")))
    return out


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in order. Refuses if an applied migration file changed."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at_utc TEXT NOT NULL)"
    )
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM schema_migrations")}
    newly = []
    for version, sql in _migration_files():
        checksum = sha256_text(sql)
        if version in applied:
            if applied[version] != checksum:
                raise AstraError(
                    f"migration {version} changed after it was applied (checksum mismatch); "
                    "add a new migration instead of editing an applied one"
                )
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_utc) VALUES (?,?,?)",
                (version, checksum, now_str()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        newly.append(version)
    return newly


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies intact."""
    statements, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        candidate = "\n".join(buf)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    if "".join(buf).strip():
        raise AstraError("incomplete SQL statement at end of migration")
    return statements


def init_db(path: str | Path, mode: str) -> sqlite3.Connection:
    if mode not in VALID_MODES:
        raise AstraError(f"mode must be one of {VALID_MODES}")
    conn = connect(path)
    migrate(conn)
    existing = get_meta(conn, "db_mode")
    if existing and existing != mode:
        raise AstraError(
            f"database already initialised in mode {existing!r}; use a separate database file for {mode!r}"
        )
    if not existing:
        with tx(conn):
            conn.execute("INSERT INTO meta(key, value) VALUES ('db_mode', ?)", (mode,))
            conn.execute("INSERT INTO meta(key, value) VALUES ('created_at_utc', ?)", (now_str(),))
    return conn


def open_db(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise AstraError(f"database {p} does not exist; run `astra init --mode sample|replay|live` first")
    conn = connect(p)
    migrate(conn)
    if not get_meta(conn, "db_mode"):
        raise AstraError("database has no mode; run `astra init`")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def db_mode(conn: sqlite3.Connection) -> str:
    mode = get_meta(conn, "db_mode")
    if mode not in VALID_MODES:
        raise AstraError("database mode not set")
    return mode
