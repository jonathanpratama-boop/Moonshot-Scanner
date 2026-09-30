"""Local notification preview queue.

Statuses: queued (created, not delivered) -> attempted (written to the local preview
outbox) -> acknowledged (an operator explicitly acknowledged it). External delivery is
NOT configured in this prototype; attempting it records 'not_configured'.

Payloads carry the ASTRA-OPS r1.2 decision row: every required field is present, either
with a value or as {"value": "UNKNOWN", "reason": ...}. Nothing is inferred to fill gaps.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import OPS_REVISION
from .config import settings
from .core import AstraError, canonical_json, new_id, now_str, to_zone_str
from .db import tx

REQUIRED_ROW_FIELDS = (
    "candidate_id", "state", "source_publication_time", "public_availability_time",
    "first_actual_observation_time", "quote_or_reference", "unresolved_dependencies", "owner",
    "next_recheck", "expiry", "delivery_time",
)


def unknown(reason: str) -> dict:
    return {"value": "UNKNOWN", "reason": reason}


def _times(utc_s: str | None, reason_if_missing: str) -> dict:
    if not utc_s:
        return unknown(reason_if_missing)
    out = {"utc": utc_s}
    for z in settings().display_timezones:
        out[z] = to_zone_str(utc_s, z)
    return out


def decision_row(conn: sqlite3.Connection, cand: sqlite3.Row) -> dict:
    from .candidates import open_blockers

    ann = None
    if cand["announcement_id"]:
        ann = conn.execute("SELECT * FROM announcements WHERE announcement_id = ?", (cand["announcement_id"],)).fetchone()
    ev = conn.execute("SELECT content_json FROM evidence_snapshots WHERE evidence_id = ?",
                      (cand["origin_evidence_id"],)).fetchone()
    evc = json.loads(ev["content_json"]) if ev else {}
    if ann is not None:
        pub = _times(ann["published_at_utc"], "source supplied no publication time")
        if ann["published_at_utc"]:
            pub.update({"raw": ann["published_raw"], "source_tz": ann["published_tz"], "kind": ann["published_time_kind"]})
        if ann["published_time_kind"] == "SEC_ACCEPTANCE":
            avail = {**_times(ann["published_at_utc"], ""), "basis": "SEC EDGAR acceptance time (filings coverage only)"}
        else:
            avail = unknown("public availability time not established by this source")
        first_obs = {**_times(ann["first_observed_at_utc"], ""), "basis": "actual ASTRA first observation (software clock)"}
        quote = unknown("no executable quote feed in this prototype; reference prices are bar closes, see outcomes")
    else:
        pub = unknown("anomaly route: no source publication (TAPE)")
        avail = unknown("anomaly route: no source publication")
        first_obs = {**_times(cand["detected_at_utc"], ""), "basis": "actual ASTRA detection time (software clock)"}
        ref = evc.get("signal_bar_reference") or {}
        quote = {
            "reference_role": "SIGNAL_BAR_REFERENCE",
            "bar_close": ref.get("close"),
            "bar_end": _times(ref.get("bar_end_utc"), "signal bar end unknown"),
            "session": ref.get("session_class", "RTH"),
            "scoring_baseline_eligible": False,
            "executable_quote": unknown("no contemporaneous executable quote retained"),
        }
        if evc.get("feature_available_at_utc"):
            quote["feature_available"] = _times(evc["feature_available_at_utc"], "")
    from .research import unresolved

    blockers = open_blockers(conn, cand["candidate_id"])
    deps: Any = [{"blocker": b["description"], "blocks": b["blocks"], "clearing_evidence": b["clearing_evidence"]}
                 for b in blockers]
    if cand["research_status"] == "COMPLETED" or cand["latest_research_id"]:
        un = unresolved(conn, cand["candidate_id"])
        deps += [{"unknown": u["item"], "blocks": u.get("blocks_conclusions", []),
                  "clearing_evidence": "UNKNOWN: not specified by research"} for u in un["unknowns"]]
        deps += [{"unknown_section": s["section"], "blocks": s["blocks_conclusions"],
                  "clearing_evidence": "UNKNOWN: not specified by research"} for s in un["unknown_sections"] if s["blocks_conclusions"]]
    if not deps:
        deps = unknown("research not yet completed") if cand["research_status"] != "COMPLETED" else []
    recheck = (_times(cand["next_recheck_at_utc"], "") if cand["next_recheck_at_utc"]
               else unknown(cand["next_recheck_condition"] or "no recheck scheduled"))
    if cand["next_recheck_at_utc"]:
        recheck["condition"] = cand["next_recheck_condition"]
    expiry = (_times(cand["expires_at_utc"], "") if cand["expires_at_utc"]
              else unknown(cand["expiry_condition"] or "no expiry scheduled"))
    if cand["expires_at_utc"]:
        expiry["condition"] = cand["expiry_condition"]
    return {
        "ops_revision": OPS_REVISION,
        "candidate_id": cand["candidate_id"],
        "state": cand["state"],
        "route": cand["route"],
        "symbol": cand["symbol"],
        "mode": cand["mode"],
        "sample_data": cand["mode"] == "sample",
        "observation_class": cand["observation_class"],
        "source_publication_time": pub,
        "public_availability_time": avail,
        "first_actual_observation_time": first_obs,
        "quote_or_reference": quote,
        "unresolved_dependencies": deps,
        "owner": cand["owner"],
        "next_recheck": recheck,
        "expiry": expiry,
        "delivery_time": unknown("delivery is not confirmed until an operator acknowledges it"),
        "evidence_scope": "current-run evidence frozen at detection; research and history retrieved from the local database",
        "entry_permission": False,
    }


def validate_decision_row(row: dict) -> list[str]:
    """Names of required fields that are missing or empty (must be value or explicit UNKNOWN)."""
    bad = []
    for f in REQUIRED_ROW_FIELDS:
        v = row.get(f)
        if v is None or v == {} or v == "":
            bad.append(f)
        elif isinstance(v, dict) and v.get("value") == "UNKNOWN" and not v.get("reason"):
            bad.append(f)
    return bad


def queue(conn: sqlite3.Connection, *, dedup_key: str, kind: str, priority: str, mode: str,
          payload: dict, candidate_id: str | None = None) -> bool:
    """Insert once per dedup key. Replay mode never queues notifications."""
    if mode == "replay":
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications(notification_id,dedup_key,candidate_id,kind,priority,mode,payload_json,"
        "created_at_utc,status) VALUES (?,?,?,?,?,?,?,?,'queued')",
        (new_id("ntf"), dedup_key, candidate_id, kind, priority, mode, canonical_json(payload), now_str()),
    )
    return cur.rowcount == 1


def queue_for_candidate(conn: sqlite3.Connection, candidate_id: str, *, kind: str, dedup_suffix: str,
                        priority: str, note: str) -> bool:
    cand = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if cand is None or cand["mode"] == "replay":
        return False
    row = decision_row(conn, cand)
    missing = validate_decision_row(row)
    if missing:  # defensive: should be impossible because decision_row fills UNKNOWN with reasons
        raise AstraError(f"decision row incomplete: {missing}")
    payload = {"kind": kind, "note": note, "decision_row": row}
    return queue(conn, dedup_key=f"{kind}:{candidate_id}:{dedup_suffix}", kind=kind, priority=priority,
                 mode=cand["mode"], payload=payload, candidate_id=candidate_id)


def render_text(n: sqlite3.Row) -> str:
    p = json.loads(n["payload_json"])
    row = p.get("decision_row", {})
    lines = [f"[{n['priority'].upper()}] {n['kind']} {n['notification_id']} ({n['mode'].upper()})"]
    if row.get("sample_data"):
        lines.append("SAMPLE DATA - synthetic, not a market observation")
    lines.append(p.get("note", ""))
    for k in ("ops_revision", *REQUIRED_ROW_FIELDS):
        if k in row:
            lines.append(f"  {k}: {json.dumps(row[k], ensure_ascii=False)}")
    for k, v in p.items():
        if k not in ("decision_row", "note", "kind"):
            lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines)


def deliver(conn: sqlite3.Connection, channel: str = "local_outbox", limit: int = 50) -> dict:
    """Attempt delivery of queued notifications. Only the local preview outbox exists."""
    rows = conn.execute(
        "SELECT * FROM notifications WHERE status='queued' ORDER BY priority='urgent' DESC, created_at_utc LIMIT ?",
        (limit,),
    ).fetchall()
    out = {"channel": channel, "attempted": 0, "not_configured": 0, "failed": 0, "files": []}
    outbox = Path(settings().outbox_dir)
    for n in rows:
        with tx(conn):
            if channel != "local_outbox":
                conn.execute("INSERT INTO notification_attempts VALUES (?,?,?,?,?,?)",
                             (new_id("nat"), n["notification_id"], channel, now_str(), "not_configured",
                              "external delivery is not configured in this prototype"))
                out["not_configured"] += 1
                continue
            try:
                outbox.mkdir(parents=True, exist_ok=True)
                path = outbox / f"{n['notification_id']}.txt"
                path.write_text(render_text(n) + "\n", encoding="utf-8")
                conn.execute("INSERT INTO notification_attempts VALUES (?,?,?,?,?,?)",
                             (new_id("nat"), n["notification_id"], channel, now_str(), "written_to_local_outbox", str(path)))
                conn.execute("UPDATE notifications SET status='attempted', last_attempt_at_utc=? WHERE notification_id=?",
                             (now_str(), n["notification_id"]))
                out["attempted"] += 1
                out["files"].append(str(path))
            except OSError as exc:
                conn.execute("INSERT INTO notification_attempts VALUES (?,?,?,?,?,?)",
                             (new_id("nat"), n["notification_id"], channel, now_str(), "failed", str(exc)))
                out["failed"] += 1
    return out


def acknowledge(conn: sqlite3.Connection, notification_id: str, by: str) -> None:
    with tx(conn):
        n = conn.execute("SELECT status FROM notifications WHERE notification_id=?", (notification_id,)).fetchone()
        if not n:
            raise AstraError(f"unknown notification {notification_id}")
        if n["status"] != "attempted":
            raise AstraError(f"notification is {n['status']}; only an attempted (delivered to preview) notification can be acknowledged")
        conn.execute("UPDATE notifications SET status='acknowledged', acknowledged_at_utc=?, acknowledged_by=? WHERE notification_id=?",
                     (now_str(), by, notification_id))
