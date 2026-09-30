"""Candidate lifecycle. Research state only: no holdings, positions or orders live here.

Transition map (docs/STATE_MACHINE.md):

    (new) -> DETECTED
    DETECTED   -> RESEARCHED | BLOCKED | REJECTED | EXPIRED
    RESEARCHED -> BLOCKED | REJECTED | EXPIRED
    BLOCKED    -> RESEARCHED (all blockers cleared with evidence) | REJECTED | EXPIRED
    REJECTED, EXPIRED: terminal (new material evidence creates a new candidate)
    CONDITIONAL_READY, ENTRY_ELIGIBLE: RESERVED, unreachable in this prototype
      (also blocked by database triggers).

Every transition stores actor, reason, evidence delta and the remaining open blockers.
Functions here expect the caller to hold a transaction.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import calendar as cal
from .core import AstraError, canonical_json, fmt_utc, new_id, now, now_str, parse_utc

OPEN_STATES = ("DETECTED", "RESEARCHED", "BLOCKED")
TERMINAL_STATES = ("REJECTED", "EXPIRED")
RESERVED_STATES = ("CONDITIONAL_READY", "ENTRY_ELIGIBLE")
TRANSITIONS: dict[str, set[str]] = {
    "DETECTED": {"RESEARCHED", "BLOCKED", "REJECTED", "EXPIRED"},
    "RESEARCHED": {"BLOCKED", "REJECTED", "EXPIRED"},
    "BLOCKED": {"RESEARCHED", "REJECTED", "EXPIRED"},
    "REJECTED": set(),
    "EXPIRED": set(),
}
REASON_TYPES = ("NEW_EVENT", "PRICE_OR_CONDITION_CHANGE", "CORRECTED_INPUT", "NEWLY_FOUND_OLD_INFORMATION",
                "CHANGED_INFERENCE", "INITIAL_RESEARCH", "EXPIRY", "OPERATOR_DECISION")
DEFAULT_OWNERS = {"anomaly": "astra:anomaly-lane (unassigned)", "announcement": "astra:disclosure-lane (unassigned)"}


class TransitionError(AstraError):
    pass


class ConcurrencyError(AstraError):
    pass


def get(conn: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if not row:
        raise AstraError(f"unknown candidate {candidate_id}")
    return row


def open_blockers(conn: sqlite3.Connection, candidate_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT key, description, blocks_json, clearing_evidence_required, owner FROM blockers "
        "WHERE candidate_id = ? AND status = 'open' ORDER BY opened_at_utc, key",
        (candidate_id,),
    ).fetchall()
    return [{"key": r["key"], "description": r["description"], "blocks": json.loads(r["blocks_json"]),
             "clearing_evidence": r["clearing_evidence_required"], "owner": r["owner"]} for r in rows]


def add_event(conn: sqlite3.Connection, candidate_id: str, kind: str, actor: str, detail: dict) -> str:
    eid = new_id("cev")
    conn.execute("INSERT INTO candidate_events VALUES (?,?,?,?,?,?)",
                 (eid, candidate_id, now_str(), kind, actor, canonical_json(detail)))
    return eid


def transition(conn: sqlite3.Connection, candidate_id: str, to_state: str, *, actor: str, reason: str,
               evidence: dict, reason_type: str | None = None, run_id: str | None = None,
               extra_updates: dict | None = None) -> str:
    cand = get(conn, candidate_id)
    frm = cand["state"]
    if to_state in RESERVED_STATES:
        raise TransitionError(
            f"{to_state} is reserved for future implementation; the prototype cannot grant trading readiness "
            "through model output, research completeness or operator checkbox"
        )
    if to_state not in TRANSITIONS.get(frm, set()):
        raise TransitionError(f"invalid transition {frm} -> {to_state} for {candidate_id}")
    if not reason or not reason.strip():
        raise TransitionError("a transition requires a non-empty reason")
    if not evidence:
        raise TransitionError("a transition requires evidence (non-empty)")
    if reason_type is not None and reason_type not in REASON_TYPES:
        raise TransitionError(f"reason_type must be one of {REASON_TYPES}")
    if to_state == "RESEARCHED" and open_blockers(conn, candidate_id):
        raise TransitionError("cannot move to RESEARCHED while blockers remain open; clear them with evidence")
    updates = {"state": to_state, "updated_at_utc": now_str()}
    if to_state in TERMINAL_STATES:
        updates.update({"next_recheck_at_utc": None, "next_recheck_condition": f"none: candidate {to_state}",
                        "expires_at_utc": None,
                        "expiry_condition": f"none: candidate {to_state} (previous expiry {cand['expires_at_utc'] or 'none'})"})
    updates.update(extra_updates or {})
    sets = ", ".join(f"{k} = ?" for k in updates)
    cur = conn.execute(
        f"UPDATE candidates SET {sets}, version = version + 1 WHERE candidate_id = ? AND version = ?",
        (*updates.values(), candidate_id, cand["version"]),
    )
    if cur.rowcount != 1:
        raise ConcurrencyError(f"candidate {candidate_id} changed concurrently; retry")
    tid = new_id("tr")
    conn.execute(
        "INSERT INTO state_transitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, candidate_id, frm, to_state, now_str(), actor, reason.strip(), reason_type,
         canonical_json(evidence), canonical_json(open_blockers(conn, candidate_id)), run_id),
    )
    if to_state in TERMINAL_STATES:
        conn.execute(
            "UPDATE work_items SET status='cancelled', updated_at_utc=?, last_error=? "
            "WHERE candidate_id=? AND status IN ('pending','leased') AND kind IN ('recheck','expiry','research_request')",
            (now_str(), f"cancelled: candidate {to_state}", candidate_id),
        )
    from . import notifications  # local import avoids a cycle

    notifications.queue_for_candidate(conn, candidate_id, kind="state_changed", dedup_suffix=tid,
                                      priority="urgent" if to_state == "BLOCKED" else "routine",
                                      note=f"{frm} -> {to_state}: {reason.strip()}")
    return tid


def default_expiry(detected_at: datetime, sessions: int) -> tuple[str | None, str]:
    """Close of the N-th US session whose close is after the detection time."""
    try:
        d = cal.local_date(detected_at)
        s = cal.session(d)
        first = d if (s is not None and detected_at < s.close_utc) else cal.next_session(d)
        end = cal.add_sessions(first, sessions - 1) if sessions > 1 else first
        return fmt_utc(cal.session(end).close_utc), (
            f"default: no completed research by the close of the {sessions}th US session after detection ({end.isoformat()})"
        )
    except cal.CalendarUnavailable as exc:
        return None, f"UNKNOWN: calendar unavailable ({exc})"


def create(conn: sqlite3.Connection, *, route: str, mode: str, symbol: str | None, dedup_key: str,
           origin_run_id: str, origin_evidence_id: str, data_cutoff_utc: str | None,
           simulated_cutoff_utc: str | None, observation_class: str | None = None,
           announcement_id: str | None = None, mechanism_hint: str | None = None,
           issuer_cik: str | None = None, reason: str, expiry_sessions: int = 5) -> tuple[str, bool]:
    """Create a DETECTED candidate, or return the existing one for the same dedup key."""
    row = conn.execute("SELECT candidate_id FROM candidates WHERE dedup_key = ?", (dedup_key,)).fetchone()
    if row:
        return row["candidate_id"], False
    cid = new_id("cand")
    ts = now_str()
    if mode == "replay":
        expires_at, expiry_condition = None, "replay candidate: no expiry scheduled (retrospective evaluation only)"
        recheck_condition = "replay candidate: no rechecks scheduled"
    else:
        expires_at, expiry_condition = default_expiry(now(), expiry_sessions)
        recheck_condition = "UNKNOWN: no recheck scheduled until research sets one"
    conn.execute(
        "INSERT INTO candidates(candidate_id,route,mode,symbol,issuer_cik,dedup_key,state,owner,created_at_utc,"
        "detected_at_utc,simulated_cutoff_utc,data_cutoff_utc,origin_run_id,origin_evidence_id,announcement_id,"
        "observation_class,mechanism_hint,next_recheck_at_utc,next_recheck_condition,expires_at_utc,expiry_condition,"
        "research_status,latest_research_id,updated_at_utc,version) VALUES "
        "(?,?,?,?,?,?,'DETECTED',?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,'QUEUED',NULL,?,1)",
        (cid, route, mode, symbol, issuer_cik, dedup_key, DEFAULT_OWNERS[route], ts, ts, simulated_cutoff_utc,
         data_cutoff_utc, origin_run_id, origin_evidence_id, announcement_id, observation_class, mechanism_hint,
         recheck_condition, expires_at, expiry_condition, ts),
    )
    conn.execute("INSERT INTO candidate_evidence VALUES (?,?,?,?)", (cid, origin_evidence_id, "origin", ts))
    conn.execute(
        "INSERT INTO state_transitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("tr"), cid, None, "DETECTED", ts, "system", reason, None,
         canonical_json({"evidence_id": origin_evidence_id, "run_id": origin_run_id}), "[]", origin_run_id),
    )
    if mode != "replay":
        from . import notifications, work

        work.schedule(conn, kind="research_request", candidate_id=cid, due_at=ts, priority="routine",
                      dedup_key=f"research_request:initial:{cid}", condition="initial research after detection")
        if expires_at:
            work.schedule(conn, kind="expiry", candidate_id=cid, due_at=expires_at, priority="urgent",
                          dedup_key=f"expiry:{cid}:{expires_at}", condition=expiry_condition)
        notifications.queue_for_candidate(conn, cid, kind="candidate_detected", dedup_suffix="initial",
                                          priority="routine", note=reason)
    return cid, True


def attach_evidence(conn: sqlite3.Connection, candidate_id: str, evidence_id: str, role: str, actor: str,
                    detail: dict) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO candidate_evidence VALUES (?,?,?,?)",
                       (candidate_id, evidence_id, role, now_str()))
    if cur.rowcount:
        add_event(conn, candidate_id, role, actor, {"evidence_id": evidence_id, **detail})
        return True
    return False


def assign_owner(conn: sqlite3.Connection, candidate_id: str, owner: str, actor: str) -> None:
    cand = get(conn, candidate_id)
    if not owner.strip():
        raise AstraError("owner must be non-empty")
    conn.execute("UPDATE candidates SET owner=?, updated_at_utc=?, version=version+1 WHERE candidate_id=?",
                 (owner.strip(), now_str(), candidate_id))
    add_event(conn, candidate_id, "owner_changed", actor, {"from": cand["owner"], "to": owner.strip()})


def open_anomaly_candidate(conn: sqlite3.Connection, mode: str, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM candidates WHERE route='anomaly' AND mode=? AND symbol=? AND state IN ('DETECTED','RESEARCHED','BLOCKED')",
        (mode, symbol),
    ).fetchone()


def is_due(ts: str | None) -> bool:
    return bool(ts) and parse_utc(ts) <= now()


def within(ts: str, seconds: int) -> bool:
    return parse_utc(ts) >= now() - timedelta(seconds=seconds)
