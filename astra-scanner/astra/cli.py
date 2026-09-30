"""ASTRA command-line interface. Run `astra --help` or `python -m astra --help`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import PROJECT_ROOT, load_env_file, settings
from .core import AstraError, parse_source_time


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _db(args: argparse.Namespace):
    from .db import open_db

    return open_db(args.db)


def cmd_init(a):
    from .db import init_db

    conn = init_db(a.db, a.mode)
    _out({"db": str(a.db), "mode": a.mode, "status": "ready"})
    conn.close()


def cmd_status(a):
    from .status import overview, text_report

    ov = overview(_db(a))
    if a.json:
        _out(ov)
    else:
        print(text_report(ov))


def cmd_dataset(a):
    from .market import register_dataset

    _out({"dataset_id": register_dataset(_db(a), json.loads(Path(a.file).read_text(encoding="utf-8")))})


def cmd_instruments(a):
    from .market import import_instruments

    _out(import_instruments(_db(a), a.file))


def cmd_universe(a):
    from .market import import_universe

    _out({"universe_id": import_universe(_db(a), a.file)})


def cmd_bars(a):
    from .market import import_bars

    _out(import_bars(_db(a), a.dataset, a.file, as_of=a.as_of))


def cmd_announcements(a):
    from .announcements import import_local_json

    _out(import_local_json(_db(a), a.file))


def cmd_sec(a):
    from .sec import collect

    _out(collect(_db(a), a.issuers, settings()))


def cmd_scan(a):
    from .scan import scan

    cutoff = parse_source_time(a.cutoff)[0] if a.cutoff else None
    _out(scan(_db(a), a.dataset, a.universe, cutoff))


def cmd_replay(a):
    from datetime import date

    from .replay import run_replay

    _out(run_replay(_db(a), a.dataset, a.universe, date.fromisoformat(a.start), date.fromisoformat(a.end), a.every_minutes))


def cmd_research_import(a):
    from .research import import_research_file

    _out(import_research_file(_db(a), a.file, candidate_id=a.candidate))


def cmd_research_ai(a):
    from .ai import investigate

    _out(investigate(_db(a), a.candidate))


def cmd_candidates(a):
    conn = _db(a)
    q = "SELECT candidate_id, mode, route, symbol, state, owner, research_status, next_recheck_at_utc, expires_at_utc FROM candidates"
    rows = [dict(r) for r in conn.execute(q + (" WHERE state=?" if a.state else "") + " ORDER BY created_at_utc",
                                          (a.state,) if a.state else ())]
    _out(rows)


def cmd_candidate_show(a):
    from .research import unresolved

    conn = _db(a)
    c = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (a.candidate,)).fetchone()
    if not c:
        raise AstraError(f"unknown candidate {a.candidate}")
    _out({"candidate": dict(c),
          "transitions": [dict(r) for r in conn.execute("SELECT * FROM state_transitions WHERE candidate_id=? ORDER BY at_utc, rowid", (a.candidate,))],
          "unresolved": unresolved(conn, a.candidate)})


def cmd_transition(a):
    from . import candidates as cands
    from .db import tx

    evidence = json.loads(a.evidence_json)
    conn = _db(a)
    with tx(conn):
        tid = cands.transition(conn, a.candidate, a.state, actor=f"operator:{a.actor}", reason=a.reason,
                               evidence=evidence, reason_type=a.reason_type or "OPERATOR_DECISION")
    _out({"transition_id": tid, "candidate_id": a.candidate, "to_state": a.state})


def cmd_assign(a):
    from . import candidates as cands
    from .db import tx

    conn = _db(a)
    with tx(conn):
        cands.assign_owner(conn, a.candidate, a.owner, actor=f"operator:{a.actor}")
    _out({"candidate_id": a.candidate, "owner": a.owner})


def cmd_promote(a):
    from .announcements import create_announcement_candidate
    from .db import db_mode, tx
    from .runs import finish_run, start_run

    conn = _db(a)
    mode = db_mode(conn)
    ann = conn.execute("SELECT * FROM announcements WHERE announcement_id=?", (a.announcement,)).fetchone()
    if not ann:
        raise AstraError(f"unknown announcement {a.announcement}")
    run_id = start_run(conn, "research_import", mode, source_classification="OPERATOR_IMPORT",
                       declared_scope={"promote_announcement": a.announcement, "reason": a.reason, "actor": a.actor})
    with tx(conn):
        src = conn.execute("SELECT * FROM sources WHERE source_id=?", (ann["source_id"],)).fetchone()
        cid = create_announcement_candidate(conn, a.announcement, run_id, mode, bool(ann["is_sample"]), src, None, None, None)
        finish_run(conn, run_id, "completed", summary={"candidate_id": cid, "reason": a.reason}, in_tx=True)
    _out({"candidate_id": cid, "note": None if cid else "a candidate already exists for this announcement"})


def cmd_process_due(a):
    from .processing import process_due

    _out(process_due(_db(a), worker=a.worker, limit=a.limit, with_ai=a.with_ai))


def cmd_cycle(a):
    from .processing import process_due

    conn = _db(a)

    def collect() -> dict:
        out: dict[str, Any] = {}
        if a.sec_issuers:
            from .sec import collect as sec_collect

            try:
                out["sec"] = sec_collect(conn, a.sec_issuers, settings())
            except AstraError as exc:
                out["sec"] = {"failed": str(exc)}
        if a.dataset and a.universe:
            from .scan import scan

            try:
                out["scan"] = scan(conn, a.dataset, a.universe)
            except AstraError as exc:
                out["scan"] = {"failed": str(exc)}
        return out

    _out(process_due(conn, worker=a.worker, limit=a.limit, with_ai=a.with_ai, collect=collect))


def cmd_outcomes(a):
    from .outcomes import measure_all
    from .runs import finish_run, start_run
    from .db import db_mode

    conn = _db(a)
    run_id = start_run(conn, "outcome_measure", db_mode(conn), source_classification="INTERNAL")
    res = measure_all(conn, run_id)
    finish_run(conn, run_id, "completed", summary=res)
    _out({"run_id": run_id, **res})


def cmd_notify(a):
    from . import notifications as n

    conn = _db(a)
    if a.action == "list":
        rows = conn.execute("SELECT * FROM notifications WHERE status = ? OR ? = 'all' ORDER BY priority='urgent' DESC, created_at_utc",
                            (a.status, a.status)).fetchall()
        for r in rows:
            print(n.render_text(r) + f"\n  status: {r['status']}\n")
        print(f"{len(rows)} notification(s)")
    elif a.action == "deliver":
        _out(n.deliver(conn, channel=a.channel))
    elif a.action == "ack":
        if not a.id:
            raise AstraError("notify ack needs --id")
        n.acknowledge(conn, a.id, by=a.by)
        _out({"acknowledged": a.id, "by": a.by})


def cmd_alerts(a):
    from .exports import import_external_alerts

    uni = [s.strip().upper() for s in a.universe.split(",")] if a.universe else None
    _out(import_external_alerts(_db(a), a.file, source_name=a.source_name, coverage_start=a.coverage_start,
                                coverage_end=a.coverage_end, universe=uni, is_sample=a.sample, notes=a.notes))


def cmd_export(a):
    from . import exports as ex

    conn = _db(a)
    fn = {"candidates": ex.export_candidates, "detector-results": ex.export_detector_results,
          "outcomes": ex.export_outcomes, "runs": ex.export_runs, "comparison": ex.export_comparison}[a.what]
    _out({"export": a.what, "path": a.out, "result": fn(conn, a.out)})


def cmd_serve(a):
    import uvicorn

    from .web.app import create_app

    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.allow_remote:
        raise AstraError("refusing to bind a non-localhost address without --allow-remote (the dashboard has no authentication)")
    print(f"ASTRA dashboard (read-only) on http://{a.host}:{a.port}  db={a.db}")
    uvicorn.run(create_app(str(a.db)), host=a.host, port=a.port, log_level="warning")


def cmd_demo(a):
    from .demo import run_demo

    run_demo(Path(a.db), reset=a.reset)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="astra", description="ASTRA local US-equities research scanner (research only; no orders).")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=None, help="SQLite path (default $ASTRA_DB or var/astra.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create/migrate a database in a fixed mode")
    s.add_argument("--mode", required=True, choices=["sample", "replay", "live"])
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("status", help="operational summary")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("dataset", help="register a provider/dataset definition (JSON)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_dataset)
    s = sub.add_parser("instruments", help="import instrument metadata (JSON)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_instruments)
    s = sub.add_parser("universe", help="declare an immutable universe version (JSON)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_universe)
    s = sub.add_parser("bars", help="import a CSV/JSON bar batch into a dataset")
    s.add_argument("file")
    s.add_argument("--dataset", required=True)
    s.add_argument("--as-of", default=None, help="batch snapshot time (else <file>.meta.json or JSON header)")
    s.set_defaults(fn=cmd_bars)
    s = sub.add_parser("announcements", help="import retained announcements (JSON)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_announcements)
    s = sub.add_parser("sec-collect", help="bounded SEC EDGAR submissions collection (live/replay DB)")
    s.add_argument("--issuers", required=True)
    s.set_defaults(fn=cmd_sec)
    s = sub.add_parser("scan", help="run the anomaly detectors once")
    s.add_argument("--dataset", required=True)
    s.add_argument("--universe", required=True)
    s.add_argument("--cutoff", default=None, help="sample/replay only; live uses the current clock")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("replay", help="historical replay at simulated cutoffs (replay DB)")
    s.add_argument("--dataset", required=True)
    s.add_argument("--universe", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--every-minutes", type=int, default=30)
    s.set_defaults(fn=cmd_replay)
    s = sub.add_parser("research-import", help="import a structured research record (JSON)")
    s.add_argument("file")
    s.add_argument("--candidate", default=None)
    s.set_defaults(fn=cmd_research_import)
    s = sub.add_parser("research-ai", help="one gated AI research call (disabled unless explicitly enabled)")
    s.add_argument("--candidate", required=True)
    s.set_defaults(fn=cmd_research_ai)
    s = sub.add_parser("candidates", help="list candidates")
    s.add_argument("--state", default=None)
    s.set_defaults(fn=cmd_candidates)
    s = sub.add_parser("candidate", help="show one candidate")
    s.add_argument("candidate")
    s.set_defaults(fn=cmd_candidate_show)
    s = sub.add_parser("transition", help="operator state transition (reason and evidence required)")
    s.add_argument("candidate")
    s.add_argument("state")
    s.add_argument("--reason", required=True)
    s.add_argument("--evidence-json", required=True, help='e.g. \'{"note":"...","source":"..."}\'')
    s.add_argument("--reason-type", default=None)
    s.add_argument("--actor", default="operator")
    s.set_defaults(fn=cmd_transition)
    s = sub.add_parser("assign", help="set candidate owner")
    s.add_argument("candidate")
    s.add_argument("owner")
    s.add_argument("--actor", default="operator")
    s.set_defaults(fn=cmd_assign)
    s = sub.add_parser("promote-announcement", help="manually create a candidate from a retained announcement")
    s.add_argument("announcement")
    s.add_argument("--reason", required=True)
    s.add_argument("--actor", default="operator")
    s.set_defaults(fn=cmd_promote)
    s = sub.add_parser("process-due", help="process due rechecks/expiries, measure outcomes, report backlog")
    s.add_argument("--worker", default=None)
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--with-ai", action="store_true", help="also run the gated AI adapter on the research backlog")
    s.set_defaults(fn=cmd_process_due)
    s = sub.add_parser("cycle", help="urgent work -> discovery sweeps -> routine work -> outcomes")
    s.add_argument("--dataset", default=None)
    s.add_argument("--universe", default=None)
    s.add_argument("--sec-issuers", default=None)
    s.add_argument("--worker", default=None)
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--with-ai", action="store_true")
    s.set_defaults(fn=cmd_cycle)
    s = sub.add_parser("outcomes", help="measure forward outcomes now")
    s.set_defaults(fn=cmd_outcomes)
    s = sub.add_parser("notify", help="notification preview queue")
    s.add_argument("action", choices=["list", "deliver", "ack"])
    s.add_argument("--status", default="queued", help="list filter: queued|attempted|acknowledged|all")
    s.add_argument("--channel", default="local_outbox", help="local_outbox (only configured channel) or external")
    s.add_argument("--id", default=None)
    s.add_argument("--by", default="operator")
    s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("import-hourly-alerts", help="retain hourly ChatGPT alert rows for comparison")
    s.add_argument("file")
    s.add_argument("--source-name", required=True)
    s.add_argument("--coverage-start", required=True)
    s.add_argument("--coverage-end", required=True)
    s.add_argument("--universe", default=None, help="comma-separated symbols the alerts covered (optional)")
    s.add_argument("--notes", default=None)
    s.add_argument("--sample", action="store_true")
    s.set_defaults(fn=cmd_alerts)
    s = sub.add_parser("export", help="CSV exports")
    s.add_argument("what", choices=["candidates", "detector-results", "outcomes", "runs", "comparison"])
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("serve", help="read-only dashboard (localhost)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-remote", action="store_true")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("demo", help="run the complete SAMPLE workflow into a fresh database")
    s.add_argument("--reset", action="store_true", help="delete an existing demo database first")
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(".env")
    args = build_parser().parse_args(argv)
    if args.db is None:
        args.db = str(PROJECT_ROOT / "var" / "demo.db") if args.cmd == "demo" else settings().db_path
    try:
        args.fn(args)
    except AstraError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
