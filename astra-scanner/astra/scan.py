"""Anomaly scan: one run over a declared universe at one cutoff.

Retains per run: declared membership (with hash), provider/dataset and data cutoff,
detector config versions, and one result per (member, detector) including NO_SIGNAL,
INSUFFICIENT_DATA, UNAVAILABLE and FAILED. Signals freeze their inputs as evidence
before a candidate exists. Bars are read from the shared incremental store; runs do not
copy history.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import calendar as cal
from . import candidates as cands
from . import detectors as det
from . import evidence, notifications, outcomes
from .config import settings
from .core import AstraError, canonical_json, content_hash, fmt_utc, new_id, now, now_str, parse_utc
from .db import db_mode, tx
from .market import PointInTimeStore, dataset_as_of, get_dataset, get_universe
from .runs import finish_run, start_run

LOOKBACK_DAYS = 70
NON_SIGNAL_SPACING_SECONDS = 3600  # doctrine: full non-signal samples at least one hour apart
COHORT = {"live": "PROSPECTIVE_ASTRA_EQ (separate from the authoritative v1 pilot cohort)",
          "sample": "EXCLUDED_SYNTHETIC", "replay": "EXCLUDED_RETROSPECTIVE"}


def ensure_configs(conn: sqlite3.Connection) -> dict[str, dict]:
    recs = {r["detector"]: r for r in det.config_records()}
    for r in recs.values():
        row = conn.execute("SELECT params_hash FROM detector_configs WHERE config_id=?", (r["config_id"],)).fetchone()
        if row:
            if row["params_hash"] != r["params_hash"]:
                raise AstraError(f"detector config {r['config_id']} changed after freezing; a new version is required")
            continue
        conn.execute("INSERT INTO detector_configs VALUES (?,?,?,?,?,?,?,?)",
                     (r["config_id"], r["detector"], r["rule_version"], r["engine_version"],
                      canonical_json(r["params"]), r["params_hash"], r["source_ref"], now_str()))
    return recs


def resolve_cutoff(conn: sqlite3.Connection, mode: str, dataset_id: str, cutoff: datetime | None) -> tuple[datetime, str]:
    as_of = dataset_as_of(conn, dataset_id)
    if as_of is None:
        raise AstraError(f"dataset {dataset_id} has no completed bar import; nothing to scan")
    if mode == "live":
        if cutoff is not None:
            raise AstraError("live scans use the current clock; an explicit cutoff is only allowed in sample/replay")
        return now(), as_of
    if mode == "replay":
        if cutoff is None:
            raise AstraError("replay scans require an explicit simulated cutoff")
        return cutoff, as_of
    return (cutoff or parse_utc(as_of)), as_of


def _sample_non_signals(run_id: str, symbols: list[str], k: int) -> list[str]:
    ranked = sorted(symbols, key=lambda s: hashlib.sha256(f"{run_id}:{s}".encode()).hexdigest())
    return ranked[:k]


def scan(conn: sqlite3.Connection, dataset_id: str, universe_id: str, cutoff: datetime | None = None, *,
         store: PointInTimeStore | None = None, run_kind: str = "anomaly_scan") -> dict[str, Any]:
    mode = db_mode(conn)
    ds = get_dataset(conn, dataset_id)
    members = get_universe(conn, universe_id)
    v1, _ = det.load_v1()
    if len(members) > v1["maximum_members"]:
        raise AstraError(f"universe has {len(members)} members; frozen v1 maximum is {v1['maximum_members']} per batch")
    requested, as_of = resolve_cutoff(conn, mode, dataset_id, cutoff)
    data_cutoff = min(requested, parse_utc(as_of))
    instruments = {r["symbol"]: r for r in conn.execute(
        f"SELECT * FROM instruments WHERE symbol IN ({','.join('?' for _ in members)})", members)}
    benchmarks = sorted({i["benchmark_symbol"] for i in instruments.values() if i["benchmark_symbol"]})
    with tx(conn):
        recs = ensure_configs(conn)
    config_version = ",".join(sorted(r["config_id"] for r in recs.values()))
    scope = {"universe_id": universe_id, "members": members, "members_hash": content_hash(members),
             "benchmarks": benchmarks, "dataset_id": dataset_id, "calendar_id": cal.CALENDAR_ID}
    run_id = start_run(
        conn, run_kind, mode, provider_id=ds["provider_id"], dataset_id=dataset_id,
        requested_cutoff_utc=fmt_utc(requested), data_cutoff_utc=fmt_utc(data_cutoff),
        config_version=config_version, config_hash=content_hash({k: r["params_hash"] for k, r in recs.items()}),
        declared_scope=scope, cohort_eligibility=COHORT[mode],
    )
    try:
        if store is None:
            store = PointInTimeStore(conn, dataset_id, [*members, *benchmarks],
                                     since=requested - timedelta(days=LOOKBACK_DAYS), until=requested)
        bars = store.as_of(requested)
        views = {s: det.make_view(s, bars.get(s, []), requested) for s in {*members, *benchmarks}}
        per_symbol: dict[str, list[det.DetectorResult]] = {}
        for sym in members:
            inst = instruments.get(sym)
            bsym = inst["benchmark_symbol"] if inst else None
            per_symbol[sym] = det.run_detectors(views[sym], views.get(bsym) if bsym else None, requested, ds, inst)
        summary = _persist(conn, run_id, mode, ds, requested, data_cutoff, recs, per_symbol, views, instruments)
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, **summary}


def _persist(conn, run_id, mode, ds, requested, data_cutoff, recs, per_symbol, views, instruments) -> dict:
    is_sample = bool(ds["is_sample"])
    counts: dict[str, dict[str, int]] = {}
    coverage: dict[str, str] = {}
    signals, created, attached = [], [], []
    no_signal_symbols = []
    with tx(conn):
        for sym, results in per_symbol.items():
            statuses = [r.status for r in results]
            if all(s in ("SIGNAL", "NO_SIGNAL") for s in statuses):
                coverage[sym] = "EVALUATED"
            elif "FAILED" in statuses:
                coverage[sym] = "FAILED"
            elif any(s in ("SIGNAL", "NO_SIGNAL") for s in statuses):
                coverage[sym] = "PARTIAL"
            else:
                coverage[sym] = "NOT_EVALUATED"
            if statuses and all(s == "NO_SIGNAL" for s in statuses):
                no_signal_symbols.append(sym)
            fired = []
            inst = instruments.get(sym)
            latest = views[sym].usable[-1] if views[sym].usable else None
            for r in results:
                counts.setdefault(r.detector, {}).setdefault(r.status, 0)
                counts[r.detector][r.status] += 1
                ev_id = None
                if r.status == "SIGNAL":
                    content = {
                        "schema": "astra.evidence.anomaly.v1", "sample_data": is_sample, "mode": mode, "symbol": sym,
                        "detector": r.detector, "config_id": recs[r.detector]["config_id"],
                        "rule_version": recs[r.detector]["rule_version"], "params_hash": recs[r.detector]["params_hash"],
                        "dataset": json.loads(ds["metadata_json"]),
                        "instrument": json.loads(inst["metadata_json"]) if inst else None,
                        "instrument_note": "instrument metadata is the current record, not point-in-time",
                        "requested_cutoff_utc": fmt_utc(requested), "data_cutoff_utc": fmt_utc(data_cutoff),
                        "calendar_id": cal.CALENDAR_ID, "signal_bar_end_utc": fmt_utc(r.signal_bar_end),
                        "feature_available_at_utc": fmt_utc(r.feature_available_at), "features": r.features,
                        "inputs": r.inputs,
                        "signal_bar_reference": {
                            "role": "SIGNAL_BAR_REFERENCE", "close": latest.close, "bar_start_utc": fmt_utc(latest.start),
                            "bar_end_utc": fmt_utc(latest.end), "session_class": "RTH", "scoring_baseline_eligible": False,
                        },
                        "detection_reference": None,
                        "entry_permission": False,
                    }
                    ev_id, _ = evidence.freeze(conn, "anomaly_detection", run_id, sym, content, is_sample)
                    fired.append((r.detector, ev_id))
                    signals.append({"symbol": sym, "detector": r.detector, "evidence_id": ev_id})
                conn.execute(
                    "INSERT INTO detector_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("dr"), run_id, sym, r.detector, recs[r.detector]["config_id"], r.status, r.reason,
                     canonical_json(r.features), fmt_utc(r.signal_bar_end), fmt_utc(r.feature_available_at), ev_id),
                )
            if fired:
                cid, new = _candidate_for(conn, run_id, mode, sym, fired, requested, data_cutoff, latest, ds, inst)
                (created if new else attached).append(cid)
        # sampled non-signals for the outcome baseline (deterministic per run)
        sampled = _sample_non_signals(run_id, sorted(no_signal_symbols), settings().non_signal_samples_per_run)
        for sym in sampled:
            latest = views[sym].usable[-1]
            inst = instruments.get(sym)
            if conn.execute(
                "SELECT 1 FROM outcome_subjects WHERE subject_type='sampled_non_signal' AND symbol=? AND dataset_id=? "
                "AND mode=? AND reference_bar_start_utc > ? AND reference_bar_start_utc <= ?",
                (sym, ds["dataset_id"], mode, fmt_utc(latest.start - timedelta(seconds=NON_SIGNAL_SPACING_SECONDS)),
                 fmt_utc(latest.start))).fetchone():
                continue  # samples are >= 1 hour apart; repeated runs must not inflate the denominator
            outcomes.create_subject(
                conn, subject_type="sampled_non_signal", candidate_id=None, run_id=run_id, symbol=sym,
                dataset_id=ds["dataset_id"], mode=mode, reference_role="SIGNAL_BAR_REFERENCE",
                ref_bar_start=fmt_utc(latest.start), ref_price=latest.close, ref_session=latest.session_date,
                reason=None, benchmark_symbol=inst["benchmark_symbol"] if inst else None,
            )
        n = len(coverage)
        evaluated = sum(1 for v in coverage.values() if v == "EVALUATED")
        any_eval = sum(1 for v in coverage.values() if v in ("EVALUATED", "PARTIAL"))
        cov_status = "SCANNED" if evaluated == n else ("NOT_SCANNED" if any_eval == 0 else "PARTIAL")
        if cov_status == "SCANNED":
            headline = "NO SIGNAL IN COMPLETED COVERAGE" if not signals else f"{len(signals)} SIGNAL(S) IN COMPLETED COVERAGE"
        else:
            gaps = n - evaluated
            headline = (f"COVERAGE INCOMPLETE: {gaps} of {n} members not fully evaluated; absence of signals for them is not a "
                        "negative market result") if cov_status == "PARTIAL" else "NOT_SCANNED: no member could be evaluated"
        cov = {"status": cov_status, "headline": headline, "declared_members": n, "fully_evaluated": evaluated,
               "per_symbol": coverage, "counts": counts, "cutoff_session_context": cal.session_context(requested),
               "sampled_non_signals": sampled}
        if cov_status != "SCANNED":
            reasons = sorted({f"{sym}:{r.detector}:{r.status}:{(r.reason or '').split(':')[0]}"
                              for sym, rs in per_symbol.items() for r in rs if r.status not in ("SIGNAL", "NO_SIGNAL")})
            notifications.queue(
                conn, dedup_key=f"coverage:{ds['dataset_id']}:{content_hash(reasons)}:{cal.local_date(requested).isoformat()}",
                kind="coverage_incomplete", priority="routine", mode=mode,
                payload={"kind": "coverage_incomplete", "note": headline, "run_id": run_id,
                         "cutoff_utc": fmt_utc(requested), "gaps": reasons[:50], "sample_data": is_sample},
            )
        summary = {"coverage_status": cov_status, "headline": headline, "signals": signals,
                   "candidates_created": created, "candidates_updated": attached}
        finish_run(conn, run_id, "completed", summary=summary, coverage=cov, coverage_status=cov_status, in_tx=True)
    return summary


def _candidate_for(conn, run_id, mode, sym, fired, requested, data_cutoff, latest, ds, inst) -> tuple[str, bool]:
    detectors_fired = [d for d, _ in fired]
    if mode == "replay":
        key = f"anomaly:replay:{sym}:{cal.local_date(requested).isoformat()}"
        row = conn.execute("SELECT candidate_id FROM candidates WHERE dedup_key=?", (key,)).fetchone()
        existing = cands.get(conn, row["candidate_id"]) if row else None
    else:
        existing = cands.open_anomaly_candidate(conn, mode, sym)
        key = f"anomaly:{mode}:{sym}:{fired[0][1]}"
    if existing is not None:
        cid = existing["candidate_id"]
        for d, ev in fired:
            linked_before = conn.execute(
                "SELECT 1 FROM candidate_evidence ce JOIN evidence_snapshots e ON e.evidence_id = ce.evidence_id "
                "WHERE ce.candidate_id=? AND json_extract(e.content_json,'$.detector')=?", (cid, d)).fetchone()
            if cands.attach_evidence(conn, cid, ev, "repeat_detection", "system",
                                     {"detector": d, "run_id": run_id, "cutoff_utc": fmt_utc(requested)}) and not linked_before:
                notifications.queue_for_candidate(conn, cid, kind="new_detector_evidence", dedup_suffix=d,
                                                  priority="routine", note=f"{d} fired on open candidate {sym}")
        return cid, False
    cid, is_new = cands.create(
        conn, route="anomaly", mode=mode, symbol=sym, dedup_key=key, origin_run_id=run_id,
        origin_evidence_id=fired[0][1], data_cutoff_utc=fmt_utc(data_cutoff),
        simulated_cutoff_utc=fmt_utc(requested) if mode == "replay" else None, mechanism_hint="TAPE",
        issuer_cik=inst["issuer_cik"] if inst else None, expiry_sessions=settings().default_expiry_sessions,
        reason=f"experimental detector signal(s) {'+'.join(detectors_fired)}; no catalyst required (TAPE)",
    )
    if not is_new:  # identical evidence already produced this candidate (re-scan of the same inputs)
        return cid, False
    for d, ev in fired[1:]:
        cands.attach_evidence(conn, cid, ev, "co_detection", "system", {"detector": d, "run_id": run_id})
    outcomes.create_subject(
        conn, subject_type="candidate", candidate_id=cid, run_id=run_id, symbol=sym, dataset_id=ds["dataset_id"],
        mode=mode, reference_role="SIGNAL_BAR_REFERENCE", ref_bar_start=fmt_utc(latest.start), ref_price=latest.close,
        ref_session=latest.session_date, reason=None, benchmark_symbol=inst["benchmark_symbol"] if inst else None,
    )
    return cid, True
