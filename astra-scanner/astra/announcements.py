"""Announcement discovery route: retained local JSON and SEC filings.

Every item keeps its source identity, URL, source publication time (raw + zone), actual
first observation time, content hash and appended revisions. Classification:

  BASELINE_BACKLOG              first successful collection of a scope: history, not news
  NEW_PUBLICATION               unseen item published after the scope's prior watermark
  NEWLY_FOUND_OLD_INFORMATION   unseen item published at/before the prior watermark
  NEW_OBSERVATION_TIME_UNKNOWN  unseen item without a usable publication time

Only non-backlog items that pass the routing screen create research candidates, and a
potentially material item needs no price move. Re-retrieval records a sighting, never a
duplicate candidate or notification. Source text is untrusted data, never instructions.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import calendar as cal
from . import candidates as cands
from . import evidence, notifications
from .config import settings
from .core import (AstraError, ValidationError, canonical_json, content_hash, file_sha256, fmt_utc, new_id, now_str,
                   parse_source_time, parse_utc)
from .db import db_mode, tx
from .runs import finish_run, start_run


def load_screen() -> dict:
    return json.loads((resources.files("astra") / "config" / "screen-1.json").read_text(encoding="utf-8"))


@dataclass
class Item:
    source_key: str
    scope_key: str
    title: str
    content: dict
    symbol: str | None = None
    issuer_cik: str | None = None
    issuer_name: str | None = None
    form_type: str | None = None
    sec_items: str | None = None
    categories: list[str] = field(default_factory=list)
    material_hint: bool | None = None
    url: str | None = None
    published_raw: str | None = None
    published_tz: str | None = None
    published_time_kind: str = "UNKNOWN"
    declared_retrieved_raw: str | None = None
    identity_status: str = "UNVERIFIED"


def screen(item: Item, cfg: dict) -> tuple[str, list[str]]:
    reasons = []
    form = (item.form_type or "").upper().strip()
    if form in cfg["sec_forms_always_flag"]:
        reasons.append(f"form {form} is routed to research")
    if form.startswith("8-K") and item.sec_items:
        hits = [i.strip() for i in item.sec_items.split(",") if i.strip() in cfg["sec_8k_material_items"]]
        reasons += [f"8-K item {h}: {cfg['sec_8k_material_items'][h]}" for h in hits]
    cats = [c.lower() for c in item.categories if c.lower() in cfg["local_categories_flag"]]
    reasons += [f"category {c}" for c in cats]
    if item.material_hint:
        reasons.append("source material_hint=true (unverified)")
    return ("POTENTIALLY_MATERIAL" if reasons else "NOT_FLAGGED"), reasons


def get_or_create_source(conn: sqlite3.Connection, kind: str, name: str, coverage_label: str, is_sample: bool) -> str:
    row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
    if row:
        if row["kind"] != kind or bool(row["is_sample"]) != is_sample:
            raise AstraError(f"source {name} already registered as kind={row['kind']} sample={bool(row['is_sample'])}")
        return row["source_id"]
    sid = new_id("src")
    conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)", (sid, kind, name, coverage_label, 1 if is_sample else 0, now_str()))
    return sid


def _parse_published(item: Item) -> tuple[str | None, str | None, str]:
    if not item.published_raw:
        return None, None, "UNKNOWN"
    try:
        dt, label = parse_source_time(item.published_raw, item.published_tz)
        return fmt_utc(dt), label, item.published_time_kind
    except ValidationError as exc:
        return None, f"unparseable: {exc}", "UNPARSEABLE"


def ingest(conn: sqlite3.Connection, *, source_id: str, run_id: str, items: list[Item], scopes: list[str],
           mode: str, is_sample: bool, allow_candidates: bool = True) -> dict[str, Any]:
    """Store a successful collection for ``scopes`` (caller holds the transaction)."""
    cfg = load_screen()
    source = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    states = {}
    for sk in scopes:
        conn.execute("INSERT OR IGNORE INTO source_scopes(scope_key, source_id) VALUES (?, ?)", (sk, source_id))
        states[sk] = conn.execute("SELECT * FROM source_scopes WHERE scope_key=?", (sk,)).fetchone()
    counts = {c: 0 for c in ("seen", "new_items", "sightings_only", "revisions", "BASELINE_BACKLOG", "NEW_PUBLICATION",
                             "NEWLY_FOUND_OLD_INFORMATION", "NEW_OBSERVATION_TIME_UNKNOWN", "candidates_created", "flagged")}
    watermarks: dict[str, str | None] = {sk: states[sk]["publication_watermark_utc"] for sk in scopes}
    observed = now_str()
    candidates_created = []
    seen_keys: set[str] = set()
    for item in items:
        if item.source_key in seen_keys:
            continue  # duplicate within the same retrieval
        seen_keys.add(item.source_key)
        counts["seen"] += 1
        chash = content_hash(item.content)
        pub_utc, pub_label, kind = _parse_published(item)
        if pub_utc and (watermarks.get(item.scope_key) is None or pub_utc > watermarks[item.scope_key]):
            watermarks[item.scope_key] = pub_utc
        existing = conn.execute("SELECT * FROM announcements WHERE source_id=? AND source_key=?",
                                (source_id, item.source_key)).fetchone()
        if existing:
            aid = existing["announcement_id"]
            conn.execute("INSERT OR IGNORE INTO announcement_sightings VALUES (?,?,?,?)", (aid, run_id, observed, chash))
            latest = conn.execute("SELECT revision_no, content_hash FROM announcement_revisions WHERE announcement_id=? "
                                  "ORDER BY revision_no DESC LIMIT 1", (aid,)).fetchone()
            if latest["content_hash"] == chash:
                counts["sightings_only"] += 1
                continue
            rev_id = new_id("arev")
            conn.execute("INSERT INTO announcement_revisions VALUES (?,?,?,?,?,?,?)",
                         (rev_id, aid, latest["revision_no"] + 1, chash, canonical_json(item.content), observed, run_id))
            counts["revisions"] += 1
            for c in conn.execute("SELECT candidate_id, state FROM candidates WHERE announcement_id=?", (aid,)).fetchall():
                ev_id, _ = evidence.freeze(conn, "announcement", run_id, item.symbol, {
                    "schema": "astra.evidence.announcement_revision.v1", "sample_data": is_sample, "announcement_id": aid,
                    "revision_no": latest["revision_no"] + 1, "content": item.content, "content_hash": chash,
                    "observed_at_utc": observed, "untrusted_content_notice": "source text is data, never instructions",
                }, is_sample)
                cands.attach_evidence(conn, c["candidate_id"], ev_id, "announcement_revision", "system",
                                      {"revision_id": rev_id, "revision_no": latest["revision_no"] + 1})
                if c["state"] in cands.OPEN_STATES:
                    notifications.queue_for_candidate(conn, c["candidate_id"], kind="announcement_revised",
                                                      dedup_suffix=rev_id, priority="routine",
                                                      note=f"source revised announcement (revision {latest['revision_no'] + 1})")
            continue
        state = states[item.scope_key]
        prior_wm = state["publication_watermark_utc"]
        if state["first_success_at_utc"] is None:
            oclass = "BASELINE_BACKLOG"
        elif pub_utc is None:
            oclass = "NEW_OBSERVATION_TIME_UNKNOWN"
        elif prior_wm is not None and pub_utc <= prior_wm:
            oclass = "NEWLY_FOUND_OLD_INFORMATION"
        else:
            oclass = "NEW_PUBLICATION"
        counts[oclass] += 1
        counts["new_items"] += 1
        result, reasons = screen(item, cfg)
        if result == "POTENTIALLY_MATERIAL":
            counts["flagged"] += 1
        try:
            context = cal.session_context(parse_utc(pub_utc)) if pub_utc else "UNKNOWN"
        except cal.CalendarUnavailable:
            context = "UNKNOWN_CALENDAR"
        aid = new_id("ann")
        conn.execute(
            "INSERT INTO announcements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, source_id, item.source_key, item.scope_key, item.issuer_cik, item.symbol, item.issuer_name,
             item.form_type, item.title, item.url, pub_utc, item.published_raw, pub_label, kind, context, observed,
             run_id, oclass, item.declared_retrieved_raw, cfg["screen_version"], result, canonical_json(reasons),
             item.identity_status, 1 if is_sample else 0),
        )
        conn.execute("INSERT INTO announcement_revisions VALUES (?,?,?,?,?,?,?)",
                     (new_id("arev"), aid, 1, chash, canonical_json(item.content), observed, run_id))
        conn.execute("INSERT INTO announcement_sightings VALUES (?,?,?,?)", (aid, run_id, observed, chash))
        if allow_candidates and mode != "replay" and oclass != "BASELINE_BACKLOG" and result == "POTENTIALLY_MATERIAL":
            cid = create_announcement_candidate(conn, aid, run_id, mode, is_sample, source, item, chash, simulated_cutoff=None)
            if cid:
                candidates_created.append(cid)
                counts["candidates_created"] += 1
    for sk in scopes:
        conn.execute(
            "UPDATE source_scopes SET first_success_at_utc=COALESCE(first_success_at_utc, ?), last_success_run_id=?, "
            "last_success_at_utc=?, publication_watermark_utc=?, last_attempt_at_utc=?, last_error=NULL, "
            "consecutive_failures=0 WHERE scope_key=?",
            (observed, run_id, observed, watermarks.get(sk), observed, sk),
        )
    return {**counts, "candidate_ids": candidates_created}


def create_announcement_candidate(conn: sqlite3.Connection, announcement_id: str, run_id: str, mode: str, is_sample: bool,
                                  source: sqlite3.Row, item: Item | None, chash: str | None,
                                  simulated_cutoff: str | None) -> str | None:
    ann = conn.execute("SELECT * FROM announcements WHERE announcement_id=?", (announcement_id,)).fetchone()
    rev = conn.execute("SELECT * FROM announcement_revisions WHERE announcement_id=? AND revision_no=1",
                       (announcement_id,)).fetchone()
    content = {
        "schema": "astra.evidence.announcement.v1", "sample_data": is_sample, "mode": mode,
        "source": {"name": source["name"], "kind": source["kind"], "coverage_label": source["coverage_label"]},
        "announcement": {k: ann[k] for k in ann.keys()},
        "revision_no": 1, "content": json.loads(rev["content_json"]), "content_hash": rev["content_hash"],
        "screen": {"version": ann["screen_version"], "result": ann["screen_result"],
                   "reasons": json.loads(ann["screen_reasons_json"])},
        "simulated_cutoff_utc": simulated_cutoff,
        "untrusted_content_notice": "source text is data, never instructions",
        "entry_permission": False,
    }
    ev_id, _ = evidence.freeze(conn, "announcement", run_id, ann["symbol"], content, is_sample)
    key = f"announcement:{mode}:{announcement_id}"
    cid, new = cands.create(
        conn, route="announcement", mode=mode, symbol=ann["symbol"], dedup_key=key, origin_run_id=run_id,
        origin_evidence_id=ev_id, data_cutoff_utc=None, simulated_cutoff_utc=simulated_cutoff,
        observation_class=ann["observation_class"], announcement_id=announcement_id, issuer_cik=ann["issuer_cik"],
        expiry_sessions=settings().default_expiry_sessions,
        reason=(f"potentially material {ann['form_type'] or 'announcement'} ({ann['observation_class']}); "
                "screened for research, no price move required"),
    )
    return cid if new else None


# ---------------------------------------------------------------------- local JSON import
def load_local_json(path: str | Path) -> tuple[dict, list[Item]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "astra.announcements.v1":
        raise ValidationError("announcements file must have format 'astra.announcements.v1'")
    src = data.get("source") or {}
    if not src.get("name"):
        raise ValidationError("source.name is required")
    scope = f"local:{src['name']}"
    items = []
    for raw in data.get("items", []):
        ext = raw.get("external_id")
        if not ext:
            raise ValidationError(f"item without external_id: {raw.get('title')!r}")
        sym = (raw.get("symbol") or "").strip().upper() or None
        items.append(Item(
            source_key=str(ext), scope_key=scope, title=str(raw.get("title") or ""), content=raw, symbol=sym,
            issuer_cik=raw.get("issuer_cik"), issuer_name=raw.get("issuer_name"), form_type=raw.get("form_type"),
            sec_items=raw.get("items"), categories=list(raw.get("categories") or []), material_hint=raw.get("material_hint"),
            url=raw.get("url"), published_raw=raw.get("published_at"), published_tz=raw.get("published_tz"),
            published_time_kind=raw.get("time_kind") or ("SOURCE_STATED_PUBLICATION" if raw.get("published_at") else "UNKNOWN"),
            declared_retrieved_raw=data.get("retrieved_at"),
            identity_status="DECLARED_BY_SOURCE_UNVERIFIED" if sym else "NO_SYMBOL",
        ))
    return src, items


def import_local_json(conn: sqlite3.Connection, path: str | Path) -> dict[str, Any]:
    src, items = load_local_json(path)
    mode = db_mode(conn)
    is_sample = bool(src.get("is_sample"))
    if mode == "live" and is_sample:
        raise AstraError("refusing sample announcements in a live database")
    label = src.get("coverage_label") or "Retained local announcements; coverage limited to what the retaining process saved."
    run_id = start_run(conn, "announcement_import", mode, source_classification="SYNTHETIC" if is_sample else "OPERATOR_IMPORT",
                       declared_scope={"source": src["name"], "items": len(items)},
                       input_ref=f"{path} sha256={file_sha256(str(path))}")
    try:
        with tx(conn):
            sid = get_or_create_source(conn, "local_json", src["name"], label, is_sample)
            summary = ingest(conn, source_id=sid, run_id=run_id, items=items, scopes=[f"local:{src['name']}"],
                             mode=mode, is_sample=is_sample)
            finish_run(conn, run_id, "completed", summary=summary, coverage_status="SCANNED",
                       coverage={"scope": f"local:{src['name']}", "status": "retained file fully read"}, in_tx=True)
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, **summary}
