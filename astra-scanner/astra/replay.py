"""Historical replay at simulated cutoffs (replay-mode databases only).

Point-in-time rules:
- Bars: only revisions whose availability time is at or before the simulated cutoff.
  First-imported historical bars use the dataset's ASSUMED availability (bar end + declared
  latency, capped at the batch as_of); later corrections are available only from their own
  batch as_of. Replay cannot prove what a provider actually showed at the time.
- Announcements: a flagged item becomes a replay candidate once the cutoff passes its source
  publication time (ASSUMED availability = publication). Actual collection times are kept
  separately and are never replaced by simulated times.
- Instrument metadata is the current record (not point-in-time); recorded as a limitation.
- Every candidate stores its simulated cutoff; created/detected timestamps remain actual
  software times. Replay establishes neither historical live detection nor delivery.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from . import calendar as cal
from .announcements import create_announcement_candidate
from .core import AstraError, fmt_utc
from .db import db_mode, tx
from .market import PointInTimeStore, get_dataset, get_universe
from .runs import finish_run, start_run
from .scan import LOOKBACK_DAYS, scan

LIMITATIONS = [
    "assumed bar availability (bar end + declared latency) for imported history",
    "announcement availability assumed at source publication time",
    "instrument metadata is current, not point-in-time",
    "adjusted price datasets embed later corporate actions (hindsight) if price_adjustment is not 'unadjusted'",
    "replay cannot establish historical live detection, delivery or executable prices",
]


def cutoffs_for(start: date, end: date, every_minutes: int, latency_seconds: int) -> list[datetime]:
    if every_minutes % 5 != 0 or every_minutes <= 0:
        raise AstraError("every_minutes must be a positive multiple of 5")
    out = []
    for d in cal.sessions_between(start, end):
        s = cal.session(d)
        t = s.open_utc + timedelta(minutes=every_minutes)
        while t <= s.close_utc:
            out.append(t + timedelta(seconds=latency_seconds))
            t += timedelta(minutes=every_minutes)
        if out and out[-1] != s.close_utc + timedelta(seconds=latency_seconds):
            out.append(s.close_utc + timedelta(seconds=latency_seconds))
    return out


def run_replay(conn: sqlite3.Connection, dataset_id: str, universe_id: str, start: date, end: date,
               every_minutes: int = 30) -> dict[str, Any]:
    if db_mode(conn) != "replay":
        raise AstraError("replay requires a database initialised with --mode replay")
    ds = get_dataset(conn, dataset_id)
    members = get_universe(conn, universe_id)
    if ds["price_adjustment"] != "unadjusted":
        limitation = f"dataset price_adjustment={ds['price_adjustment']}: adjusted history embeds later corporate actions"
    else:
        limitation = None
    cuts = cutoffs_for(start, end, every_minutes, int(ds["assumed_latency_seconds"]))
    if not cuts:
        raise AstraError("no US sessions in the requested range")
    benchmarks = {r["benchmark_symbol"] for r in conn.execute(
        f"SELECT benchmark_symbol FROM instruments WHERE symbol IN ({','.join('?' for _ in members)})", members)
        if r["benchmark_symbol"]}
    store = PointInTimeStore(conn, dataset_id, [*members, *benchmarks], since=cuts[0] - timedelta(days=LOOKBACK_DAYS),
                             until=cuts[-1])
    parent = start_run(conn, "replay", "replay", source_classification="RETROSPECTIVE", dataset_id=dataset_id,
                       provider_id=ds["provider_id"], declared_scope={"universe_id": universe_id, "members": members,
                                                                      "start": start.isoformat(), "end": end.isoformat(),
                                                                      "every_minutes": every_minutes},
                       cohort_eligibility="EXCLUDED_RETROSPECTIVE")
    runs, signals, ann_cands = [], 0, []
    src_rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM sources")}
    try:
        for cut in cuts:
            with tx(conn):
                for a in conn.execute(
                    "SELECT a.* FROM announcements a WHERE a.screen_result='POTENTIALLY_MATERIAL' AND a.published_at_utc IS NOT NULL "
                    "AND a.published_at_utc <= ? AND a.published_at_utc >= ? AND NOT EXISTS (SELECT 1 FROM candidates c "
                    "WHERE c.announcement_id = a.announcement_id AND c.mode='replay')",
                    (fmt_utc(cut), fmt_utc(cal.session(cal.sessions_between(start, end)[0]).open_utc - timedelta(hours=18))),
                ).fetchall():
                    cid = create_announcement_candidate(conn, a["announcement_id"], parent, "replay", bool(a["is_sample"]),
                                                        src_rows[a["source_id"]], None, None, simulated_cutoff=fmt_utc(cut))
                    if cid:
                        ann_cands.append(cid)
            res = scan(conn, dataset_id, universe_id, cut, store=store, run_kind="replay")
            runs.append({"run_id": res["run_id"], "cutoff_utc": fmt_utc(cut), "coverage": res["coverage_status"],
                         "signals": len(res["signals"])})
            signals += len(res["signals"])
    except Exception as exc:
        finish_run(conn, parent, "failed", error=f"{type(exc).__name__}: {exc}", summary={"runs": runs})
        raise
    summary = {"cutoffs": len(cuts), "signals": signals, "announcement_candidates": ann_cands,
               "limitations": LIMITATIONS + ([limitation] if limitation else []), "child_runs": runs}
    finish_run(conn, parent, "completed", summary=summary, requested_cutoff_utc=fmt_utc(cuts[-1]))
    return {"run_id": parent, **{k: v for k, v in summary.items() if k != "child_runs"}, "child_runs": len(runs)}
