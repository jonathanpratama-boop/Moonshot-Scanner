"""Due-work processing and the collection cycle.

Order follows ASTRA-OPS-20260929-r1 run priority:
  1. urgent due work (expiries, urgent rechecks)
  2. reserved discovery sweeps: disclosure AND anomaly collection (cycle only)
  3. routine rechecks
  4. outcome measurement
  5. research backlog (only through an explicitly enabled AI adapter; otherwise left queued)
Collection never waits on research, and each step's failure is recorded without
stopping the independent steps.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import candidates as cands
from . import notifications, outcomes, work
from .config import settings
from .core import AstraError, now_str
from .db import db_mode
from .runs import finish_run, start_run


def _handle(conn: sqlite3.Connection, item: sqlite3.Row) -> dict:
    kind = item["kind"]
    cand = cands.get(conn, item["candidate_id"]) if item["candidate_id"] else None
    if cand is None:
        return {"skipped": "no candidate"}
    if cand["state"] not in cands.OPEN_STATES:
        return {"skipped": f"candidate {cand['state']}"}
    if kind == "expiry":
        if cand["expires_at_utc"] != item["due_at_utc"]:
            return {"skipped": "expiry superseded", "current_expiry": cand["expires_at_utc"]}
        tid = cands.transition(
            conn, cand["candidate_id"], "EXPIRED", actor=f"process_due:{item['lease_owner']}",
            reason=f"expiry reached: {cand['expiry_condition']}", reason_type="EXPIRY",
            evidence={"work_id": item["work_id"], "expires_at_utc": cand["expires_at_utc"], "processed_at_utc": now_str()},
        )
        return {"expired": cand["candidate_id"], "transition_id": tid}
    if kind == "recheck":
        cands.add_event(conn, cand["candidate_id"], "recheck_due_processed", f"process_due:{item['lease_owner']}",
                        {"work_id": item["work_id"], "due_at_utc": item["due_at_utc"], "condition": item["condition"]})
        work.schedule(conn, kind="research_request", candidate_id=cand["candidate_id"], due_at=now_str(),
                      priority=item["priority"], dedup_key=f"research_request:recheck:{item['work_id']}",
                      condition=f"recheck due: {item['condition']}")
        conn.execute(
            "UPDATE candidates SET next_recheck_at_utc=NULL, next_recheck_condition=?, research_status='QUEUED', "
            "updated_at_utc=?, version=version+1 WHERE candidate_id=?",
            (f"UNKNOWN: recheck ({item['condition']}) processed {now_str()}; awaiting research update", now_str(),
             cand["candidate_id"]),
        )
        notifications.queue_for_candidate(conn, cand["candidate_id"], kind="recheck_due", dedup_suffix=item["work_id"],
                                          priority=item["priority"], note=f"recheck due: {item['condition']}")
        return {"recheck_processed": cand["candidate_id"], "research_requested": True}
    raise AstraError(f"no handler for work kind {kind}")


def run_due(conn: sqlite3.Connection, run_id: str, worker: str, priority: str | None, limit: int) -> dict:
    counts: dict[str, int] = {}
    items = work.claim(conn, worker, ("expiry", "recheck"), limit, settings().lease_seconds, priority=priority)
    for it in items:
        outcome = work.process(conn, it, worker, _handle, run_id=run_id)
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"claimed": len(items), **counts}


def process_due(conn: sqlite3.Connection, *, worker: str | None = None, limit: int = 100, with_ai: bool = False,
                collect: Any = None) -> dict[str, Any]:
    """Process due work. ``collect`` is an optional callable run between urgent and routine work."""
    mode = db_mode(conn)
    worker = worker or settings().worker_id
    run_id = start_run(conn, "process_due" if collect is None else "cycle", mode, source_classification="INTERNAL",
                       worker_id=worker)
    summary: dict[str, Any] = {"worker": worker}
    errors = []
    try:
        summary["1_urgent"] = run_due(conn, run_id, worker, "urgent", limit)
    except Exception as exc:
        errors.append(f"urgent: {type(exc).__name__}: {exc}")
    if collect is not None:
        try:
            summary["2_collection"] = collect()
        except Exception as exc:
            errors.append(f"collection: {type(exc).__name__}: {exc}")
    try:
        summary["3_routine"] = run_due(conn, run_id, worker, None, limit)
    except Exception as exc:
        errors.append(f"routine: {type(exc).__name__}: {exc}")
    try:
        summary["4_outcomes"] = outcomes.measure_all(conn, run_id)
    except Exception as exc:
        errors.append(f"outcomes: {type(exc).__name__}: {exc}")
    try:
        summary["5_research_backlog"] = research_backlog(conn, with_ai=with_ai)
    except Exception as exc:
        errors.append(f"research_backlog: {type(exc).__name__}: {exc}")
    summary["errors"] = errors
    finish_run(conn, run_id, "completed" if not errors else "partial", summary=summary,
               error="; ".join(errors) if errors else None)
    return {"run_id": run_id, **summary}


def research_backlog(conn: sqlite3.Connection, with_ai: bool) -> dict:
    pending = conn.execute(
        "SELECT count(*) AS n FROM work_items WHERE kind='research_request' AND status='pending'").fetchone()["n"]
    if not with_ai:
        return {"pending_research_requests": pending, "ai": "not invoked (manual research import is the required path)"}
    from .ai import run_backlog

    return {"pending_research_requests": pending, "ai": run_backlog(conn)}


def backlog_summary(conn: sqlite3.Connection) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    t = now_str()
    return {
        "research_requests_pending": q("SELECT count(*) FROM work_items WHERE kind='research_request' AND status='pending'"),
        "due_now": q("SELECT count(*) FROM work_items WHERE status='pending' AND kind IN ('recheck','expiry') AND due_at_utc<=?", t),
        "scheduled_future": q("SELECT count(*) FROM work_items WHERE status='pending' AND kind IN ('recheck','expiry') AND due_at_utc>?", t),
        "leased": q("SELECT count(*) FROM work_items WHERE status='leased'"),
        "dead": q("SELECT count(*) FROM work_items WHERE status='dead'"),
        "retrying": q("SELECT count(*) FROM work_items WHERE status='pending' AND last_error IS NOT NULL AND attempts>0"),
    }
