"""Structured research records (schema astra.research.v1) and their import.

Research is data about a candidate, never a command. Unknown fields are rejected (so
fields like target_price, expected_return or state cannot be smuggled in), returns
targets and probabilities are never required, and the only states research can produce
are RESEARCHED, BLOCKED and (manual/sample research only) REJECTED.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydValidationError, field_validator, model_validator

from . import candidates as cands
from . import notifications, work
from .core import (AstraError, ValidationError, canonical_json, content_hash, fmt_utc, new_id, now, now_str,
                   parse_duration, parse_source_time, parse_utc)
from .db import db_mode, tx
from .runs import finish_run, start_run

MECHANISMS = ("ECON", "PROB", "NAV", "FLOW", "SUPPLY", "TAPE")
REVISION_REASONS = ("NEW_EVENT", "PRICE_OR_CONDITION_CHANGE", "CORRECTED_INPUT", "NEWLY_FOUND_OLD_INFORMATION",
                    "CHANGED_INFERENCE")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(_Strict):
    ref: str
    title: str
    url: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    kind: Literal["primary", "secondary", "data", "other"] = "other"
    stance: Literal["supports", "contradicts", "context"] = "context"
    evidence_id: str | None = None  # an ASTRA evidence snapshot this source refers to


class Statement(_Strict):
    id: str
    text: str = Field(min_length=1)
    source_refs: list[str] = []


class Inference(_Strict):
    id: str
    text: str = Field(min_length=1)
    based_on: list[str] = Field(min_length=1)


class Unknown(_Strict):
    item: str = Field(min_length=1)
    blocks_conclusions: list[str] = []


class Assessment(_Strict):
    """A section that may be unknown. Unknown blocks only the conclusions listed."""
    status: Literal["assessed", "unknown", "not_applicable"] = "unknown"
    summary: str | None = None
    source_refs: list[str] = []
    blocks_conclusions: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if self.status == "assessed" and not self.summary:
            raise ValueError("an assessed section needs a summary")
        if self.status != "unknown" and self.blocks_conclusions:
            raise ValueError("only an unknown section can block conclusions")
        return self


class ValuationScale(Assessment):
    """Small equity value needs contemporaneous price x verified economic shares (ops r1.2)."""
    price: float | None = None
    price_time: str | None = None
    economic_shares: float | None = None
    shares_source_ref: str | None = None

    @model_validator(mode="after")
    def _need_inputs(self):
        if self.status == "assessed" and None in (self.price, self.price_time, self.economic_shares, self.shares_source_ref):
            raise ValueError("assessed valuation_scale requires price, price_time, economic_shares and shares_source_ref")
        return self


class Blocker(_Strict):
    key: str
    description: str = Field(min_length=1)
    blocks: list[str] = Field(min_length=1)
    clearing_evidence: str = Field(min_length=1)
    owner: str | None = None

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("blocker key must be a lowercase slug")
        return v


class ClearedBlocker(_Strict):
    key: str
    clearing_evidence: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class Schedule(_Strict):
    due_at: str | None = None
    due_in: str | None = None
    condition: str = Field(min_length=1)
    priority: Literal["urgent", "routine"] = "routine"

    @model_validator(mode="after")
    def _one(self):
        if (self.due_at is None) == (self.due_in is None):
            raise ValueError("give exactly one of due_at or due_in")
        return self


class WhatChanged(_Strict):
    summary: str = Field(min_length=1)
    when: str | None = None
    when_basis: Literal["source_publication", "source_public_availability", "first_observation", "unknown"] = "unknown"


class SubjectiveProbability(_Strict):
    event: str = Field(min_length=10)
    deadline_utc: str
    probability: float = Field(ge=0.0, le=1.0)
    label: Literal["UNCALIBRATED_JUDGMENT"]


class Researcher(_Strict):
    kind: Literal["manual", "ai", "sample"]
    name: str = Field(min_length=1)
    model: str | None = None


class Selector(_Strict):
    symbol: str
    route: Literal["announcement", "anomaly"]


class ResearchRecordV1(_Strict):
    schema_version: Literal["astra.research.v1"]
    candidate_id: str | None = None
    candidate_selector: Selector | None = None
    researcher: Researcher
    is_sample: bool
    retrospective: bool = False
    completed_at: str | None = None
    parent_research_id: str | None = None
    revision_reason_type: Literal[REVISION_REASONS] | None = None  # type: ignore[valid-type]
    what_changed: WhatChanged
    mechanism: list[Literal[MECHANISMS]] = Field(min_length=1)  # type: ignore[valid-type]
    facts: list[Statement] = []
    inferences: list[Inference] = []
    unknowns: list[Unknown] = []
    sources: list[Source] = []
    contrary_evidence: list[Statement] = []
    competing_explanations: list[str] = []
    economic_materiality: Assessment
    valuation_scale: ValuationScale = ValuationScale()
    liquidity: Assessment
    financing: Assessment
    dilution: Assessment
    execution: Assessment
    remaining_uncertainty: str = Field(min_length=1)
    blockers: list[Blocker] = []
    cleared_blockers: list[ClearedBlocker] = []
    recheck: Schedule | None = None
    expiry: Schedule | None = None
    proposed_disposition: Literal["researched", "blocked", "reject"]
    disposition_rationale: str = Field(min_length=1)
    subjective_probabilities: list[SubjectiveProbability] = []

    @model_validator(mode="after")
    def _consistency(self):
        refs = {s.ref for s in self.sources}
        used = [r for st in self.facts + self.contrary_evidence for r in st.source_refs]
        used += [r for sec in (self.economic_materiality, self.valuation_scale, self.liquidity, self.financing,
                               self.dilution, self.execution) for r in sec.source_refs]
        used += [r for cb in self.cleared_blockers for r in cb.source_refs]
        if self.valuation_scale.shares_source_ref:
            used.append(self.valuation_scale.shares_source_ref)
        missing = sorted(set(used) - refs)
        if missing:
            raise ValueError(f"source_refs not defined in sources: {missing}")
        if not self.facts and not self.unknowns:
            raise ValueError("record at least one fact or one unknown")
        if self.proposed_disposition == "blocked" and not self.blockers:
            raise ValueError("proposed_disposition 'blocked' requires at least one blocker")
        if self.proposed_disposition in ("researched", "reject") and self.blockers:
            raise ValueError("open blockers require proposed_disposition 'blocked'")
        if self.researcher.kind == "ai" and self.proposed_disposition == "reject":
            raise ValueError("AI research cannot reject a candidate; it may only report researched or blocked")
        if self.researcher.kind == "sample" and not self.is_sample:
            raise ValueError("sample research must set is_sample true")
        if ({"ECON", "NAV"} & set(self.mechanism)) and self.economic_materiality.status == "not_applicable":
            raise ValueError("economic_materiality cannot be not_applicable for ECON/NAV mechanisms")
        keys = [b.key for b in self.blockers]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate blocker keys")
        if set(keys) & {c.key for c in self.cleared_blockers}:
            raise ValueError("a blocker cannot be both open and cleared")
        if self.candidate_id is None and self.candidate_selector is None:
            raise ValueError("give candidate_id or candidate_selector")
        return self


FORBIDDEN_HINTS = ("target", "probability_of", "expected_return", "entry", "state", "position", "order", "stop")


def parse_record(data: dict) -> ResearchRecordV1:
    try:
        return ResearchRecordV1.model_validate(data)
    except PydValidationError as exc:
        extras = [e["loc"][-1] for e in exc.errors() if e["type"] == "extra_forbidden"]
        hint = ""
        if any(any(h in str(x).lower() for h in FORBIDDEN_HINTS) for x in extras):
            hint = (" (research records carry no return targets, trading states, orders or positions; subjective "
                    "probabilities go in subjective_probabilities with an exact event and deadline)")
        raise ValidationError(f"research record invalid: {exc.errors(include_url=False)}{hint}") from exc


def _resolve_candidate(conn: sqlite3.Connection, rec: ResearchRecordV1, override: str | None, mode: str) -> sqlite3.Row:
    cid = override or rec.candidate_id
    if cid:
        return cands.get(conn, cid)
    sel = rec.candidate_selector
    rows = conn.execute(
        "SELECT * FROM candidates WHERE symbol=? AND route=? AND mode=? AND state IN ('DETECTED','RESEARCHED','BLOCKED')",
        (sel.symbol.upper(), sel.route, mode),
    ).fetchall()
    if len(rows) != 1:
        raise AstraError(f"candidate_selector {sel.symbol}/{sel.route} matched {len(rows)} open candidates; pass --candidate")
    return rows[0]


def _schedule_at(s: Schedule, base: datetime) -> str:
    if s.due_in is not None:
        return fmt_utc(base + parse_duration(s.due_in))
    dt, _ = parse_source_time(s.due_at)
    return fmt_utc(dt)


def import_research(conn: sqlite3.Connection, data: dict, *, candidate_id: str | None = None, actor: str | None = None,
                    source_label: str = "inline") -> dict[str, Any]:
    rec = parse_record(data)
    mode = db_mode(conn)
    if mode == "live" and rec.is_sample:
        raise AstraError("refusing sample research in a live database")
    run_id = start_run(conn, "ai_research" if rec.researcher.kind == "ai" else "research_import", mode,
                       source_classification="OPERATOR_IMPORT" if rec.researcher.kind != "ai" else "INTERNAL",
                       input_ref=source_label)
    try:
        with tx(conn):
            cand = _resolve_candidate(conn, rec, candidate_id, mode)
            result = _apply(conn, cand, rec, data, run_id, actor or f"research:{rec.researcher.kind}:{rec.researcher.name}")
            finish_run(conn, run_id, "completed", summary=result, in_tx=True)
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, **result}


def _collect_urls(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.strip():
                out.add(v.strip())
            else:
                _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


def context_sources(conn: sqlite3.Connection, candidate_id: str) -> list[dict]:
    """Sources an AI researcher was given for this candidate: linked evidence snapshots and
    the sources of the latest prior research record. AI research may cite only these."""
    out: list[dict] = []
    for r in conn.execute(
        "SELECT e.evidence_id, e.kind, e.content_json FROM candidate_evidence ce JOIN evidence_snapshots e "
        "ON e.evidence_id = ce.evidence_id WHERE ce.candidate_id = ? ORDER BY ce.linked_at_utc", (candidate_id,)):
        urls: set[str] = set()
        _collect_urls(json.loads(r["content_json"]), urls)
        out.append({"evidence_id": r["evidence_id"], "kind": r["kind"], "urls": sorted(urls)})
    cand = cands.get(conn, candidate_id)
    if cand["latest_research_id"]:
        rec = json.loads(conn.execute("SELECT content_json FROM research_records WHERE research_id=?",
                                      (cand["latest_research_id"],)).fetchone()["content_json"])
        for src in rec.get("sources", []):
            if src.get("url") or src.get("evidence_id"):
                out.append({"research_id": cand["latest_research_id"], "title": src.get("title"),
                            "evidence_id": src.get("evidence_id"), "urls": [src["url"]] if src.get("url") else []})
    return out


def _check_ai_record(conn: sqlite3.Connection, candidate_id: str, rec: "ResearchRecordV1") -> None:
    """AI works only from saved context: it cannot bring new evidence, so it may cite only
    sources present in that context and may not clear blockers."""
    if rec.cleared_blockers:
        raise AstraError(
            "AI_BLOCKER_CLEARING_REFUSED: AI research cannot clear blockers because it does not retrieve new "
            "evidence; clear blockers with manual research citing the clearing evidence"
        )
    allowed = context_sources(conn, candidate_id)
    ids = {a["evidence_id"] for a in allowed if a.get("evidence_id")}
    urls = {u for a in allowed for u in a["urls"]}
    bad = [s.ref for s in rec.sources
           if not ((s.evidence_id and s.evidence_id in ids) or (s.url and s.url.strip() in urls))]
    if bad:
        raise AstraError(
            f"AI_SOURCE_NOT_IN_CONTEXT: sources {bad} are not evidence ids or URLs present in the candidate's "
            "saved context; AI research may not introduce sources"
        )


def _apply(conn: sqlite3.Connection, cand: sqlite3.Row, rec: ResearchRecordV1, raw: dict, run_id: str, actor: str) -> dict:
    cid = cand["candidate_id"]
    if cand["state"] not in cands.OPEN_STATES:
        raise AstraError(f"candidate {cid} is {cand['state']} (terminal); new evidence needs a new candidate")
    if rec.researcher.kind == "ai":
        _check_ai_record(conn, cid, rec)
    if cand["mode"] == "replay" and not rec.retrospective:
        raise AstraError("research on a replay candidate must set retrospective=true (hindsight risk)")
    imported = now()
    if rec.completed_at:
        completed_dt, _ = parse_source_time(rec.completed_at)
        basis = "DECLARED_BY_RESEARCHER"
        if completed_dt < parse_utc(cand["detected_at_utc"]):
            raise AstraError(
                f"research completed_at {fmt_utc(completed_dt)} precedes candidate detection {cand['detected_at_utc']}; "
                "research cannot predate detection"
            )
        if completed_dt > imported:
            raise AstraError("research completed_at is in the future")
    else:
        completed_dt, basis = imported, "IMPORT_TIME"
    stored = raw.copy()
    stored["candidate_id"] = cid
    chash = content_hash(stored)
    dup = conn.execute("SELECT research_id FROM research_records WHERE candidate_id=? AND content_hash=?", (cid, chash)).fetchone()
    if dup:
        return {"candidate_id": cid, "research_id": dup["research_id"], "duplicate": True, "state": cand["state"]}
    latest = cand["latest_research_id"]
    parent = rec.parent_research_id
    if parent == "latest":
        parent = latest
    if latest and parent != latest:
        raise AstraError(f"candidate already has research {latest}; set parent_research_id to it (or 'latest') and a revision_reason_type")
    if latest and not rec.revision_reason_type:
        raise AstraError(f"a research revision needs revision_reason_type, one of {REVISION_REASONS}")
    if not latest and parent:
        raise AstraError("parent_research_id given but the candidate has no prior research")
    # blockers: every open blocker must be retained or cleared with evidence
    open_now = {b["key"] for b in cands.open_blockers(conn, cid)}
    listed = {b.key for b in rec.blockers}
    cleared = {c.key for c in rec.cleared_blockers}
    unaccounted = open_now - listed - cleared
    if unaccounted:
        raise AstraError(f"open blockers {sorted(unaccounted)} are neither retained nor cleared with evidence")
    bogus = cleared - open_now
    if bogus:
        raise AstraError(f"cleared_blockers {sorted(bogus)} are not open blockers of this candidate")
    rid = new_id("res")
    ts = now_str()
    conn.execute(
        "INSERT INTO research_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, cid, parent, rec.revision_reason_type, rec.researcher.kind, rec.researcher.name, rec.researcher.model,
         1 if rec.is_sample else 0, 1 if rec.retrospective else 0, rec.completed_at, fmt_utc(completed_dt), basis, ts,
         canonical_json(stored), chash, run_id),
    )
    for c in rec.cleared_blockers:
        conn.execute(
            "UPDATE blockers SET status='cleared', cleared_by_research_id=?, cleared_at_utc=?, clearing_evidence_json=? "
            "WHERE candidate_id=? AND key=? AND status='open'",
            (rid, ts, canonical_json(c.model_dump()), cid, c.key),
        )
    new_blockers = []
    for b in rec.blockers:
        if b.key in open_now:
            continue
        conn.execute("INSERT INTO blockers(blocker_id,candidate_id,key,description,blocks_json,clearing_evidence_required,"
                     "owner,opened_by_research_id,opened_at_utc,status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
                     (new_id("blk"), cid, b.key, b.description, canonical_json(b.blocks), b.clearing_evidence,
                      b.owner or cand["owner"], rid, ts))
        new_blockers.append(b.key)
    still_open = cands.open_blockers(conn, cid)
    if still_open:
        target = "BLOCKED"
    elif rec.proposed_disposition == "reject":
        target = "REJECTED"
    else:
        target = "RESEARCHED"
    updates: dict[str, Any] = {"research_status": "COMPLETED", "latest_research_id": rid}
    work.cancel_pending(conn, cid, "research_request", f"fulfilled by research {rid}")
    if target not in cands.TERMINAL_STATES:
        if rec.recheck:
            due = _schedule_at(rec.recheck, imported)
            updates.update(next_recheck_at_utc=due, next_recheck_condition=rec.recheck.condition)
            work.cancel_pending(conn, cid, "recheck", f"superseded by research {rid}")
            work.schedule(conn, kind="recheck", candidate_id=cid, due_at=due, priority=rec.recheck.priority,
                          dedup_key=f"recheck:{rid}", condition=rec.recheck.condition)
        else:
            # removing a recheck removes its scheduled job too; a stale job must not fire later
            work.cancel_pending(conn, cid, "recheck", f"recheck removed by research {rid}")
            updates.update(next_recheck_at_utc=None,
                           next_recheck_condition="UNKNOWN: latest research set no recheck")
        if rec.expiry:
            exp = _schedule_at(rec.expiry, imported)
            updates.update(expires_at_utc=exp, expiry_condition=rec.expiry.condition)
            work.cancel_pending(conn, cid, "expiry", f"superseded by research {rid}")
            work.schedule(conn, kind="expiry", candidate_id=cid, due_at=exp, priority="urgent",
                          dedup_key=f"expiry:{cid}:{exp}", condition=rec.expiry.condition)
    evidence_delta = {"research_id": rid, "content_hash": chash, "new_blockers": new_blockers,
                      "cleared_blockers": [c.model_dump() for c in rec.cleared_blockers],
                      "researcher": rec.researcher.model_dump(), "is_sample": rec.is_sample}
    if target != cand["state"]:
        cands.transition(conn, cid, target, actor=actor, reason=rec.disposition_rationale,
                         reason_type=rec.revision_reason_type or "INITIAL_RESEARCH", evidence=evidence_delta,
                         run_id=run_id, extra_updates=updates)
    else:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE candidates SET {sets}, updated_at_utc = ?, version = version + 1 WHERE candidate_id = ?",
                     (*updates.values(), ts, cid))
        cands.add_event(conn, cid, "research_updated", actor, evidence_delta)
        notifications.queue_for_candidate(conn, cid, kind="research_updated", dedup_suffix=rid, priority="routine",
                                          note=f"research revision ({rec.revision_reason_type}) without state change")
    return {"candidate_id": cid, "research_id": rid, "duplicate": False, "state": target,
            "new_blockers": new_blockers, "cleared_blockers": sorted(cleared)}


def import_research_file(conn: sqlite3.Connection, path: str | Path, candidate_id: str | None = None) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return import_research(conn, data, candidate_id=candidate_id, source_label=str(path))


def unresolved(conn: sqlite3.Connection, candidate_id: str) -> dict:
    """Unknowns and open blockers from the latest research, for the dashboard."""
    cand = cands.get(conn, candidate_id)
    out: dict[str, Any] = {"open_blockers": cands.open_blockers(conn, candidate_id), "unknowns": [], "unknown_sections": []}
    if cand["latest_research_id"]:
        rec = json.loads(conn.execute("SELECT content_json FROM research_records WHERE research_id=?",
                                      (cand["latest_research_id"],)).fetchone()["content_json"])
        out["unknowns"] = rec.get("unknowns", [])
        for sec in ("economic_materiality", "valuation_scale", "liquidity", "financing", "dilution", "execution"):
            s = rec.get(sec) or {}
            if s.get("status", "unknown") == "unknown":
                out["unknown_sections"].append({"section": sec, "blocks_conclusions": s.get("blocks_conclusions", [])})
    return out
