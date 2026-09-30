"""Run ledger. A run row is committed as 'started' before work begins, so an interrupted
invocation remains visible as an incomplete run rather than disappearing."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .core import canonical_json, clock_source, content_hash, new_id, now, now_str, parse_utc
from .db import tx

MODE_CLASSIFICATION = {"sample": "SYNTHETIC", "replay": "RETROSPECTIVE", "live": "OBSERVED_MARKET"}


def start_run(conn: sqlite3.Connection, kind: str, mode: str, *, source_classification: str | None = None,
              declared_scope: Any = None, **fields: Any) -> str:
    run_id = new_id("run")
    cols = {
        "run_id": run_id,
        "kind": kind,
        "mode": mode,
        "source_classification": source_classification or MODE_CLASSIFICATION[mode],
        "status": "started",
        "started_at_utc": now_str(),
        "clock_source": clock_source(),
    }
    if declared_scope is not None:
        cols["declared_scope_json"] = canonical_json(declared_scope)
        cols["declared_scope_hash"] = content_hash(declared_scope)
    cols.update({k: v for k, v in fields.items() if v is not None})
    names = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    with tx(conn):
        conn.execute(f"INSERT INTO runs({names}) VALUES ({marks})", tuple(cols.values()))
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, *, summary: Any = None,
               coverage: Any = None, error: str | None = None, in_tx: bool = False, **fields: Any) -> None:
    cols: dict[str, Any] = {"status": status, "completed_at_utc": now_str()}
    if summary is not None:
        cols["summary_json"] = canonical_json(summary)
    if coverage is not None:
        cols["coverage_json"] = canonical_json(coverage)
    if error is not None:
        cols["error"] = error[:4000]
    cols.update({k: v for k, v in fields.items() if v is not None})
    sets = ",".join(f"{k} = ?" for k in cols)
    sql = f"UPDATE runs SET {sets} WHERE run_id = ?"
    if in_tx:
        conn.execute(sql, (*cols.values(), run_id))
    else:
        with tx(conn):
            conn.execute(sql, (*cols.values(), run_id))


def incomplete_runs(conn: sqlite3.Connection, older_than_seconds: int = 900) -> list[sqlite3.Row]:
    """Runs still 'started' after the threshold: interrupted or still running. Never assumed successful."""
    cutoff = now() - timedelta(seconds=older_than_seconds)
    rows = conn.execute("SELECT * FROM runs WHERE status = 'started' ORDER BY started_at_utc").fetchall()
    return [r for r in rows if parse_utc(r["started_at_utc"]) <= cutoff]
