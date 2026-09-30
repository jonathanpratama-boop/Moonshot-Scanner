"""Reproductions of the defects reported in the ASTRA review of 2026-09-30 (round 1).

Each test was written to fail against commit 39c9c78 before the corresponding fix.
The reviewer's own scripts were not available; these are independent reconstructions of
the reported scenarios.
"""
from __future__ import annotations

import json
from datetime import datetime

from astra import market
from astra.core import UTC
from astra.market import PointInTimeStore

from .conftest import DATASET, FIX, make_db, write_csv


def _replay_db(tmp_path, clock):
    conn = make_db(tmp_path, "replay")
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    market.import_bars(conn, DATASET, FIX / "bars_stage1.csv")
    return conn


def _bar(conn, symbol, start):
    return conn.execute("SELECT * FROM bars WHERE dataset_id=? AND symbol=? AND start_utc=?",
                        (DATASET, symbol, start)).fetchone()


# ---------------------------------------------------------------- 1. chronology
def test_later_correction_is_not_visible_at_an_earlier_cutoff(tmp_path, clock):
    conn = _replay_db(tmp_path, clock)
    original = _bar(conn, "ZSMPA", "2026-09-17T14:00:00Z")
    corrected = {"symbol": "ZSMPA", "start": "2026-09-17T14:00:00Z", "open": original["open"],
                 "high": original["high"] + 1.0, "low": original["low"], "close": original["close"] + 0.5,
                 "volume": original["volume"]}
    market.import_bars(conn, DATASET, write_csv(tmp_path / "corr.csv", [corrected], as_of="2026-09-24T21:00:00Z"))
    rev2 = conn.execute("SELECT * FROM bar_revisions WHERE symbol='ZSMPA' AND start_utc='2026-09-17T14:00:00Z' "
                        "AND revision_no=2").fetchone()
    assert rev2["available_at_utc"] == "2026-09-24T21:00:00Z"
    store = PointInTimeStore(conn, DATASET, ["ZSMPA"], since=datetime(2026, 9, 1, tzinfo=UTC),
                             until=datetime(2026, 9, 30, tzinfo=UTC))
    at_17 = {b.start: b for b in store.as_of(datetime(2026, 9, 17, 15, 0, 30, tzinfo=UTC))["ZSMPA"]}
    bar = at_17[datetime(2026, 9, 17, 14, 0, tzinfo=UTC)]
    assert bar.revision_no == 1 and bar.close == original["close"]
    at_25 = {b.start: b for b in store.as_of(datetime(2026, 9, 25, tzinfo=UTC))["ZSMPA"]}
    assert at_25[datetime(2026, 9, 17, 14, 0, tzinfo=UTC)].revision_no == 2


def test_gap_filled_later_is_available_only_from_its_batch(tmp_path, clock):
    conn = _replay_db(tmp_path, clock)
    # ZSMPD's 10:10 ET bar was missing from the Sep-17 batch; it arrives in a Sep-24 batch.
    fill = {"symbol": "ZSMPD", "start": "2026-09-17T14:10:00Z", "open": 31, "high": 31.2, "low": 30.9,
            "close": 31.1, "volume": 20000}
    market.import_bars(conn, DATASET, write_csv(tmp_path / "fill.csv", [fill], as_of="2026-09-24T21:00:00Z"))
    row = conn.execute("SELECT * FROM bar_revisions WHERE symbol='ZSMPD' AND start_utc='2026-09-17T14:10:00Z'").fetchone()
    assert row["available_at_utc"] == "2026-09-24T21:00:00Z" and row["availability_basis"] == "LATE_ARRIVAL_AT_BATCH_AS_OF"
    # History outside the earlier batch's coverage keeps the documented assumed availability.
    old = {"symbol": "ZSMPD", "start": "2026-08-20T14:10:00Z", "open": 31, "high": 31.2, "low": 30.9,
           "close": 31.1, "volume": 20000}
    market.import_bars(conn, DATASET, write_csv(tmp_path / "old.csv", [old], as_of="2026-09-24T21:00:00Z"))
    row = conn.execute("SELECT * FROM bar_revisions WHERE symbol='ZSMPD' AND start_utc='2026-08-20T14:10:00Z'").fetchone()
    assert row["availability_basis"] == "ASSUMED_BAR_END_PLUS_LATENCY"


# ---------------------------------------------------------------- 2. SEC identity
def test_sec_ticker_mismatch_blocks_stock_association(tmp_path, clock):
    import copy

    import httpx

    from astra import sec
    from astra.config import Settings

    from .test_sec import PAYLOAD

    conn = make_db(tmp_path, "live")
    issuers = tmp_path / "issuers.json"
    issuers.write_text(json.dumps({"format": "astra.sec_issuers.v1", "issuers": [{"cik": "320193", "ticker": "GOOG"}]}))
    cfg = Settings()
    cfg.sec_user_agent = "ASTRA test harness test@example.com"
    first = sec.collect(conn, issuers, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=PAYLOAD)),
                        sleep=lambda s: None)
    p2 = copy.deepcopy(PAYLOAD)
    rec = p2["filings"]["recent"]
    for k in rec:
        rec[k].insert(0, rec[k][0])
    rec["accessionNumber"][0], rec["form"][0], rec["items"][0] = "0000320193-26-999999", "8-K", "1.01"
    rec["acceptanceDateTime"][0] = "2026-09-30T20:30:00.000Z"
    clock.advance(hours=1)
    sec.collect(conn, issuers, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=p2)),
                sleep=lambda s: None)
    assert conn.execute("SELECT count(*) FROM announcements WHERE symbol='GOOG'").fetchone()[0] == 0
    cands = conn.execute("SELECT * FROM candidates").fetchall()
    assert len(cands) == 1 and cands[0]["symbol"] is None and cands[0]["issuer_cik"] == "0000320193"
    assert "identity conflict" in conn.execute("SELECT reason FROM state_transitions WHERE candidate_id=?",
                                               (cands[0]["candidate_id"],)).fetchone()[0]
    assert first["per_issuer"][0]["identity_status"].startswith("TICKER_MISMATCH")


# ---------------------------------------------------------------- 3. AI evidence validation
def _ai_revision_clearing_with(sources, cleared_refs):
    return {
        "revision_reason_type": "NEW_EVENT",
        "what_changed": {"summary": "found a catalyst", "when_basis": "unknown"},
        "mechanism": ["FLOW"],
        "facts": [{"id": "F1", "text": "index inclusion announced", "source_refs": cleared_refs}],
        "inferences": [], "unknowns": [], "sources": sources, "contrary_evidence": [], "competing_explanations": [],
        "economic_materiality": {"status": "not_applicable", "summary": "flow"}, "liquidity": {"status": "unknown"},
        "financing": {"status": "unknown"}, "dilution": {"status": "unknown"}, "execution": {"status": "unknown"},
        "remaining_uncertainty": "some",
        "blockers": [],
        "cleared_blockers": [
            {"key": "catalyst-unidentified", "clearing_evidence": "exchange notice", "source_refs": cleared_refs},
            {"key": "liquidity-unverified", "clearing_evidence": "quote", "source_refs": cleared_refs}],
        "recheck": None, "expiry": None,
        "proposed_disposition": "researched", "disposition_rationale": "cleared",
    }


def test_ai_cannot_clear_blockers_with_invented_sources(scanned_db):
    import pytest

    from astra import ai, research
    from astra import candidates as cands
    from astra.core import AstraError

    from .test_ai_gate import client_with, enabled, resp

    r = research.import_research(scanned_db, json.loads((FIX / "research" / "ZSMPA_initial.json").read_text()))
    assert r["state"] == "BLOCKED"
    invented = [{"ref": "X1", "title": "Exchange notice", "url": "https://example.com/not-in-context", "kind": "primary",
                 "stance": "supports"}]
    out = _ai_revision_clearing_with(invented, ["X1"])
    with pytest.raises(AstraError):
        ai.investigate(scanned_db, r["candidate_id"], settings=enabled(), client=client_with(resp(json.dumps(out))))
    c = cands.get(scanned_db, r["candidate_id"])
    assert c["state"] == "BLOCKED" and len(cands.open_blockers(scanned_db, r["candidate_id"])) == 2


def test_ai_may_cite_sources_present_in_its_context(scanned_db):
    from astra import ai

    from .test_ai_gate import blocked_output, client_with, enabled, resp

    b = scanned_db.execute("SELECT * FROM candidates WHERE symbol='ZSMPB' AND route='anomaly'").fetchone()
    ctx = ai.build_context(scanned_db, b["candidate_id"])
    assert [a["evidence_id"] for a in ctx["allowed_sources"]] == [b["origin_evidence_id"]]
    out = ai.investigate(scanned_db, b["candidate_id"], settings=enabled(),
                         client=client_with(resp(json.dumps(blocked_output(b["origin_evidence_id"])))))
    assert out["state"] == "BLOCKED"
    call = scanned_db.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert call["status"] == "succeeded"


def test_ai_invented_source_rejection_is_recorded(scanned_db):
    import pytest

    from astra import ai
    from astra.core import AstraError

    from .test_ai_gate import blocked_output, client_with, enabled, resp

    b = scanned_db.execute("SELECT * FROM candidates WHERE symbol='ZSMPB' AND route='anomaly'").fetchone()
    data = blocked_output("ev_does_not_exist")
    with pytest.raises(AstraError, match="AI_SOURCE_NOT_IN_CONTEXT"):
        ai.investigate(scanned_db, b["candidate_id"], settings=enabled(), client=client_with(resp(json.dumps(data))))
    assert scanned_db.execute("SELECT status FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "invalid_output"


# ---------------------------------------------------------------- 4. outcome timing
def _reference_time(conn, subj):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outcome_subjects)")}
    if "reference_time_utc" in cols and subj["reference_time_utc"]:
        return subj["reference_time_utc"]
    if subj["reference_role"] == "OBSERVATION_BAR_REFERENCE":  # pre-fix schema: derive the observation time
        c = conn.execute("SELECT c.simulated_cutoff_utc, a.first_observed_at_utc FROM candidates c JOIN announcements a "
                         "USING(announcement_id) WHERE c.candidate_id=?", (subj["candidate_id"],)).fetchone()
        return c[0] or c[1]
    return None


def test_forward_outcomes_never_end_before_their_reference_time(scanned_db, clock):
    from astra import outcomes, processing

    clock.advance(hours=1)
    market.import_bars(scanned_db, DATASET, FIX / "bars_stage2.csv")
    processing.process_due(scanned_db, worker="w")
    measured = [o for o in outcomes.current_observations(scanned_db) if o["status"] == "MEASURED"]
    assert measured
    from astra import calendar as cal
    from datetime import date as _date
    for o in measured:
        ref_time = _reference_time(scanned_db, o)
        if ref_time is None:
            continue
        close = cal.session(_date.fromisoformat(o["endpoint_session_date"])).close_utc
        assert close.strftime("%Y-%m-%dT%H:%M:%SZ") > ref_time, (o["symbol"], o["reference_role"], o["horizon_sessions"])


def test_later_benchmark_correction_does_not_change_frozen_reference(tmp_path, clock):
    from astra import scan

    conn = _replay_db(tmp_path, clock)
    xb = _bar(conn, "XBMK", "2026-09-17T14:55:00Z")
    corrected = {"symbol": "XBMK", "start": "2026-09-17T14:55:00Z", "open": xb["open"], "high": xb["high"] + 5,
                 "low": xb["low"], "close": xb["close"] + 4.0, "volume": xb["volume"]}
    market.import_bars(conn, DATASET, write_csv(tmp_path / "xb.csv", [corrected], as_of="2026-09-24T21:00:00Z"))
    scan.scan(conn, DATASET, "SAMPLE-US-7@1", cutoff=datetime(2026, 9, 17, 15, 0, 30, tzinfo=UTC))
    subj = conn.execute("SELECT * FROM outcome_subjects WHERE subject_type='candidate' AND symbol='ZSMPA'").fetchone()
    assert subj["reference_bar_start_utc"] == "2026-09-17T14:55:00Z"
    assert subj["benchmark_reference_price"] == xb["close"]


# ---------------------------------------------------------------- 6. announcement revisions
def test_revision_that_becomes_material_creates_candidate_and_research_request(sample_db, clock, tmp_path):
    from astra import announcements

    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    data = json.loads((FIX / "announcements_new.json").read_text())
    clock.advance(minutes=5)
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    # SW-0006 (ZSMPB website refresh) was not flagged; its revision describes a material contract.
    item = [i for i in data["items"] if i["external_id"] == "SW-0006"][0]
    assert sample_db.execute("SELECT screen_result FROM announcements WHERE source_key='SW-0006'").fetchone()[0] == "NOT_FLAGGED"
    item.update(title="SAMPLE: Synthetic Sample B signs multi-year supply contract", categories=["contract"],
                body="SYNTHETIC. Revised: material contract.")
    data["items"] = [item]
    p = tmp_path / "rev.json"
    p.write_text(json.dumps(data))
    clock.advance(hours=1)
    res = announcements.import_local_json(sample_db, p)
    assert res["revisions"] == 1
    c = sample_db.execute("SELECT * FROM candidates WHERE route='announcement' AND symbol='ZSMPB'").fetchone()
    assert c is not None and c["observation_class"] == "MATERIAL_REVISION"
    rev = sample_db.execute("SELECT * FROM announcement_revisions r JOIN announcements a USING(announcement_id) "
                            "WHERE a.source_key='SW-0006' AND r.revision_no=2").fetchone()
    assert rev["screen_result"] == "POTENTIALLY_MATERIAL"
    ev = json.loads(sample_db.execute("SELECT content_json FROM evidence_snapshots WHERE evidence_id=?",
                                      (c["origin_evidence_id"],)).fetchone()[0])
    assert ev["revision_no"] == 2 and "material contract" in ev["content"]["body"]
    assert sample_db.execute("SELECT count(*) FROM work_items WHERE candidate_id=? AND kind='research_request' "
                             "AND status='pending'", (c["candidate_id"],)).fetchone()[0] == 1


def test_revision_of_open_candidate_queues_research(sample_db, clock, tmp_path):
    from astra import announcements, research

    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    r = research.import_research(sample_db, json.loads((FIX / "research" / "ZSMPF_announcement.json").read_text()))
    data = json.loads((FIX / "announcements_new.json").read_text())
    data["items"][0]["body"] = "SYNTHETIC. Amended consideration."
    p = tmp_path / "amended.json"
    p.write_text(json.dumps(data))
    clock.advance(hours=1)
    announcements.import_local_json(sample_db, p)
    assert sample_db.execute("SELECT count(*) FROM work_items WHERE candidate_id=? AND kind='research_request' "
                             "AND status='pending'", (r["candidate_id"],)).fetchone()[0] == 1


# ---------------------------------------------------------------- 5. alert comparison
def test_comparison_respects_each_source_window_and_attribution(scanned_db, tmp_path):
    import csv as _csv

    from astra import exports

    def alerts(path, rows):
        with path.open("w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=["radar", "symbol", "alert_time", "alert_time_tz", "alert_time_basis",
                                                "delivery_time", "excerpt"])
            w.writeheader()
            w.writerows(rows)
        return path

    # Source A monitors 13:00-14:00Z only; the only scan (cutoff 15:00:30Z) is outside it.
    exports.import_external_alerts(scanned_db, alerts(tmp_path / "a.csv", [
        {"radar": "A", "symbol": "ZSMPA", "alert_time": "2026-09-17T13:30:00Z", "alert_time_basis": "message_timestamp"}]),
        source_name="Radar A", coverage_start="2026-09-17T13:00:00Z", coverage_end="2026-09-17T14:00:00Z", is_sample=True)
    # Source B monitors 14:30-21:00Z and alerted on ZSMPG.
    exports.import_external_alerts(scanned_db, alerts(tmp_path / "b.csv", [
        {"radar": "B", "symbol": "ZSMPG", "alert_time": "2026-09-17T16:00:00Z", "alert_time_basis": "message_timestamp"}]),
        source_name="Radar B", coverage_start="2026-09-17T14:30:00Z", coverage_end="2026-09-17T21:00:00Z", is_sample=True)
    out = tmp_path / "cmp.csv"
    summary = exports.export_comparison(scanned_db, out)
    with out.open() as fh:
        rows = list(_csv.DictReader(fh))
    assert not [r for r in rows if r["alert_source"] == "Radar A"]  # no scan inside A's window
    alert_rows = [r for r in rows if r["hourly_alert"] == "True"]
    assert len(alert_rows) == 1 and alert_rows[0]["symbol"] == "ZSMPG" and alert_rows[0]["alert_source"] == "Radar B"
    excluded = {(e["source_name"], e["symbol"]): e["reason"] for e in summary["excluded_alerts"]}
    assert "no fully evaluated scanner run" in excluded[("Radar A", "ZSMPA")]


# ---------------------------------------------------------------- 7. work scheduling
def test_removing_a_recheck_cancels_the_pending_job(scanned_db, clock):
    from astra import research

    r = research.import_research(scanned_db, json.loads((FIX / "research" / "ZSMPA_initial.json").read_text()))
    rev = json.loads((FIX / "research" / "ZSMPA_initial.json").read_text())
    rev.update(parent_research_id="latest", revision_reason_type="CHANGED_INFERENCE", recheck=None,
               disposition_rationale="recheck no longer scheduled")
    clock.advance(minutes=1)
    research.import_research(scanned_db, rev)
    pending = scanned_db.execute("SELECT count(*) FROM work_items WHERE candidate_id=? AND kind='recheck' "
                                 "AND status IN ('pending','leased')", (r["candidate_id"],)).fetchone()[0]
    assert pending == 0
    c = scanned_db.execute("SELECT * FROM candidates WHERE candidate_id=?", (r["candidate_id"],)).fetchone()
    assert c["next_recheck_at_utc"] is None and c["next_recheck_condition"].startswith("UNKNOWN")


def test_repeated_crashes_never_exceed_max_attempts(scanned_db, clock):
    from astra import work
    from astra.db import tx

    with tx(scanned_db):
        work.schedule(scanned_db, kind="recheck", due_at="2026-09-30T12:00:00Z", priority="routine",
                      dedup_key="crashy", candidate_id=None, max_attempts=2)
    claims = 0
    for _ in range(4):  # worker claims, then "crashes" without finishing
        claims += len([i for i in work.claim(scanned_db, "w", ("recheck",), 10, 60) if i["dedup_key"] == "crashy"])
        clock.advance(seconds=61)
        work.claim(scanned_db, "sweeper", ("expiry",), 0, 60)  # any later claim pass sweeps expired leases
    item = scanned_db.execute("SELECT * FROM work_items WHERE dedup_key='crashy'").fetchone()
    assert claims == 2 and item["attempts"] == 2
    assert item["status"] == "dead" and "lease expired" in item["last_error"]


# ---------------------------------------------------------------- 8. AI daily limit
def test_overlapping_ai_calls_cannot_exceed_the_daily_limit(scanned_db):
    from types import SimpleNamespace


    from astra import ai
    from astra.db import connect

    from .test_ai_gate import blocked_output, enabled, resp

    path = scanned_db.execute("PRAGMA database_list").fetchone()["file"]
    b = scanned_db.execute("SELECT * FROM candidates WHERE symbol='ZSMPB' AND route='anomaly'").fetchone()
    a = scanned_db.execute("SELECT * FROM candidates WHERE symbol='ZSMPA' AND route='anomaly'").fetchone()
    nested = {}

    class Overlapping:
        def create(self, **kw):
            if nested:  # the overlapping call itself: answer without overlapping again
                return resp(json.dumps(blocked_output(a["origin_evidence_id"])))
            nested["result"] = "started"
            other = connect(path)  # a second process starts while the first call is in flight
            try:
                ai.investigate(other, a["candidate_id"], settings=enabled(max_calls_per_day=1),
                               client=SimpleNamespace(beta=SimpleNamespace(messages=self)))
                nested["result"] = "ran"
            except ai.AIGateError as exc:
                nested["result"] = str(exc)
            finally:
                other.close()
            return resp(json.dumps(blocked_output(b["origin_evidence_id"])))

    ai.investigate(scanned_db, b["candidate_id"], settings=enabled(max_calls_per_day=1),
                   client=SimpleNamespace(beta=SimpleNamespace(messages=Overlapping())))
    assert "AI_DAILY_LIMIT" in nested["result"]
    assert scanned_db.execute("SELECT count(*) FROM ai_calls WHERE status='succeeded'").fetchone()[0] == 1
    refused = scanned_db.execute("SELECT * FROM ai_calls WHERE status='blocked_by_gate'").fetchall()
    assert len(refused) == 1 and refused[0]["candidate_id"] == a["candidate_id"] and "AI_DAILY_LIMIT" in refused[0]["error"]
    assert scanned_db.execute("SELECT count(*) FROM ai_calls WHERE status='reserved'").fetchone()[0] == 0


# ---------------------------------------------------------------- 9. cohort labels
def _live_dataset(conn, tmp_path, basis):
    d = json.loads((FIX / "dataset.json").read_text())
    d["provider"].update(provider_id="hist", name="historical file provider", is_sample=False)
    d["dataset"].update(dataset_id=f"hist-{basis.lower()}", availability_basis=basis)
    market.register_dataset(conn, d)
    inst = json.loads((FIX / "instruments.json").read_text())
    inst["is_sample"] = False
    (tmp_path / "inst.json").write_text(json.dumps(inst))
    market.import_instruments(conn, tmp_path / "inst.json")
    uni = json.loads((FIX / "universe.json").read_text())
    uni.update(is_sample=False, universe_id="LIVE-7@1")
    (tmp_path / "uni.json").write_text(json.dumps(uni))
    market.import_universe(conn, tmp_path / "uni.json")
    return d["dataset"]["dataset_id"]


def test_historical_imports_are_not_labelled_prospective(tmp_path, clock):
    from astra import scan

    conn = make_db(tmp_path, "live")
    for basis in ("ASSUMED_BAR_END_PLUS_LATENCY", "OBSERVED_RETRIEVAL"):
        ds = _live_dataset(conn, tmp_path, basis)
        imp = market.import_bars(conn, ds, FIX / "bars_stage1.csv")  # as_of 17 Sep, imported 30 Sep
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (imp["run_id"],)).fetchone()
        assert run["source_classification"] == "RETROSPECTIVE", basis
        res = scan.scan(conn, ds, "LIVE-7@1")
        srun = conn.execute("SELECT * FROM runs WHERE run_id=?", (res["run_id"],)).fetchone()
        assert srun["cohort_eligibility"].startswith("EXCLUDED_NOT_PROSPECTIVE"), basis
        assert srun["source_classification"] == "RETROSPECTIVE", basis


def test_fresh_observed_retrieval_scan_is_prospective(tmp_path, clock):
    from astra import scan

    clock.t = datetime(2026, 9, 17, 15, 1, 0, tzinfo=UTC)
    conn = make_db(tmp_path, "live")
    ds = _live_dataset(conn, tmp_path, "OBSERVED_RETRIEVAL")
    imp = market.import_bars(conn, ds, FIX / "bars_stage1.csv")  # as_of 15:00:30, 30 s ago
    assert conn.execute("SELECT source_classification FROM runs WHERE run_id=?", (imp["run_id"],)).fetchone()[0] == "OBSERVED_MARKET"
    res = scan.scan(conn, ds, "LIVE-7@1")
    srun = conn.execute("SELECT * FROM runs WHERE run_id=?", (res["run_id"],)).fetchone()
    assert srun["cohort_eligibility"].startswith("PROSPECTIVE_ASTRA_EQ") and srun["source_classification"] == "OBSERVED_MARKET"


def test_material_revision_has_no_pre_event_reference(scanned_db, clock):
    from astra import outcomes, processing

    market.import_bars(scanned_db, DATASET, FIX / "bars_stage2.csv")
    processing.process_due(scanned_db, worker="w")
    rows = [o for o in outcomes.current_observations(scanned_db)
            if o["symbol"] == "ZSMPA" and o["reference_role"] == "PRE_EVENT_REFERENCE"]
    assert rows and all(o["status"] == "UNKNOWN" and "revision publication time unknown" in o["reason"] for o in rows)
