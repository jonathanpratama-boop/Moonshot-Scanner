"""Persistent work queue with leases: rechecks, expiries and the research backlog.

Claiming is atomic (conditional UPDATE inside BEGIN IMMEDIATE), so two workers never
process the same item. Each item is processed and marked done in one transaction; a
crash before commit leaves the lease to expire, after which another run reclaims it
(the attempt is counted and recorded). Items exceeding max_attempts become 'dead' and
stay visible as failures.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Callable

from .core import AstraError, canonical_json, fmt_utc, new_id, now, now_str
from .db import tx


def schedule(conn: sqlite3.Connection, *, kind: str, due_at: str, priority: str, dedup_key: str,
             candidate_id: str | None = None, condition: str | None = None, max_attempts: int = 3) -> bool:
    """Insert once per dedup key (caller holds the transaction)."""
    ts = now_str()
    cur = conn.execute(
        "INSERT OR IGNORE INTO work_items(work_id,dedup_key,kind,priority,candidate_id,due_at_utc,condition,status,"
        "attempts,max_attempts,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,'pending',0,?,?,?)",
        (new_id("wk"), dedup_key, kind, priority, candidate_id, due_at, condition, max_attempts, ts, ts),
    )
    return cur.rowcount == 1


def cancel_pending(conn: sqlite3.Connection, candidate_id: str, kind: str, reason: str, keep_dedup: str | None = None) -> int:
    cur = conn.execute(
        "UPDATE work_items SET status='cancelled', last_error=?, updated_at_utc=? "
        "WHERE candidate_id=? AND kind=? AND status='pending' AND dedup_key IS NOT ?",
        (reason, now_str(), candidate_id, kind, keep_dedup),
    )
    return cur.rowcount


def claim(conn: sqlite3.Connection, worker: str, kinds: tuple[str, ...], limit: int, lease_seconds: int,
          priority: str | None = None) -> list[sqlite3.Row]:
    t = now_str()
    lease_until = fmt_utc(now() + timedelta(seconds=lease_seconds))
    marks = ",".join("?" for _ in kinds)
    pri_sql, pri_args = ("AND priority = ?", (priority,)) if priority else ("", ())
    claimed = []
    with tx(conn):
        rows = conn.execute(
            f"SELECT * FROM work_items WHERE kind IN ({marks}) {pri_sql} AND "
            "((status='pending' AND due_at_utc <= ?) OR (status='leased' AND lease_expires_at_utc < ?)) "
            "ORDER BY priority='urgent' DESC, due_at_utc LIMIT ?",
            (*kinds, *pri_args, t, t, limit),
        ).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE work_items SET status='leased', lease_owner=?, lease_expires_at_utc=?, attempts=attempts+1, "
                "updated_at_utc=? WHERE work_id=? AND ((status='pending' AND due_at_utc <= ?) OR "
                "(status='leased' AND lease_expires_at_utc < ?))",
                (worker, lease_until, t, r["work_id"], t, t),
            )
            if cur.rowcount == 1:
                detail = (f"recovered expired lease held by {r['lease_owner']}" if r["status"] == "leased" else "claimed")
                conn.execute("INSERT INTO work_attempts VALUES (?,?,?,?,?,?,?)",
                             (new_id("wat"), r["work_id"], worker, t, "claimed", detail, None))
                claimed.append(conn.execute("SELECT * FROM work_items WHERE work_id=?", (r["work_id"],)).fetchone())
    return claimed


def process(conn: sqlite3.Connection, item: sqlite3.Row, worker: str, handler: Callable[[sqlite3.Connection, sqlite3.Row], dict],
            run_id: str | None = None) -> str:
    """Run handler and mark the item done in one transaction; on error record and retry/dead-letter."""
    try:
        with tx(conn):
            still = conn.execute(
                "SELECT 1 FROM work_items WHERE work_id=? AND status='leased' AND lease_owner=?",
                (item["work_id"], worker),
            ).fetchone()
            if not still:
                raise LeaseLost(f"lease on {item['work_id']} no longer held by {worker}")
            result = handler(conn, item)
            conn.execute(
                "UPDATE work_items SET status='done', result_json=?, lease_owner=NULL, lease_expires_at_utc=NULL, "
                "updated_at_utc=? WHERE work_id=?",
                (canonical_json(result), now_str(), item["work_id"]),
            )
            conn.execute("INSERT INTO work_attempts VALUES (?,?,?,?,?,?,?)",
                         (new_id("wat"), item["work_id"], worker, now_str(), "done", canonical_json(result), run_id))
        return "done"
    except LeaseLost:
        return "lease_lost"
    except Exception as exc:  # recorded, never swallowed silently
        err = f"{type(exc).__name__}: {exc}"
        with tx(conn):
            row = conn.execute("SELECT attempts, max_attempts FROM work_items WHERE work_id=?", (item["work_id"],)).fetchone()
            status = "dead" if row["attempts"] >= row["max_attempts"] else "pending"
            conn.execute(
                "UPDATE work_items SET status=?, last_error=?, lease_owner=NULL, lease_expires_at_utc=NULL, updated_at_utc=? "
                "WHERE work_id=?",
                (status, err[:2000], now_str(), item["work_id"]),
            )
            conn.execute("INSERT INTO work_attempts VALUES (?,?,?,?,?,?,?)",
                         (new_id("wat"), item["work_id"], worker, now_str(), "failed", err[:2000], run_id))
        return "dead" if status == "dead" else "failed_will_retry"


class LeaseLost(AstraError):
    pass
