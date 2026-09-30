"""State transitions, reserved states, research validation and blockers."""
from __future__ import annotations

import copy
import json

import pytest

from astra import candidates as cands
from astra import research
from astra.core import AstraError, ValidationError
from astra.db import tx

from .conftest import FIX, cand_by


def load(name):
    return json.loads((FIX / "research" / f"{name}.json").read_text())


def test_transition_map_and_required_reason_evidence(scanned_db):
    c = cand_by(scanned_db, "ZSMPB", "anomaly")
    for bad_reason, bad_ev in (("", {"x": 1}), ("ok", {})):
        with pytest.raises(cands.TransitionError):
            with tx(scanned_db):
                cands.transition(scanned_db, c["candidate_id"], "REJECTED", actor="t", reason=bad_reason, evidence=bad_ev)
    with tx(scanned_db):
        cands.transition(scanned_db, c["candidate_id"], "REJECTED", actor="operator:t", reason="operator decision",
                         evidence={"note": "test"}, reason_type="OPERATOR_DECISION")
    for target in ("DETECTED", "RESEARCHED", "BLOCKED", "EXPIRED"):
        with pytest.raises(cands.TransitionError, match="invalid transition"):
            with tx(scanned_db):
                cands.transition(scanned_db, c["candidate_id"], target, actor="t", reason="r", evidence={"x": 1})
    t = scanned_db.execute("SELECT * FROM state_transitions WHERE candidate_id=? ORDER BY rowid", (c["candidate_id"],)).fetchall()
    assert [(x["from_state"], x["to_state"]) for x in t] == [(None, "DETECTED"), ("DETECTED", "REJECTED")]
    assert json.loads(t[1]["evidence_json"]) == {"note": "test"}
    with pytest.raises(Exception, match="append-only"):
        scanned_db.execute("UPDATE state_transitions SET reason='x' WHERE candidate_id=?", (c["candidate_id"],))


def test_reserved_states_unreachable_in_code_and_database(scanned_db):
    c = cand_by(scanned_db, "ZSMPA", "anomaly")
    for st in ("CONDITIONAL_READY", "ENTRY_ELIGIBLE"):
        with pytest.raises(cands.TransitionError, match="reserved"):
            with tx(scanned_db):
                cands.transition(scanned_db, c["candidate_id"], st, actor="t", reason="r", evidence={"x": 1})
        with pytest.raises(Exception, match="reserved state"):
            scanned_db.execute("UPDATE candidates SET state=? WHERE candidate_id=?", (st, c["candidate_id"]))


def test_research_blocked_then_cleared_with_evidence(scanned_db, clock):
    r1 = research.import_research(scanned_db, load("ZSMPA_initial"))
    assert r1["state"] == "BLOCKED"
    c = cands.get(scanned_db, r1["candidate_id"])
    assert c["research_status"] == "COMPLETED" and c["next_recheck_at_utc"] == "2026-09-30T12:03:00Z"
    # A revision that drops a blocker without clearing evidence is refused
    rev = load("ZSMPA_revision")
    bad = copy.deepcopy(rev)
    bad["cleared_blockers"] = bad["cleared_blockers"][:1]
    with pytest.raises(AstraError, match="neither retained nor cleared"):
        research.import_research(scanned_db, bad)
    # Direct move to RESEARCHED while blockers are open is refused
    with pytest.raises(cands.TransitionError, match="blockers remain open"):
        with tx(scanned_db):
            cands.transition(scanned_db, c["candidate_id"], "RESEARCHED", actor="t", reason="r", evidence={"x": 1})
    clock.advance(minutes=30)
    r2 = research.import_research(scanned_db, rev)
    assert r2["state"] == "RESEARCHED" and sorted(r2["cleared_blockers"]) == ["catalyst-unidentified", "liquidity-unverified"]
    rows = scanned_db.execute("SELECT key, status, clearing_evidence_json FROM blockers WHERE candidate_id=?", (c["candidate_id"],)).fetchall()
    assert all(r["status"] == "cleared" and json.loads(r["clearing_evidence_json"])["source_refs"] for r in rows)
    last = scanned_db.execute("SELECT * FROM state_transitions WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (c["candidate_id"],)).fetchone()
    assert last["reason_type"] == "NEW_EVENT" and json.loads(last["remaining_blockers_json"]) == []
    # originals are preserved: both research records remain
    assert scanned_db.execute("SELECT count(*) FROM research_records WHERE candidate_id=?", (c["candidate_id"],)).fetchone()[0] == 2


def test_revision_requires_parent_and_reason(scanned_db):
    research.import_research(scanned_db, load("ZSMPA_initial"))
    rev = load("ZSMPA_revision")
    rev.pop("revision_reason_type")
    with pytest.raises(AstraError, match="revision_reason_type"):
        research.import_research(scanned_db, rev)
    rev = load("ZSMPA_revision")
    rev["parent_research_id"] = None
    with pytest.raises(AstraError, match="parent_research_id"):
        research.import_research(scanned_db, rev)


def test_schema_rejects_targets_states_and_forced_fields(scanned_db):
    base = load("ZSMPF_announcement")
    for field, value in (("target_price", 99.0), ("state", "ENTRY_ELIGIBLE"), ("expected_return", 0.5)):
        rec = copy.deepcopy(base)
        rec[field] = value
        with pytest.raises(ValidationError, match="research record invalid"):
            research.import_research(scanned_db, rec)
    rec = copy.deepcopy(base)
    rec["subjective_probabilities"] = [{"event": "close above 70 by deadline", "deadline_utc": "2026-10-02T20:00:00Z",
                                        "probability": 0.3, "label": "CALIBRATED"}]
    with pytest.raises(ValidationError):
        research.import_research(scanned_db, rec)
    rec = copy.deepcopy(base)
    rec["facts"][0]["source_refs"] = ["S9"]
    with pytest.raises(ValidationError, match="not defined in sources"):
        research.import_research(scanned_db, rec)


def test_missing_optional_information_blocks_only_dependent_conclusions(scanned_db):
    r = research.import_research(scanned_db, load("ZSMPF_announcement"))
    assert r["state"] == "RESEARCHED"  # unknown valuation/financing did not block the whole record
    un = research.unresolved(scanned_db, r["candidate_id"])
    blocked = {s["section"]: s["blocks_conclusions"] for s in un["unknown_sections"]}
    assert blocked["valuation_scale"] == ["valuation_scale"] and blocked["financing"] == ["per_share_accretion"]


def test_ai_research_cannot_reject_and_sample_flags_enforced(scanned_db):
    rec = load("ZSMPG_anomaly")
    rec["researcher"] = {"kind": "ai", "name": "x", "model": "m"}
    rec["is_sample"] = True
    with pytest.raises(ValidationError, match="cannot reject"):
        research.import_research(scanned_db, rec)
    rec = load("ZSMPG_anomaly")
    rec["is_sample"] = False
    with pytest.raises(ValidationError, match="is_sample"):
        research.import_research(scanned_db, rec)


def test_research_cannot_predate_detection(scanned_db):
    rec = load("ZSMPF_announcement")
    rec["completed_at"] = "2026-09-17T12:00:00Z"
    with pytest.raises(AstraError, match="precedes candidate detection"):
        research.import_research(scanned_db, rec)


def test_terminal_candidates_accept_no_research(scanned_db):
    r = research.import_research(scanned_db, load("ZSMPG_anomaly"))
    assert r["state"] == "REJECTED"
    rec = load("ZSMPG_anomaly")
    rec["candidate_selector"] = None
    rec["candidate_id"] = r["candidate_id"]
    rec["disposition_rationale"] = "second opinion"
    with pytest.raises(AstraError, match="terminal"):
        research.import_research(scanned_db, rec)
    c = cands.get(scanned_db, r["candidate_id"])
    assert c["expires_at_utc"] is None and c["expiry_condition"].startswith("none: candidate REJECTED")
    pending = scanned_db.execute("SELECT count(*) FROM work_items WHERE candidate_id=? AND status='pending'", (r["candidate_id"],)).fetchone()[0]
    assert pending == 0


def test_untrusted_text_cannot_change_state(scanned_db):
    g = cand_by(scanned_db, "ZSMPG", "announcement")
    assert g["state"] == "DETECTED"  # the "set ENTRY_ELIGIBLE" title is inert data
    ev = scanned_db.execute("SELECT content_json FROM evidence_snapshots WHERE evidence_id=?", (g["origin_evidence_id"],)).fetchone()
    assert "IGNORE PREVIOUS INSTRUCTIONS" in ev[0] and '"entry_permission":false' in ev[0]
