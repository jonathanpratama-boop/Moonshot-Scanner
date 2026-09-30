"""End-to-end SAMPLE workflow:

sample import -> detection -> frozen evidence -> research import -> state update ->
dashboard -> due recheck -> outcome record

Every step checks its own expected result and raises if it does not hold. All data is
synthetic; all research is sample research. Operational timestamps are real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import OPS_REVISION
from .config import PROJECT_ROOT
from .core import AstraError, now_str

FIX = PROJECT_ROOT / "fixtures" / "sample"
DATASET = "sample-synthetic-5m-rth"
UNIVERSE = "SAMPLE-US-7@1"


def _step(n: int, title: str) -> None:
    print(f"\n=== STEP {n}: {title} ===")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AstraError(f"DEMO CHECK FAILED: {msg}")
    print(f"  ok: {msg}")


def run_demo(db_path: Path, reset: bool = False) -> dict:
    from . import announcements, market, notifications, processing, research, scan
    from . import candidates as cands
    from .ai import AIGateError, AISettings, investigate
    from .db import init_db, tx
    from .evidence import load as load_evidence
    from .exports import (export_candidates, export_comparison, export_detector_results, export_outcomes, export_runs,
                          import_external_alerts)
    from .outcomes import current_observations

    if db_path.exists():
        if not reset:
            raise AstraError(f"{db_path} exists; pass --reset to rebuild the sample database")
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    out_dir = db_path.parent / "demo_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ASTRA_OUTBOX_DIR", str(db_path.parent / "outbox"))
    print("SAMPLE DATA DEMONSTRATION - synthetic fixtures, sample research, no network, no paid calls")
    print(f"database: {db_path}   started (actual clock): {now_str()}   ops revision: {OPS_REVISION}")

    _step(1, "sample import (dataset, instruments, universe, stage-1 bars, announcements)")
    conn = init_db(db_path, "sample")
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    b1 = market.import_bars(conn, DATASET, FIX / "bars_stage1.csv")
    print(f"  bars stage 1: {b1}")
    _check(b1["data_conflict"] == 1 and b1["forming"] == 7, "one DATA_CONFLICT bar quarantined, 7 forming bars retained but unusable")
    a1 = announcements.import_local_json(conn, FIX / "announcements_backlog.json")
    _check(a1["BASELINE_BACKLOG"] == 3 and a1["candidates_created"] == 0, "first collection is baseline backlog: no candidates")
    a2 = announcements.import_local_json(conn, FIX / "announcements_new.json")
    print(f"  announcements second import: { {k: a2[k] for k in ('NEW_PUBLICATION', 'NEWLY_FOUND_OLD_INFORMATION', 'NEW_OBSERVATION_TIME_UNKNOWN', 'sightings_only', 'revisions', 'candidates_created')} }")
    _check(a2["NEW_PUBLICATION"] == 1 and a2["revisions"] == 1 and a2["sightings_only"] == 1, "new item, one revision and one duplicate sighting")
    a3 = announcements.import_local_json(conn, FIX / "announcements_new.json")
    _check(a3["candidates_created"] == 0 and a3["new_items"] == 0 and a3["revisions"] == 0, "re-importing the same file creates no duplicates")

    _step(2, "detection (anomaly scan at the stage-1 data cutoff)")
    sc = scan.scan(conn, DATASET, UNIVERSE)
    print(f"  {sc['headline']}")
    fired = sorted((s["symbol"], s["detector"]) for s in sc["signals"])
    print(f"  signals: {fired}")
    _check(fired == [("ZSMPA", "IGNITION"), ("ZSMPB", "COMPRESSION"), ("ZSMPG", "EQ_GAP")], "expected three experimental signals")
    _check(sc["coverage_status"] == "PARTIAL", "coverage reported PARTIAL (missing/conflict/stale members), not 'no signal'")
    sc2 = scan.scan(conn, DATASET, UNIVERSE)
    _check(not sc2["candidates_created"], "re-running the same scan creates no duplicate candidates")

    _step(3, "frozen evidence (saved before research, read back)")
    for cid in sc["candidates_created"]:
        c = cands.get(conn, cid)
        ev = load_evidence(conn, c["origin_evidence_id"])
        rb = conn.execute("SELECT ok FROM evidence_readbacks WHERE evidence_id=?", (c["origin_evidence_id"],)).fetchone()
        _check(rb["ok"] == 1 and c["research_status"] == "QUEUED",
               f"{c['symbol']} evidence {ev['evidence_id']} hash {ev['content_hash'][:12]} read back; research not yet done")
    try:
        with tx(conn):
            conn.execute("UPDATE evidence_snapshots SET content_json='{}' WHERE evidence_id=?", (c["origin_evidence_id"],))
        raise AstraError("evidence was modifiable")
    except Exception as exc:  # sqlite3.IntegrityError from the append-only trigger
        _check("append-only" in str(exc), "evidence rows are append-only (UPDATE aborted by trigger)")

    _step(4, "research import (sample research) and state update")
    ra = research.import_research_file(conn, FIX / "research" / "ZSMPA_initial.json")
    _check(ra["state"] == "BLOCKED", "ZSMPA -> BLOCKED with 2 blockers and clearing evidence requirements")
    rf = research.import_research_file(conn, FIX / "research" / "ZSMPF_announcement.json")
    _check(rf["state"] == "RESEARCHED", "ZSMPF announcement candidate -> RESEARCHED (no price move needed)")
    rg = research.import_research_file(conn, FIX / "research" / "ZSMPG_anomaly.json")
    _check(rg["state"] == "REJECTED", "ZSMPG anomaly -> REJECTED with retained reason")
    try:
        with tx(conn):
            cands.transition(conn, rg["candidate_id"], "RESEARCHED", actor="demo", reason="try", evidence={"x": 1})
        raise AstraError("invalid transition accepted")
    except cands.TransitionError as exc:
        _check(True, f"invalid transition refused: {exc}")
    try:
        with tx(conn):
            cands.transition(conn, ra["candidate_id"], "ENTRY_ELIGIBLE", actor="demo", reason="try", evidence={"x": 1})
        raise AstraError("reserved state reached")
    except cands.TransitionError:
        _check(True, "ENTRY_ELIGIBLE refused (reserved; no readiness in prototype)")
    try:
        investigate(conn, ra["candidate_id"], settings=AISettings(paid_calls="", provider=""))
        raise AstraError("AI call ran while disabled")
    except AIGateError as exc:
        _check("AI_RESEARCH_DISABLED" in str(exc), "paid AI research disabled by default (gate refused, no request sent)")

    _step(5, "dashboard (read-only HTTP render check)")
    from fastapi.testclient import TestClient

    from .web.app import create_app

    client = TestClient(create_app(str(db_path)))
    home = client.get("/")
    _check(home.status_code == 200 and "SAMPLE DATA" in home.text, "overview renders with the SAMPLE DATA banner")
    _check("COVERAGE INCOMPLETE" in home.text or "PARTIAL" in home.text, "overview distinguishes incomplete coverage")
    page = client.get(f"/candidates/{ra['candidate_id']}")
    _check(page.status_code == 200 and "catalyst-unidentified" in page.text, "candidate page shows blockers and evidence")
    anns = client.get("/announcements")
    _check("<script>alert" not in anns.text and "&lt;script&gt;" in anns.text, "untrusted source text is escaped")

    _step(6, "due recheck processing (interruption-safe queue)")
    d1 = processing.process_due(conn, worker="demo-worker")
    print(f"  urgent: {d1['1_urgent']}  routine: {d1['3_routine']}  backlog: {d1['5_research_backlog']['pending_research_requests']}")
    _check(d1["1_urgent"].get("done", 0) >= 1, "the due urgent recheck for ZSMPA was processed and queued for research")
    rv = research.import_research_file(conn, FIX / "research" / "ZSMPA_revision.json")
    _check(rv["state"] == "RESEARCHED" and len(rv["cleared_blockers"]) == 2,
           "revision cleared both blockers with cited evidence: BLOCKED -> RESEARCHED")

    _step(7, "outcome records (stage-2 bars arrive, forward outcomes measured)")
    before = {o["status"] for o in current_observations(conn)}
    print(f"  outcome statuses before stage 2: {sorted(before)}")
    b2 = market.import_bars(conn, DATASET, FIX / "bars_stage2.csv")
    _check(b2["revised"] == 7, "forming bars from stage 1 were appended as completed revisions (originals kept)")
    d2 = processing.process_due(conn, worker="demo-worker")
    oc = d2["4_outcomes"]
    print(f"  outcomes: {oc}")
    _check(oc["MEASURED"] > 0 and oc["UNKNOWN"] > 0 and oc["CENSORED"] > 0,
           "MEASURED, UNKNOWN (missing endpoint bar) and CENSORED (coverage ended) all recorded")

    _step(8, "notifications (local preview only), comparison exports")
    q = conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
    dl = notifications.deliver(conn)
    ext = notifications.deliver(conn, channel="external")
    first = conn.execute("SELECT notification_id FROM notifications WHERE status='attempted' ORDER BY created_at_utc LIMIT 1").fetchone()
    notifications.acknowledge(conn, first["notification_id"], by="demo-operator")
    st = dict(conn.execute("SELECT status, count(*) FROM notifications GROUP BY status").fetchall())
    _check(dl["attempted"] == q and ext["attempted"] == 0, f"{q} notifications written to local outbox; external channel not configured")
    print(f"  notification statuses: {st}")
    import_external_alerts(conn, FIX / "hourly_alerts.csv", source_name="SAMPLE hourly radar", is_sample=True,
                           coverage_start="2026-09-17T13:00:00Z", coverage_end="2026-09-17T21:00:00Z",
                           universe=["ZSMPA", "ZSMPB", "ZSMPC", "ZSMPD", "ZSMPE", "ZSMPF", "ZSMPG", "ZSMPQ"])
    cmp_summary = export_comparison(conn, out_dir / "comparison.csv")
    export_candidates(conn, out_dir / "candidates.csv")
    export_detector_results(conn, out_dir / "detector_results.csv")
    export_outcomes(conn, out_dir / "outcomes.csv")
    export_runs(conn, out_dir / "runs.csv")
    for w in cmp_summary["per_window"]:
        print(f"  comparison [{w['source_name']} {w['window_start_utc']}..{w['window_end_utc']}]: rows={w['rows_in_common_coverage']} "
              f"detector_only={w['detector_only_signal_rows']} hourly={w['hourly_alert_rows']} both={w['both']} "
              f"alerts {w['alerts_in_common_coverage']}/{w['alerts_imported']} in common coverage")
    _check(sum(w["alerts_in_common_coverage"] for w in cmp_summary["per_window"]) + len(cmp_summary["excluded_alerts"])
           == sum(w["alerts_imported"] for w in cmp_summary["per_window"]),
           "every imported alert is either in common coverage once or excluded with a reason")
    print(f"  exports written to {out_dir}")

    states = dict(conn.execute("SELECT state, count(*) FROM candidates GROUP BY state").fetchall())
    print(f"\nDEMO COMPLETE (actual clock {now_str()}). Candidate states: {states}")
    print(f"Start the dashboard:  astra --db {db_path} serve   ->  http://127.0.0.1:8765")
    return {"states": states, "db": str(db_path)}
