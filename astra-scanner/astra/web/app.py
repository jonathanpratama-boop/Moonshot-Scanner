"""Read-only local dashboard (FastAPI + server-rendered HTML).

Binds to 127.0.0.1 by default (see cli.serve). It never writes to the database; all
source text is autoescaped, so fetched content cannot inject markup or scripts.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from .. import __version__
from .. import research as research_mod
from ..config import settings
from ..core import to_zone_str
from ..db import connect
from ..outcomes import SPEC, current_observations
from ..status import open_candidates, overview

TEMPLATES = Path(__file__).parent / "templates"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True,
                      finalize=lambda v: "" if v is None else v)
    zones = settings().display_timezones

    def tz(utc_s: str | None) -> str:
        if not utc_s:
            return "UNKNOWN"
        return " | ".join([f"{utc_s}"] + [to_zone_str(utc_s, z) for z in zones])

    def pretty(v: Any) -> str:
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                return v
        return json.dumps(v, indent=2, ensure_ascii=False, default=str)

    env.filters["tz"] = tz
    env.filters["pretty"] = pretty
    env.globals["version"] = __version__
    return env


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="ASTRA scanner (local, read-only)", docs_url=None, redoc_url=None, openapi_url=None)
    env = _env()

    def conn() -> sqlite3.Connection:
        if not Path(db_path).exists():
            raise HTTPException(503, f"database {db_path} not found; run `astra init` or `astra demo`")
        c = connect(db_path)
        return c

    def render(name: str, db: sqlite3.Connection, **ctx: Any) -> HTMLResponse:
        ov = overview(db)
        html = env.get_template(name).render(ov=ov, **ctx)
        return HTMLResponse(html)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        c = conn()
        try:
            return render("overview.html", c, candidates=open_candidates(c)[:25])
        finally:
            c.close()

    @app.get("/candidates", response_class=HTMLResponse)
    def candidates() -> HTMLResponse:
        c = conn()
        try:
            rows = open_candidates(c)
            for r in rows:
                r["unresolved"] = research_mod.unresolved(c, r["candidate_id"])
            return render("candidates.html", c, candidates=rows)
        finally:
            c.close()

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    def candidate(candidate_id: str) -> HTMLResponse:
        c = conn()
        try:
            cand = c.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if not cand:
                raise HTTPException(404, "unknown candidate")
            ctx = {
                "c": dict(cand),
                "transitions": [dict(r) for r in c.execute("SELECT * FROM state_transitions WHERE candidate_id=? ORDER BY at_utc, rowid", (candidate_id,))],
                "events": [dict(r) for r in c.execute("SELECT * FROM candidate_events WHERE candidate_id=? ORDER BY at_utc, rowid", (candidate_id,))],
                "evidence": [dict(r) for r in c.execute(
                    "SELECT ce.role, ce.linked_at_utc, e.* FROM candidate_evidence ce JOIN evidence_snapshots e ON e.evidence_id=ce.evidence_id "
                    "WHERE ce.candidate_id=? ORDER BY ce.linked_at_utc", (candidate_id,))],
                "readbacks": {r["evidence_id"]: dict(r) for r in c.execute("SELECT * FROM evidence_readbacks")},
                "research": [dict(r) for r in c.execute("SELECT * FROM research_records WHERE candidate_id=? ORDER BY imported_at_utc, rowid", (candidate_id,))],
                "blockers": [dict(r) for r in c.execute("SELECT * FROM blockers WHERE candidate_id=? ORDER BY opened_at_utc", (candidate_id,))],
                "unresolved": research_mod.unresolved(c, candidate_id),
                "work": [dict(r) for r in c.execute("SELECT * FROM work_items WHERE candidate_id=? ORDER BY created_at_utc", (candidate_id,))],
                "notifications": [dict(r) for r in c.execute("SELECT * FROM notifications WHERE candidate_id=? ORDER BY created_at_utc", (candidate_id,))],
                "outcomes": [o for o in current_observations(c) if o["candidate_id"] == candidate_id],
                "announcement": dict(a) if (a := c.execute("SELECT * FROM announcements WHERE announcement_id=?", (cand["announcement_id"],)).fetchone()) else None,
                "revisions": [dict(r) for r in c.execute("SELECT * FROM announcement_revisions WHERE announcement_id=? ORDER BY revision_no", (cand["announcement_id"],))],
            }
            return render("candidate.html", c, **ctx)
        finally:
            c.close()

    @app.get("/runs", response_class=HTMLResponse)
    def runs() -> HTMLResponse:
        c = conn()
        try:
            return render("runs.html", c, runs=[dict(r) for r in c.execute("SELECT * FROM runs ORDER BY started_at_utc DESC, rowid DESC LIMIT 300")])
        finally:
            c.close()

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run(run_id: str) -> HTMLResponse:
        c = conn()
        try:
            r = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                raise HTTPException(404, "unknown run")
            results = [dict(x) for x in c.execute("SELECT * FROM detector_results WHERE run_id=? ORDER BY symbol, detector", (run_id,))]
            return render("run.html", c, r=dict(r), results=results)
        finally:
            c.close()

    @app.get("/announcements", response_class=HTMLResponse)
    def announcements() -> HTMLResponse:
        c = conn()
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT a.*, s.name AS source_name, s.coverage_label, (SELECT max(revision_no) FROM announcement_revisions r "
                "WHERE r.announcement_id=a.announcement_id) AS revisions, (SELECT count(*) FROM announcement_sightings g "
                "WHERE g.announcement_id=a.announcement_id) AS sightings FROM announcements a JOIN sources s ON s.source_id=a.source_id "
                "ORDER BY a.first_observed_at_utc DESC, a.rowid DESC LIMIT 500")]
            return render("announcements.html", c, rows=rows)
        finally:
            c.close()

    @app.get("/outcomes", response_class=HTMLResponse)
    def outcomes() -> HTMLResponse:
        c = conn()
        try:
            return render("outcomes.html", c, rows=current_observations(c), spec=SPEC)
        finally:
            c.close()

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications() -> HTMLResponse:
        c = conn()
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM notifications ORDER BY created_at_utc DESC, rowid DESC")]
            attempts = {}
            for a in c.execute("SELECT * FROM notification_attempts ORDER BY attempted_at_utc"):
                attempts.setdefault(a["notification_id"], []).append(dict(a))
            return render("notifications.html", c, rows=rows, attempts=attempts)
        finally:
            c.close()

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        c = conn()
        try:
            return JSONResponse(json.loads(json.dumps(overview(c), default=str)))
        finally:
            c.close()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "read_only": True}

    return app
