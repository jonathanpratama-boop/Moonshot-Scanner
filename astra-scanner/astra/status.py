"""Operational status shared by the CLI and the dashboard (read-only queries)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import settings
from .core import now_str, to_zone_str
from .db import get_meta
from .processing import backlog_summary
from .runs import incomplete_runs

COLLECTION_KINDS = ("bar_import", "announcement_import", "sec_collection", "anomaly_scan", "replay")


def times(utc_s: str | None) -> dict:
    if not utc_s:
        return {"utc": "UNKNOWN"}
    return {"utc": utc_s, **{z: to_zone_str(utc_s, z) for z in settings().display_timezones}}


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    mode = get_meta(conn, "db_mode")
    has_sample = any(conn.execute(q).fetchone()[0] for q in (
        "SELECT count(*) FROM datasets WHERE is_sample=1", "SELECT count(*) FROM sources WHERE is_sample=1",
        "SELECT count(*) FROM research_records WHERE is_sample=1"))
    collections = {}
    for kind in COLLECTION_KINDS:
        last_ok = conn.execute("SELECT * FROM runs WHERE kind=? AND status IN ('completed','partial') "
                               "ORDER BY completed_at_utc DESC LIMIT 1", (kind,)).fetchone()
        last_any = conn.execute("SELECT * FROM runs WHERE kind=? ORDER BY started_at_utc DESC LIMIT 1", (kind,)).fetchone()
        collections[kind] = {
            "last_success": dict(last_ok) if last_ok else None,
            "last_attempt_status": last_any["status"] if last_any else None,
            "last_attempt_error": last_any["error"] if last_any else None,
        }
    scan = conn.execute("SELECT * FROM runs WHERE kind IN ('anomaly_scan','replay') AND coverage_json IS NOT NULL "
                        "ORDER BY started_at_utc DESC LIMIT 1").fetchone()
    scan_cov = json.loads(scan["coverage_json"]) if scan else None
    if scan is None:
        coverage_message = "NOT_SCANNED: no completed anomaly scan exists"
    else:
        coverage_message = scan_cov.get("headline")
    failures = [dict(r) for r in conn.execute(
        "SELECT run_id, kind, status, started_at_utc, error FROM runs WHERE status IN ('failed','partial') "
        "ORDER BY started_at_utc DESC LIMIT 20")]
    scope_failures = [dict(r) for r in conn.execute(
        "SELECT scope_key, last_error, consecutive_failures, last_success_at_utc FROM source_scopes WHERE consecutive_failures > 0")]
    states = {r["state"]: r["n"] for r in conn.execute("SELECT state, count(*) AS n FROM candidates GROUP BY state")}
    notif = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM notifications GROUP BY status")}
    outc = {r["status"]: r["n"] for r in conn.execute(
        "SELECT o.status, count(*) AS n FROM outcome_observations o WHERE o.rowid = (SELECT o2.rowid FROM outcome_observations o2 "
        "WHERE o2.subject_id=o.subject_id AND o2.horizon_sessions=o.horizon_sessions ORDER BY o2.observed_at_utc DESC, o2.rowid DESC LIMIT 1) "
        "GROUP BY o.status")}
    dead = [dict(r) for r in conn.execute("SELECT work_id, kind, candidate_id, attempts, last_error FROM work_items WHERE status='dead'")]
    return {
        "generated_at": times(now_str()),
        "mode": mode,
        "sample_data_present": bool(has_sample) or mode == "sample",
        "collections": collections,
        "latest_scan": {"run": dict(scan) if scan else None, "coverage": scan_cov, "message": coverage_message},
        "incomplete_runs": [dict(r) for r in incomplete_runs(conn)],
        "recent_failures": failures,
        "source_scope_failures": scope_failures,
        "candidate_states": states,
        "backlog": backlog_summary(conn),
        "dead_work": dead,
        "notifications": notif,
        "outcomes": outc,
        "limits": [
            "Research signals only: no orders, holdings or entry permission exist in this prototype.",
            "Detectors are UNVALIDATED experiments; no predictive accuracy is claimed.",
            "Coverage is limited to declared universes, datasets and configured SEC issuers.",
            "Continuous operation is not established: runs happen only when invoked.",
        ],
    }


def open_candidates(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM candidates ORDER BY CASE state WHEN 'BLOCKED' THEN 0 WHEN 'DETECTED' THEN 1 WHEN 'RESEARCHED' THEN 2 "
        "ELSE 3 END, created_at_utc DESC")]


def text_report(ov: dict) -> str:
    lines = []
    if ov["sample_data_present"]:
        lines.append("=" * 72)
        lines.append("SAMPLE DATA - synthetic fixtures; nothing below is a market observation")
        lines.append("=" * 72)
    lines.append(f"Mode: {ov['mode']}    generated {ov['generated_at']['utc']}")
    lines.append(f"Latest scan: {ov['latest_scan']['message']}")
    for kind, c in ov["collections"].items():
        ls = c["last_success"]
        when = ls["completed_at_utc"] if ls else "never"
        extra = f" (last attempt {c['last_attempt_status']}: {c['last_attempt_error']})" if c["last_attempt_status"] in ("failed", "partial") else ""
        cutoff = f", data cutoff {ls['data_cutoff_utc']}" if ls and ls["data_cutoff_utc"] else ""
        lines.append(f"  {kind:20s} last success {when}{cutoff}{extra}")
    lines.append(f"Candidates by state: {ov['candidate_states']}")
    lines.append(f"Backlog: {ov['backlog']}")
    lines.append(f"Notifications: {ov['notifications']}    Outcomes (current): {ov['outcomes']}")
    if ov["incomplete_runs"]:
        lines.append(f"INCOMPLETE/INTERRUPTED runs: {[r['run_id'] for r in ov['incomplete_runs']]}")
    if ov["dead_work"]:
        lines.append(f"DEAD work items: {[w['work_id'] for w in ov['dead_work']]}")
    for f in ov["recent_failures"][:5]:
        lines.append(f"  failure: {f['kind']} {f['status']} {f['started_at_utc']} {f['error']}")
    lines.extend(f"Note: {x}" for x in ov["limits"])
    return "\n".join(lines)
