"""Interrupted jobs, restart recovery, duplicate-processing prevention, notifications."""
from __future__ import annotations

import json

import pytest

from astra import notifications, processing, research, work
from astra import candidates as cands
from astra.core import AstraError
from astra.db import tx

from .conftest import FIX, cand_by


def load(name):
    return json.loads((FIX / "research" / f"{name}.json").read_text())


def test_interrupted_lease_is_recovered_once(scanned_db, clock):
    research.import_research(scanned_db, load("ZSMPA_initial"))  # schedules an urgent recheck due now
    items = work.claim(scanned_db, "worker-A", ("recheck",), 10, lease_seconds=300)
    assert len(items) == 1
    # worker-A "crashes": nothing processed. A second worker cannot take the live lease.
    assert work.claim(scanned_db, "worker-B", ("recheck",), 10, lease_seconds=300) == []
    clock.advance(seconds=301)
    res = processing.process_due(scanned_db, worker="worker-B")
    assert res["1_urgent"] == {"claimed": 1, "done": 1}
    w = scanned_db.execute("SELECT * FROM work_items WHERE work_id=?", (items[0]["work_id"],)).fetchone()
    assert w["status"] == "done" and w["attempts"] == 2
    trail = [r["detail"] for r in scanned_db.execute("SELECT detail FROM work_attempts WHERE work_id=? ORDER BY rowid", (w["work_id"],))]
    assert trail[0] == "claimed" and trail[1].startswith("recovered expired lease held by worker-A")
    # a stale worker-A can no longer complete it (lease lost)
    assert work.process(scanned_db, items[0], "worker-A", lambda c, i: {"x": 1}) == "lease_lost"
    # running again processes nothing twice
    again = processing.process_due(scanned_db, worker="worker-C")
    assert again["1_urgent"]["claimed"] == 0 and again["3_routine"]["claimed"] == 0
    n = scanned_db.execute("SELECT count(*) FROM notifications WHERE kind='recheck_due'").fetchone()[0]
    assert n == 1


def test_failing_handler_retries_then_dead_letters(scanned_db, clock):
    with tx(scanned_db):
        work.schedule(scanned_db, kind="recheck", due_at="2026-09-30T12:00:00Z", priority="routine",
                      dedup_key="test-bad", candidate_id=None, max_attempts=2)
    outcomes = []
    for _ in range(3):
        items = work.claim(scanned_db, "w", ("recheck",), 10, 60)
        for it in items:
            outcomes.append(work.process(scanned_db, it, "w", lambda c, i: (_ for _ in ()).throw(RuntimeError("boom"))))
    assert outcomes == ["failed_will_retry", "dead"]
    w = scanned_db.execute("SELECT * FROM work_items WHERE dedup_key='test-bad'").fetchone()
    assert w["status"] == "dead" and "boom" in w["last_error"]


def test_expiry_transitions_and_superseded_expiry_is_skipped(scanned_db, clock):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    assert b["expires_at_utc"]  # default expiry scheduled at detection
    clock.t = clock.t.replace(year=2026, month=10, day=8)
    res = processing.process_due(scanned_db, worker="w")
    assert res["1_urgent"]["done"] >= 1
    b2 = cands.get(scanned_db, b["candidate_id"])
    assert b2["state"] == "EXPIRED"
    t = scanned_db.execute("SELECT * FROM state_transitions WHERE candidate_id=? AND to_state='EXPIRED'", (b["candidate_id"],)).fetchone()
    assert t["reason_type"] == "EXPIRY" and json.loads(t["evidence_json"])["expires_at_utc"] == b["expires_at_utc"]


def test_research_supersedes_default_expiry(scanned_db, clock):
    r = research.import_research(scanned_db, load("ZSMPF_announcement"))
    rows = scanned_db.execute("SELECT status FROM work_items WHERE candidate_id=? AND kind='expiry' ORDER BY created_at_utc",
                              (r["candidate_id"],)).fetchall()
    assert [x[0] for x in rows] == ["cancelled", "pending"]


def test_notification_dedup_and_delivery_states(scanned_db, clock):
    c = cand_by(scanned_db, "ZSMPA", "anomaly")
    with tx(scanned_db):
        assert notifications.queue_for_candidate(scanned_db, c["candidate_id"], kind="candidate_detected",
                                                 dedup_suffix="initial", priority="routine", note="dup") is False
    total = scanned_db.execute("SELECT count(*) FROM notifications").fetchone()[0]
    res = notifications.deliver(scanned_db, channel="external")
    assert res["not_configured"] == total and res["attempted"] == 0
    assert {r[0] for r in scanned_db.execute("SELECT status FROM notifications")} == {"queued"}
    nid = scanned_db.execute("SELECT notification_id FROM notifications LIMIT 1").fetchone()[0]
    with pytest.raises(AstraError, match="only an attempted"):
        notifications.acknowledge(scanned_db, nid, by="me")
    res = notifications.deliver(scanned_db)
    assert res["attempted"] == total and len(res["files"]) == total
    notifications.acknowledge(scanned_db, nid, by="me")
    st = dict(scanned_db.execute("SELECT status, count(*) FROM notifications GROUP BY status").fetchall())
    assert st == {"acknowledged": 1, "attempted": total - 1}
    again = notifications.deliver(scanned_db)
    assert again["attempted"] == 0  # nothing re-sent


def test_decision_row_is_complete_with_explicit_unknowns(scanned_db):
    for n in scanned_db.execute("SELECT payload_json FROM notifications WHERE candidate_id IS NOT NULL"):
        row = json.loads(n[0])["decision_row"]
        assert notifications.validate_decision_row(row) == []
        assert row["ops_revision"] == "ASTRA-OPS-20260929-r1.2" and row["entry_permission"] is False
        assert row["delivery_time"]["value"] == "UNKNOWN"
    f = cand_by(scanned_db, "ZSMPF", "announcement")
    row = notifications.decision_row(scanned_db, f)
    assert row["source_publication_time"]["utc"] == "2026-09-17T11:45:00Z"
    assert row["source_publication_time"]["Asia/Jakarta"].endswith("WIB")
    assert row["quote_or_reference"]["value"] == "UNKNOWN"


def test_coverage_incomplete_notified_once_per_condition(scanned_db, clock):
    from astra import scan

    from .conftest import DATASET, UNIVERSE

    before = scanned_db.execute("SELECT count(*) FROM notifications WHERE kind='coverage_incomplete'").fetchone()[0]
    scan.scan(scanned_db, DATASET, UNIVERSE)
    after = scanned_db.execute("SELECT count(*) FROM notifications WHERE kind='coverage_incomplete'").fetchone()[0]
    assert before == 1 and after == 1
