"""CSV/JSON exports that keep discovery, research, outcomes and comparisons separate.

- candidates.csv          research-route view (state, owner, blockers, schedule)
- detector_results.csv    detector-only baseline: every result incl. NO_SIGNAL / INSUFFICIENT_DATA
- outcomes.csv            current forward-outcome observation per subject and horizon
- runs.csv                every run with declared scope, cutoffs and coverage
- comparison.csv (+ .summary.json)  scanner vs detector-only vs retained hourly alerts,
                          restricted to common coverage (symbol-session pairs covered by both)
No export contains simulated trading results: none exist in this prototype.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import calendar as cal
from .core import AstraError, ValidationError, content_hash, file_sha256, fmt_utc, new_id, now_str, parse_source_time, parse_utc
from .db import db_mode, tx
from .outcomes import current_observations
from .runs import finish_run, start_run


def _write(path: str | Path, rows: list[dict], columns: list[str]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return len(rows)


def export_candidates(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = []
    for c in conn.execute("SELECT * FROM candidates ORDER BY created_at_utc").fetchall():
        blockers = conn.execute("SELECT count(*) FROM blockers WHERE candidate_id=? AND status='open'",
                                (c["candidate_id"],)).fetchone()[0]
        dets = [r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(e.content_json,'$.detector') FROM candidate_evidence ce JOIN evidence_snapshots e "
            "ON e.evidence_id=ce.evidence_id WHERE ce.candidate_id=? AND e.kind='anomaly_detection'", (c["candidate_id"],))]
        rows.append({**dict(c), "open_blockers": blockers, "detectors": "+".join(sorted(d for d in dets if d)),
                     "sample_data": c["mode"] == "sample"})
    return _write(path, rows, ["candidate_id", "mode", "sample_data", "route", "symbol", "state", "owner", "detected_at_utc",
                               "data_cutoff_utc", "simulated_cutoff_utc", "observation_class", "detectors",
                               "research_status", "open_blockers", "next_recheck_at_utc", "next_recheck_condition",
                               "expires_at_utc", "expiry_condition"])


def export_detector_results(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = [dict(r) for r in conn.execute(
        "SELECT r.run_id, u.mode, u.requested_cutoff_utc, u.data_cutoff_utc, u.completed_at_utc AS run_completed_at_utc, "
        "u.cohort_eligibility, r.symbol, r.detector, r.config_id, r.status, r.reason, r.signal_bar_end_utc, "
        "r.feature_available_at_utc, r.evidence_id FROM detector_results r JOIN runs u ON u.run_id = r.run_id "
        "ORDER BY u.started_at_utc, r.symbol, r.detector")]
    return _write(path, rows, list(rows[0].keys()) if rows else ["run_id"])


def export_outcomes(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = current_observations(conn)
    return _write(path, rows, ["subject_id", "subject_type", "candidate_id", "mode", "symbol", "dataset_id", "definition_id",
                               "reference_role", "reference_session_date", "reference_price", "reference_status",
                               "horizon_sessions", "endpoint_session_date", "status", "reason", "endpoint_price",
                               "return_value", "benchmark_return", "excess_return", "benchmark_status", "data_cutoff_utc",
                               "observed_at_utc"])


def export_runs(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at_utc")]
    return _write(path, rows, ["run_id", "kind", "mode", "source_classification", "status", "started_at_utc",
                               "completed_at_utc", "provider_id", "dataset_id", "requested_cutoff_utc", "data_cutoff_utc",
                               "config_version", "declared_scope_hash", "coverage_status", "cohort_eligibility", "error"])


# ---------------------------------------------------------------------- hourly alerts
def import_external_alerts(conn: sqlite3.Connection, path: str | Path, *, source_name: str, coverage_start: str,
                           coverage_end: str, universe: list[str] | None = None, is_sample: bool = False,
                           notes: str | None = None) -> dict:
    """Retained hourly-alert rows (CSV: radar,symbol,alert_time,alert_time_tz,alert_time_basis,delivery_time,excerpt).

    Missing delivery time stays UNKNOWN. The declared coverage window/universe is what the
    comparison may assume the alerts covered; nothing outside it is inferred.
    """
    mode = db_mode(conn)
    if mode == "live" and is_sample:
        raise AstraError("refusing sample alerts in a live database")
    cs, _ = parse_source_time(coverage_start)
    ce, _ = parse_source_time(coverage_end)
    if ce <= cs:
        raise ValidationError("coverage_end must be after coverage_start")
    with Path(path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    run_id = start_run(conn, "external_alert_import", mode, source_classification="SYNTHETIC" if is_sample else "OPERATOR_IMPORT",
                       input_ref=f"{path} sha256={file_sha256(str(path))}",
                       declared_scope={"source_name": source_name, "coverage_start": fmt_utc(cs), "coverage_end": fmt_utc(ce),
                                       "universe": universe})
    added = dup = 0
    with tx(conn):
        conn.execute("INSERT INTO external_alert_coverage VALUES (?,?,?,?,?,?)",
                     (run_id, source_name, fmt_utc(cs), fmt_utc(ce), json.dumps(universe) if universe else None, notes))
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                raise ValidationError(f"alert row without symbol: {r}")
            raw = (r.get("alert_time") or "").strip() or None
            t_utc, basis = None, (r.get("alert_time_basis") or "unknown").strip()
            if raw:
                dt, _ = parse_source_time(raw, (r.get("alert_time_tz") or "").strip() or None)
                t_utc = fmt_utc(dt)
            deliv = (r.get("delivery_time") or "").strip()
            d_utc = fmt_utc(parse_source_time(deliv)[0]) if deliv else None
            h = content_hash({"source": source_name, **{k: r.get(k) for k in sorted(r)}})
            cur = conn.execute("INSERT OR IGNORE INTO external_alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (new_id("xal"), source_name, r.get("radar"), sym, raw, t_utc, basis, d_utc,
                                (r.get("excerpt") or "")[:500], now_str(), run_id, h))
            added += cur.rowcount
            dup += 1 - cur.rowcount
        finish_run(conn, run_id, "completed", summary={"rows": len(rows), "added": added, "duplicates": dup}, in_tx=True)
    return {"run_id": run_id, "rows": len(rows), "added": added, "duplicates": dup}


def _session_of(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return cal.local_date(parse_utc(ts)).isoformat()
    except cal.CalendarUnavailable:
        return None


def _run_time(run: sqlite3.Row) -> str | None:
    """The time a scan represents: actual completion for live runs, the (simulated/data) cutoff otherwise."""
    return run["completed_at_utc"] if run["mode"] == "live" else run["requested_cutoff_utc"]


def export_comparison(conn: sqlite3.Connection, path: str | Path) -> dict[str, Any]:
    """Compare the scanner with each retained alert source over that source's own window.

    For every alert import (one declared window, one source): only scanner runs, detector
    signals and ASTRA candidates whose time lies inside that window, and only alerts from
    that import, are considered. Rows are (window, session, symbol) pairs with at least one
    fully evaluated scan inside the window (and inside the declared alert universe, if any).
    An alert is attached to at most one row; every alert not attached is listed with the reason.
    """
    windows = conn.execute("SELECT * FROM external_alert_coverage ORDER BY coverage_start_utc, run_id").fetchall()
    if not windows:
        raise AstraError("no external alert coverage imported; run import-hourly-alerts first")
    scan_runs = [r for r in conn.execute(
        "SELECT * FROM runs WHERE kind IN ('anomaly_scan','replay') AND coverage_json IS NOT NULL "
        "AND status IN ('completed','partial') ORDER BY started_at_utc")]
    signals = [r for r in conn.execute(
        "SELECT r.symbol, r.detector, u.* FROM detector_results r JOIN runs u ON u.run_id=r.run_id "
        "WHERE r.status='SIGNAL' ORDER BY u.started_at_utc")]
    all_cands = []
    for c in conn.execute("SELECT * FROM candidates ORDER BY created_at_utc"):
        if c["route"] == "announcement" and c["mode"] == "sample":
            a = conn.execute("SELECT published_at_utc FROM announcements WHERE announcement_id=?", (c["announcement_id"],)).fetchone()
            t = a["published_at_utc"] if a and a["published_at_utc"] else c["detected_at_utc"]
        else:
            t = c["simulated_cutoff_utc"] or (c["data_cutoff_utc"] if c["mode"] == "sample" else c["detected_at_utc"])
        all_cands.append((t, c))
    rows, excluded, per_window = [], [], []
    for w in windows:
        w0, w1 = w["coverage_start_utc"], w["coverage_end_utc"]
        uni = set(json.loads(w["universe_json"])) if w["universe_json"] else None
        inside = lambda t: bool(t) and w0 <= t <= w1  # noqa: E731
        covered: dict[tuple[str, str], dict[str, Any]] = {}
        for run in scan_runs:
            t = _run_time(run)
            if not inside(t):
                continue
            for sym, st in (json.loads(run["coverage_json"]).get("per_symbol") or {}).items():
                if uni is not None and sym not in uni:
                    continue
                e = covered.setdefault((_session_of(t), sym), {"scans": 0, "evaluated": 0, "mode": run["mode"]})
                e["scans"] += 1
                e["evaluated"] += 1 if st == "EVALUATED" else 0
        first_signal: dict[tuple[str, str], dict[str, Any]] = {}
        for r in signals:
            t = _run_time(r)
            if inside(t):
                e = first_signal.setdefault((_session_of(t), r["symbol"]), {"time": t, "detectors": set()})
                e["detectors"].add(r["detector"])
        attached: dict[tuple[str, str], list] = {}
        n_alerts = 0
        for a in conn.execute("SELECT * FROM external_alerts WHERE run_id=? ORDER BY alert_time_utc", (w["run_id"],)):
            n_alerts += 1
            key = (_session_of(a["alert_time_utc"]), a["symbol"])
            if not a["alert_time_utc"]:
                reason = "alert time unknown"
            elif not inside(a["alert_time_utc"]):
                reason = "alert time outside this source's declared window"
            elif uni is not None and a["symbol"] not in uni:
                reason = "symbol outside this source's declared universe"
            elif covered.get(key, {}).get("evaluated", 0) == 0:
                reason = "no fully evaluated scanner run for this symbol/session inside this source's window"
            else:
                attached.setdefault(key, []).append(a)
                continue
            excluded.append({"source_name": w["source_name"], "alert_import_run_id": w["run_id"],
                             "session_date": key[0], "symbol": a["symbol"], "alert_time_utc": a["alert_time_utc"],
                             "reason": reason})
        w_rows = []
        for (sess, sym), sc in sorted(covered.items()):
            if sc["evaluated"] == 0:
                continue
            d = first_signal.get((sess, sym))
            cs = [c for t, c in all_cands if inside(t) and c["symbol"] == sym and _session_of(t) == sess]
            al = attached.get((sess, sym), [])
            w_rows.append({
                "alert_source": w["source_name"], "alert_import_run_id": w["run_id"],
                "window_start_utc": w0, "window_end_utc": w1,
                "session_date": sess, "symbol": sym, "mode": sc["mode"], "sample_data": sc["mode"] == "sample",
                "scanner_scans_in_window": sc["scans"], "scanner_full_evaluations_in_window": sc["evaluated"],
                "detector_only_signal": bool(d), "detector_first_time_utc": d["time"] if d else None,
                "detector_time_basis": ("actual run completion" if sc["mode"] == "live" else "data/simulated cutoff (not live detection)") if d else None,
                "detectors": "+".join(sorted(d["detectors"])) if d else None,
                "astra_candidates": ";".join(f"{c['candidate_id']}:{c['route']}:{c['state']}" for c in cs) or None,
                "hourly_alert": bool(al), "hourly_alerts_count": len(al),
                "hourly_first_alert_time_utc": al[0]["alert_time_utc"] if al else None,
                "hourly_alert_time_basis": al[0]["alert_time_basis"] if al else None,
                "hourly_delivery_time_utc": (al[0]["delivery_time_utc"] or "UNKNOWN") if al else None,
                "hourly_radar": al[0]["radar"] if al else None,
            })
        rows.extend(w_rows)
        per_window.append({
            "source_name": w["source_name"], "alert_import_run_id": w["run_id"], "window_start_utc": w0,
            "window_end_utc": w1, "alerts_imported": n_alerts,
            "alerts_in_common_coverage": sum(r["hourly_alerts_count"] for r in w_rows),
            "rows_in_common_coverage": len(w_rows),
            "detector_only_signal_rows": sum(1 for r in w_rows if r["detector_only_signal"]),
            "hourly_alert_rows": sum(1 for r in w_rows if r["hourly_alert"]),
            "both": sum(1 for r in w_rows if r["hourly_alert"] and r["detector_only_signal"]),
        })
    cols = ["alert_source", "alert_import_run_id", "window_start_utc", "window_end_utc", "session_date", "symbol", "mode",
            "sample_data", "scanner_scans_in_window", "scanner_full_evaluations_in_window", "detector_only_signal",
            "detector_first_time_utc", "detector_time_basis", "detectors", "astra_candidates", "hourly_alert",
            "hourly_alerts_count", "hourly_first_alert_time_utc", "hourly_alert_time_basis", "hourly_delivery_time_utc",
            "hourly_radar"]
    _write(path, rows, cols)
    summary = {
        "per_window": per_window,
        "excluded_alerts": excluded,
        "caveats": [
            "each alert source is compared only over its own declared window (exact times, not calendar days) and universe",
            "scanner scans, detector signals and ASTRA candidates count for a window only if their time lies inside it",
            "each alert is attached to at most one row or listed as excluded with its reason; windows are not summed",
            "hourly alert delivery time is UNKNOWN unless supplied; alert time basis is as declared by the importer",
            "sample/replay times are data or simulated cutoffs, not live detection times",
            "counts are descriptive; they do not establish accuracy, lead time or trading performance",
        ],
    }
    Path(str(path) + ".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
