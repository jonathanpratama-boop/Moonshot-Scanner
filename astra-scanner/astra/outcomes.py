"""Forward price-outcome measurement (exploratory path labels, not trading returns).

The definition below is frozen into ``outcome_definitions`` on first use; if the code's
spec ever changes under the same id, measurement refuses to run (bump the id instead).

Status rules for each (subject, horizon):
  PENDING_DATA  endpoint session close is after the dataset's current data cutoff
  MEASURED      endpoint bar exists, complete, quality OK            (final, never rewritten)
  UNKNOWN       endpoint is due and later bars exist, but the endpoint bar is missing,
                forming or DATA_CONFLICT; or the reference is unavailable
  CENSORED      endpoint is due but the symbol has no usable bar after the endpoint session
                (coverage ended / delisted / not collected)
UNKNOWN and CENSORED may later be superseded by an appended observation (e.g. backfill).

ASTRA-OUTCOME-EQ-2 (2026-09-30) supersedes EQ-1 after review: every reference records the
time it represents, must be fresh at that time, and takes instrument and benchmark prices
from the bar revisions available at that time. EQ-1 subjects in older databases are kept
as recorded and are no longer evaluated.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import calendar as cal
from .core import AstraError, canonical_json, content_hash, fmt_utc, new_id, now_str, parse_utc
from .db import tx
from .market import dataset_as_of

DEFINITION_ID = "ASTRA-OUTCOME-EQ-2"
SUPERSEDED_DEFINITIONS = {"ASTRA-OUTCOME-EQ-1": "reference time not recorded; references could be stale or use later corrections"}
MAX_REFERENCE_AGE_SECONDS = 900  # frozen v1 maximum_bar_age_seconds
SPEC = {
    "definition_id": DEFINITION_ID,
    "purpose": "Exploratory forward path labels for discovery evaluation. Not detection, delivered, executable or fill prices; not trading returns.",
    "dataset_requirements": {"interval_seconds": 300, "session_class": "RTH", "calendar_id": cal.CALENDAR_ID,
                             "reference_and_endpoint_from_same_dataset": True},
    "reference_roles": {
        "SIGNAL_BAR_REFERENCE": "anomaly route and sampled non-signals: close of the latest usable RTH bar at the scan cutoff (reference time = scan cutoff)",
        "PRE_EVENT_REFERENCE": "announcement route: close of the latest usable RTH bar ending at or before the source publication time (reference time = publication); measures event repricing only",
        "OBSERVATION_BAR_REFERENCE": "announcement route: close of the latest usable RTH bar ASTRA had ingested and that was available at its first actual observation (reference time = observation, or the simulated cutoff in replay)",
    },
    "point_in_time_references": "instrument and benchmark reference prices come from the latest bar revision available at the reference time; later corrections never alter a frozen reference",
    "reference_freshness": "the reference bar must end no more than 900 s before the latest RTH bar end expected at the reference time (in-session: the last completed 5-minute slot; outside RTH: the last session close); otherwise the reference is UNAVAILABLE",
    "endpoint_after_reference_time": "an endpoint whose session close is not after the reference time is UNKNOWN, never MEASURED",
    "endpoint_values": "latest bar revision known when measured; a MEASURED observation is final",
    "scoring_baseline_eligible": False,
    "horizons_sessions": [1, 3, 5],
    "endpoint": "close of the final regular-session 5-minute bar (15:55 ET slot, or 12:55 ET on early-close days) of the N-th US session after the reference bar's session; may differ from the official closing auction price",
    "return": "endpoint_close / reference_close - 1",
    "benchmark": "instrument benchmark_symbol in the same dataset at identical reference and endpoint bar timestamps; UNKNOWN if either is unusable",
    "extended_hours": "excluded; overnight and pre/post-market moves appear only through the next RTH close",
    "holidays": "sessions from calendar XNYS-RTH-RULES-2019-2027-v1; outside its range -> UNKNOWN(calendar_unavailable)",
    "missing_data": "never substituted by a neighbouring bar; see status rules",
    "excursions": "NOT_MEASURED: MFE/MAE and target-before-stop ordering are not derived from bar highs/lows",
    "statuses": ["MEASURED", "PENDING_DATA", "UNKNOWN", "CENSORED"],
}


def ensure_definition(conn: sqlite3.Connection) -> str:
    h = content_hash(SPEC)
    row = conn.execute("SELECT spec_hash FROM outcome_definitions WHERE definition_id=?", (DEFINITION_ID,)).fetchone()
    if row:
        if row["spec_hash"] != h:
            raise AstraError(f"{DEFINITION_ID} spec changed after freezing; create a new definition id")
        return DEFINITION_ID
    conn.execute("INSERT INTO outcome_definitions VALUES (?,?,?,?)", (DEFINITION_ID, canonical_json(SPEC), h, now_str()))
    return DEFINITION_ID


def _usable_bar(conn: sqlite3.Connection, dataset_id: str, symbol: str, start_utc: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bars WHERE dataset_id=? AND symbol=? AND start_utc=? AND source_complete=1 AND quality='OK' AND session_class='RTH'",
        (dataset_id, symbol, start_utc),
    ).fetchone()


@dataclass(frozen=True)
class RefBar:
    start_utc: str
    end_utc: str
    price: float
    session_date: str
    revision_no: int

    @classmethod
    def of(cls, bar: Any) -> "RefBar | None":
        """From a market.Bar or a bar_revisions row."""
        if bar is None:
            return None
        if hasattr(bar, "start"):
            return cls(fmt_utc(bar.start), fmt_utc(bar.end), bar.close, bar.session_date, bar.revision_no)
        return cls(bar["start_utc"], bar["end_utc"], bar["close"], bar["session_date"], bar["revision_no"])


def expected_last_bar_end(reference_time: datetime) -> datetime:
    """Latest regular-session 5-minute bar end that could exist at the reference time."""
    d = cal.local_date(reference_time)
    s = cal.session(d)
    if s is not None and s.open_utc < reference_time:
        if reference_time >= s.close_utc:
            return s.close_utc
        slots = int((reference_time - s.open_utc).total_seconds() // 300)
        return s.open_utc + timedelta(seconds=300 * slots)
    return cal.session(cal.previous_session(d)).close_utc


def reference_problem(reference_time: str, ref: RefBar | None) -> str | None:
    if ref is None:
        return "no usable RTH bar available at the reference time in this dataset"
    try:
        expected = expected_last_bar_end(parse_utc(reference_time))
    except cal.CalendarUnavailable as exc:
        return f"calendar_unavailable: {exc}"
    if parse_utc(ref.end_utc) > parse_utc(reference_time):
        return f"reference bar ends {ref.end_utc}, after the reference time {reference_time}"
    if (expected - parse_utc(ref.end_utc)).total_seconds() > MAX_REFERENCE_AGE_SECONDS:
        return (f"stale reference: latest usable bar ends {ref.end_utc}, but a bar ending {fmt_utc(expected)} was "
                f"expected at reference time {reference_time}")
    return None


def create_subject(conn: sqlite3.Connection, *, subject_type: str, candidate_id: str | None, run_id: str, symbol: str,
                   dataset_id: str, mode: str, reference_role: str, reference_time: str, ref: RefBar | None,
                   bench: RefBar | None, benchmark_symbol: str | None, reason: str | None = None) -> str | None:
    """Freeze what will be measured (caller holds the transaction). Idempotent.

    ``ref`` and ``bench`` must be the bar revisions available at ``reference_time``.
    """
    ensure_definition(conn)
    problem = reason or reference_problem(reference_time, ref)
    usable = problem is None
    bench_ok = usable and bench is not None and bench.start_utc == ref.start_utc
    sid = new_id("osub")
    cur = conn.execute(
        "INSERT OR IGNORE INTO outcome_subjects(subject_id,subject_type,candidate_id,run_id,symbol,dataset_id,definition_id,"
        "mode,reference_role,reference_session_date,reference_bar_start_utc,reference_price,reference_status,"
        "reference_reason,benchmark_symbol,benchmark_reference_price,created_at_utc,reference_time_utc,"
        "reference_revision_no,benchmark_reference_revision_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, subject_type, candidate_id, run_id, symbol, dataset_id, DEFINITION_ID, mode, reference_role,
         ref.session_date if usable else None, ref.start_utc if ref else None, ref.price if usable else None,
         "AVAILABLE" if usable else "UNAVAILABLE", problem, benchmark_symbol,
         bench.price if bench_ok else None, now_str(), reference_time,
         ref.revision_no if ref else None, bench.revision_no if bench_ok else None),
    )
    return sid if cur.rowcount else None


def bar_known_at(conn: sqlite3.Connection, dataset_id: str, symbol: str, at: str, require_ingested: bool,
                 start_utc: str | None = None) -> sqlite3.Row | None:
    """Point-in-time: the latest usable RTH bar (or the bar at ``start_utc``) as known at ``at``.

    Uses the append-only revision table: the bar must have ended and its revision must be
    available by ``at``; for actual observations it must also have been ingested by then.
    Replay cutoffs skip the ingestion condition because history is ingested afterwards.
    """
    ing = "AND r2.ingested_at_utc <= :at" if require_ingested else ""
    only = "AND r.start_utc = :start" if start_utc else ""
    return conn.execute(
        f"""
        SELECT r.* FROM bar_revisions r
        WHERE r.dataset_id = :ds AND r.symbol = :sym AND r.session_class = 'RTH' AND r.end_utc <= :at {only}
          AND r.revision_no = (SELECT max(r2.revision_no) FROM bar_revisions r2 WHERE r2.dataset_id = r.dataset_id
                               AND r2.symbol = r.symbol AND r2.start_utc = r.start_utc AND r2.available_at_utc <= :at {ing})
          AND r.source_complete = 1 AND r.quality = 'OK'
        ORDER BY r.start_utc DESC LIMIT 1
        """,
        {"ds": dataset_id, "sym": symbol, "at": at, "start": start_utc},
    ).fetchone()


def register_announcement_subjects(conn: sqlite3.Connection, dataset_id: str, run_id: str) -> int:
    """Create PRE_EVENT and OBSERVATION references for announcement candidates in this dataset.

    References that need no ingestion condition (publication-time references, replay cutoffs)
    are created only once the dataset has been delivered past the reference time, so a
    subject is never frozen as unavailable merely because data had not arrived yet.
    """
    created = 0
    as_of = dataset_as_of(conn, dataset_id)
    cands = conn.execute(
        "SELECT c.*, a.published_at_utc, a.first_observed_at_utc FROM candidates c JOIN announcements a "
        "ON a.announcement_id = c.announcement_id WHERE c.route='announcement' AND c.symbol IS NOT NULL"
    ).fetchall()
    for c in cands:
        if not conn.execute("SELECT 1 FROM bars WHERE dataset_id=? AND symbol=? LIMIT 1", (dataset_id, c["symbol"])).fetchone():
            continue  # symbol not covered by this dataset; no subject rather than a fabricated reference
        instr = conn.execute("SELECT benchmark_symbol FROM instruments WHERE symbol=?", (c["symbol"],)).fetchone()
        bench_sym = instr["benchmark_symbol"] if instr else None
        if c["observation_class"] == "MATERIAL_REVISION":
            pre_why = "revision publication time unknown (the source dates only the original item)"
        else:
            pre_why = None if c["published_at_utc"] else "publication time unknown"
        refs: list[tuple[str, str | None, bool, str | None]] = [
            ("PRE_EVENT_REFERENCE", c["published_at_utc"], False, pre_why),
            ("OBSERVATION_BAR_REFERENCE", c["simulated_cutoff_utc"] or c["first_observed_at_utc"], c["mode"] != "replay", None),
        ]
        for role, ref_time, require_ingested, why in refs:
            if why is None and not require_ingested and (as_of is None or as_of < ref_time):
                continue  # wait until data covering the reference time has been delivered
            ref_row = bar_known_at(conn, dataset_id, c["symbol"], ref_time, require_ingested) if ref_time else None
            bench_row = (bar_known_at(conn, dataset_id, bench_sym, ref_time, require_ingested, start_utc=ref_row["start_utc"])
                         if (ref_row is not None and bench_sym) else None)
            sid = create_subject(
                conn, subject_type="candidate", candidate_id=c["candidate_id"], run_id=c["origin_run_id"], symbol=c["symbol"],
                dataset_id=dataset_id, mode=c["mode"], reference_role=role, reference_time=ref_time or c["detected_at_utc"],
                ref=RefBar.of(ref_row), bench=RefBar.of(bench_row), benchmark_symbol=bench_sym, reason=why,
            )
            created += 1 if sid else 0
    return created


def _latest_obs(conn: sqlite3.Connection, subject_id: str, horizon: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outcome_observations WHERE subject_id=? AND horizon_sessions=? ORDER BY observed_at_utc DESC, rowid DESC LIMIT 1",
        (subject_id, horizon),
    ).fetchone()


def evaluate(conn: sqlite3.Connection, subj: sqlite3.Row, horizon: int, data_cutoff: str | None) -> dict:
    base = {"endpoint_session_date": None, "endpoint_bar_start_utc": None, "endpoint_price": None, "return_value": None,
            "benchmark_return": None, "excess_return": None, "benchmark_status": None, "endpoint_revision_no": None}
    if subj["reference_status"] != "AVAILABLE":
        return {**base, "status": "UNKNOWN", "reason": f"reference_unavailable: {subj['reference_reason']}"}
    if not data_cutoff:
        return {**base, "status": "PENDING_DATA", "reason": "dataset has no completed import"}
    try:
        end_day = cal.add_sessions(date.fromisoformat(subj["reference_session_date"]), horizon)
        s = cal.session(end_day)
    except cal.CalendarUnavailable as exc:
        return {**base, "status": "UNKNOWN", "reason": f"calendar_unavailable: {exc}"}
    ep_start = fmt_utc(s.close_utc - timedelta(seconds=300))
    base.update(endpoint_session_date=end_day.isoformat(), endpoint_bar_start_utc=ep_start)
    if subj["reference_time_utc"] and fmt_utc(s.close_utc) <= subj["reference_time_utc"]:
        return {**base, "status": "UNKNOWN",
                "reason": f"endpoint_not_after_reference_time: endpoint close {fmt_utc(s.close_utc)} <= reference time {subj['reference_time_utc']}"}
    if fmt_utc(s.close_utc) > data_cutoff:
        return {**base, "status": "PENDING_DATA", "reason": f"endpoint close {fmt_utc(s.close_utc)} after data cutoff {data_cutoff}"}
    bar = _usable_bar(conn, subj["dataset_id"], subj["symbol"], ep_start)
    if bar is None:
        later = conn.execute(
            "SELECT 1 FROM bars WHERE dataset_id=? AND symbol=? AND session_class='RTH' AND source_complete=1 AND quality='OK' "
            "AND session_date > ? LIMIT 1", (subj["dataset_id"], subj["symbol"], end_day.isoformat()),
        ).fetchone()
        raw = conn.execute("SELECT quality, source_complete FROM bars WHERE dataset_id=? AND symbol=? AND start_utc=?",
                           (subj["dataset_id"], subj["symbol"], ep_start)).fetchone()
        detail = ("endpoint_bar_data_conflict" if raw and raw["quality"] != "OK"
                  else "endpoint_bar_forming" if raw else "endpoint_bar_missing")
        if later:
            return {**base, "status": "UNKNOWN", "reason": detail}
        return {**base, "status": "CENSORED", "reason": f"{detail}; no usable bars for symbol after endpoint session (coverage ended)"}
    ret = bar["close"] / subj["reference_price"] - 1.0
    out = {**base, "status": "MEASURED", "reason": None, "endpoint_price": bar["close"], "return_value": ret,
           "endpoint_revision_no": bar["revision_no"]}
    if subj["benchmark_symbol"] and subj["benchmark_reference_price"]:
        bb = _usable_bar(conn, subj["dataset_id"], subj["benchmark_symbol"], ep_start)
        if bb:
            bret = bb["close"] / subj["benchmark_reference_price"] - 1.0
            out.update(benchmark_return=bret, excess_return=ret - bret, benchmark_status="MEASURED")
        else:
            out["benchmark_status"] = "UNKNOWN: benchmark endpoint bar unusable"
    else:
        out["benchmark_status"] = "UNKNOWN: benchmark reference unavailable or not declared"
    return out


def measure_all(conn: sqlite3.Connection, run_id: str) -> dict:
    """Append an observation wherever the status/reason changed. MEASURED rows are final."""
    counts = {"subjects": 0, "appended": 0, "MEASURED": 0, "PENDING_DATA": 0, "UNKNOWN": 0, "CENSORED": 0}
    with tx(conn):
        ensure_definition(conn)
        for ds in conn.execute("SELECT dataset_id FROM datasets").fetchall():
            register_announcement_subjects(conn, ds["dataset_id"], run_id)
        cutoffs: dict[str, str | None] = {}
        for subj in conn.execute("SELECT * FROM outcome_subjects WHERE definition_id = ?", (DEFINITION_ID,)).fetchall():
            counts["subjects"] += 1
            cutoff = cutoffs.setdefault(subj["dataset_id"], dataset_as_of(conn, subj["dataset_id"]))
            for h in SPEC["horizons_sessions"]:
                prev = _latest_obs(conn, subj["subject_id"], h)
                if prev is not None and prev["status"] == "MEASURED":
                    counts["MEASURED"] += 1
                    continue
                res = evaluate(conn, subj, h, cutoff)
                counts[res["status"]] += 1
                if prev is not None and prev["status"] == res["status"] and prev["reason"] == res["reason"]:
                    continue
                conn.execute(
                    "INSERT INTO outcome_observations(observation_id,subject_id,horizon_sessions,endpoint_session_date,status,"
                    "reason,endpoint_bar_start_utc,endpoint_price,return_value,benchmark_return,excess_return,benchmark_status,"
                    "data_cutoff_utc,observed_at_utc,run_id,endpoint_revision_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("oobs"), subj["subject_id"], h, res["endpoint_session_date"], res["status"], res["reason"],
                     res["endpoint_bar_start_utc"], res["endpoint_price"], res["return_value"], res["benchmark_return"],
                     res["excess_return"], res["benchmark_status"], cutoff, now_str(), run_id, res["endpoint_revision_no"]),
                )
                counts["appended"] += 1
    return counts


def current_observations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.*, o.horizon_sessions, o.status, o.reason, o.endpoint_session_date, o.endpoint_price, o.return_value,
               o.benchmark_return, o.excess_return, o.benchmark_status, o.observed_at_utc, o.data_cutoff_utc
        FROM outcome_subjects s
        JOIN outcome_observations o ON o.subject_id = s.subject_id
        WHERE o.rowid = (SELECT o2.rowid FROM outcome_observations o2 WHERE o2.subject_id = o.subject_id
                         AND o2.horizon_sessions = o.horizon_sessions ORDER BY o2.observed_at_utc DESC, o2.rowid DESC LIMIT 1)
          AND s.definition_id = ?
        ORDER BY s.created_at_utc, s.symbol, s.reference_role, o.horizon_sessions
        """, (DEFINITION_ID,)
    ).fetchall()
    return [dict(r) for r in rows]
