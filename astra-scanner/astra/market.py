"""Market data import (documented CSV/JSON formats), current-state index and
point-in-time bar loading. See docs/IMPORT_FORMATS.md.

Nothing here fabricates a bar: missing, forming or conflicting data stays visible and
is excluded from calculations by the detectors.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from . import calendar as cal
from .core import (
    UTC,
    AstraError,
    ValidationError,
    canonical_json,
    content_hash,
    file_sha256,
    fmt_utc,
    now,
    now_str,
    parse_source_time,
    parse_utc,
)
from .db import db_mode, tx
from .runs import finish_run, start_run

PRICE_ADJUSTMENTS = {"unadjusted", "split_adjusted", "split_dividend_adjusted", "unknown"}
VOLUME_SCOPES = {"consolidated", "primary_venue", "unknown"}
VOLUME_UNITS = {"shares", "unknown"}
AVAILABILITY_BASES = {"ASSUMED_BAR_END_PLUS_LATENCY", "OBSERVED_RETRIEVAL"}
DATASET_REQUIRED = (
    "dataset_id", "interval_seconds", "price_adjustment", "volume_scope", "volume_unit", "venue",
    "session_timezone", "calendar_id", "currency", "bar_time_convention", "availability_basis",
    "assumed_latency_seconds",
)


# ---------------------------------------------------------------------- provider interface
class MarketProvider(Protocol):
    """Interface for a future live provider. It must return bars in the import batch format
    (see ``import_bar_rows``) with ``as_of`` set to the actual retrieval time, and declare
    ``availability_basis = OBSERVED_RETRIEVAL`` on its dataset. No live provider ships with
    this prototype."""

    provider_id: str

    def dataset_definition(self) -> dict: ...

    def fetch_bars(self, symbols: list[str], start_utc: datetime, end_utc: datetime) -> dict: ...


class ImportedFileProvider:
    """The only provider in the prototype: reads documented CSV/JSON batches from disk."""

    def __init__(self, dataset_file: str | Path):
        self.definition = json.loads(Path(dataset_file).read_text(encoding="utf-8"))
        self.provider_id = self.definition["provider"]["provider_id"]

    def dataset_definition(self) -> dict:
        return self.definition


# ---------------------------------------------------------------------- dataset / instruments
def register_dataset(conn: sqlite3.Connection, definition: dict) -> str:
    if definition.get("format") != "astra.market.dataset.v1":
        raise ValidationError("dataset file must have format 'astra.market.dataset.v1'")
    prov, ds = definition.get("provider") or {}, definition.get("dataset") or {}
    for key in ("provider_id", "name"):
        if not prov.get(key):
            raise ValidationError(f"provider.{key} is required")
    missing = [k for k in DATASET_REQUIRED if ds.get(k) in (None, "")]
    if missing:
        raise ValidationError(f"dataset fields missing: {missing}. Declare 'unknown' explicitly where unknown.")
    if ds["price_adjustment"] not in PRICE_ADJUSTMENTS:
        raise ValidationError(f"price_adjustment must be one of {sorted(PRICE_ADJUSTMENTS)}")
    if ds["volume_scope"] not in VOLUME_SCOPES:
        raise ValidationError(f"volume_scope must be one of {sorted(VOLUME_SCOPES)}")
    if ds["volume_unit"] not in VOLUME_UNITS:
        raise ValidationError(f"volume_unit must be one of {sorted(VOLUME_UNITS)}")
    if ds["availability_basis"] not in AVAILABILITY_BASES:
        raise ValidationError(f"availability_basis must be one of {sorted(AVAILABILITY_BASES)}")
    if ds["bar_time_convention"] != "start":
        raise ValidationError("only bar_time_convention='start' is supported")
    is_sample = 1 if prov.get("is_sample") else 0
    mode = db_mode(conn)
    if mode == "live" and is_sample:
        raise AstraError("refusing to register a sample provider in a live database")
    record = {"provider": prov, "dataset": ds}
    with tx(conn):
        existing = conn.execute("SELECT metadata_json FROM datasets WHERE dataset_id = ?", (ds["dataset_id"],)).fetchone()
        if existing:
            if json.loads(existing["metadata_json"]) != record:
                raise AstraError(
                    f"dataset {ds['dataset_id']} already registered with different properties; "
                    "declare a new dataset_id rather than mixing adjustment/volume scope/session"
                )
            return ds["dataset_id"]
        conn.execute(
            "INSERT OR IGNORE INTO providers(provider_id,name,is_sample,description,created_at_utc) VALUES (?,?,?,?,?)",
            (prov["provider_id"], prov["name"], is_sample, prov.get("description"), now_str()),
        )
        conn.execute(
            "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ds["dataset_id"], prov["provider_id"], int(ds["interval_seconds"]), ds["price_adjustment"],
                ds["volume_scope"], ds["volume_unit"], ds["venue"], ds["session_timezone"], ds["calendar_id"],
                ds["currency"], ds["bar_time_convention"], ds["availability_basis"],
                int(ds["assumed_latency_seconds"]), is_sample, canonical_json(record), now_str(),
            ),
        )
    return ds["dataset_id"]


def import_instruments(conn: sqlite3.Connection, path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "astra.instruments.v1":
        raise ValidationError("instruments file must have format 'astra.instruments.v1'")
    mode = db_mode(conn)
    run_id = start_run(conn, "instrument_import", mode, source_classification="OPERATOR_IMPORT",
                       input_ref=f"{path} sha256={file_sha256(str(path))}")
    changed = 0
    with tx(conn):
        for item in data.get("instruments", []):
            sym = (item.get("symbol") or "").strip().upper()
            if not sym:
                raise ValidationError("instrument without symbol")
            is_syn = 1 if item.get("is_synthetic", data.get("is_sample")) else 0
            if mode == "live" and is_syn:
                raise AstraError("refusing synthetic instruments in a live database")
            record = {k: item.get(k) for k in (
                "name", "exchange_mic", "currency", "security_type", "benchmark_symbol", "issuer_cik",
                "active_from", "active_to")}
            record["is_synthetic"] = bool(is_syn)
            h = content_hash(record)
            prev = conn.execute("SELECT metadata_json FROM instruments WHERE symbol = ?", (sym,)).fetchone()
            if prev and content_hash(json.loads(prev["metadata_json"])) == h:
                continue
            conn.execute(
                "INSERT INTO instruments(symbol,name,exchange_mic,currency,security_type,benchmark_symbol,issuer_cik,"
                "active_from,active_to,is_synthetic,metadata_json,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, exchange_mic=excluded.exchange_mic, "
                "currency=excluded.currency, security_type=excluded.security_type, benchmark_symbol=excluded.benchmark_symbol, "
                "issuer_cik=excluded.issuer_cik, active_from=excluded.active_from, active_to=excluded.active_to, "
                "is_synthetic=excluded.is_synthetic, metadata_json=excluded.metadata_json, updated_at_utc=excluded.updated_at_utc",
                (sym, record["name"], record["exchange_mic"], record["currency"], record["security_type"],
                 (record["benchmark_symbol"] or None) and record["benchmark_symbol"].upper(), record["issuer_cik"],
                 record["active_from"], record["active_to"], is_syn, canonical_json(record), now_str()),
            )
            conn.execute(
                "INSERT INTO instrument_history VALUES (?,?,?,?,?)",
                (sym, now_str(), run_id, canonical_json(record), h),
            )
            changed += 1
    finish_run(conn, run_id, "completed", summary={"instruments_changed": changed})
    return {"run_id": run_id, "instruments_changed": changed}


def import_universe(conn: sqlite3.Connection, path: str | Path) -> str:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "astra.universe.v1":
        raise ValidationError("universe file must have format 'astra.universe.v1'")
    uid = data.get("universe_id")
    members = sorted({m.strip().upper() for m in data.get("members", []) if m.strip()})
    if not uid or "@" not in uid:
        raise ValidationError("universe_id must be 'name@version' (membership is immutable per version)")
    if not members:
        raise ValidationError("universe has no members")
    h = content_hash(members)
    with tx(conn):
        prev = conn.execute("SELECT members_hash FROM universes WHERE universe_id = ?", (uid,)).fetchone()
        if prev:
            if prev["members_hash"] != h:
                raise AstraError(f"universe {uid} already declared with different members; bump the version")
            return uid
        conn.execute(
            "INSERT INTO universes VALUES (?,?,?,?,?,?,?)",
            (uid, data.get("name", uid), canonical_json(members), h, data.get("description"),
             1 if data.get("is_sample") else 0, now_str()),
        )
    return uid


def get_universe(conn: sqlite3.Connection, universe_id: str) -> list[str]:
    row = conn.execute("SELECT members_json FROM universes WHERE universe_id = ?", (universe_id,)).fetchone()
    if not row:
        raise AstraError(f"unknown universe {universe_id}")
    return json.loads(row["members_json"])


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if not row:
        raise AstraError(f"unknown dataset {dataset_id}")
    return row


def dataset_as_of(conn: sqlite3.Connection, dataset_id: str) -> str | None:
    row = conn.execute(
        "SELECT max(data_cutoff_utc) AS c FROM runs WHERE kind='bar_import' AND dataset_id=? AND status IN ('completed','partial')",
        (dataset_id,),
    ).fetchone()
    return row["c"] if row else None


# ---------------------------------------------------------------------- bar import
def _read_bar_rows(path: Path) -> tuple[list[dict], str | None, str | None]:
    """Returns (rows, as_of_raw, dataset_id) from CSV or JSON."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != "astra.market.bars.v1":
            raise ValidationError("bars JSON must have format 'astra.market.bars.v1'")
        return list(data.get("bars", [])), data.get("as_of"), data.get("dataset_id")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows, None, None


def _num(v: Any) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _truthy(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "complete"):
        return True
    if s in ("0", "false", "no", "n", "forming"):
        return False
    return None


def import_bars(conn: sqlite3.Connection, dataset_id: str, bars_path: str | Path, as_of: str | None = None) -> dict:
    """Import a batch of bars into ``dataset_id``. Incremental: only new or changed bars write rows."""
    path = Path(bars_path)
    ds = get_dataset(conn, dataset_id)
    rows, file_as_of, file_ds = _read_bar_rows(path)
    if file_ds and file_ds != dataset_id:
        raise ValidationError(f"bars file declares dataset {file_ds}, not {dataset_id}")
    sidecar = path.with_name(path.name + ".meta.json")
    if not (as_of or file_as_of) and sidecar.is_file():
        file_as_of = json.loads(sidecar.read_text(encoding="utf-8")).get("as_of")
    as_of_raw = as_of or file_as_of
    if not as_of_raw:
        raise ValidationError("batch as_of (provider snapshot time) is required; pass --as-of or add <file>.meta.json")
    as_of_dt, _ = parse_source_time(as_of_raw)
    if as_of_dt > now() + timedelta(seconds=5):
        raise ValidationError(f"batch as_of {fmt_utc(as_of_dt)} is in the future relative to the system clock")
    mode = db_mode(conn)
    if mode == "live" and ds["is_sample"]:
        raise AstraError("refusing sample dataset in a live database")
    classification = "SYNTHETIC" if ds["is_sample"] else ("OBSERVED_MARKET" if ds["availability_basis"] == "OBSERVED_RETRIEVAL" else "RETROSPECTIVE")
    run_id = start_run(conn, "bar_import", mode, source_classification=classification,
                       provider_id=ds["provider_id"], dataset_id=dataset_id,
                       data_cutoff_utc=fmt_utc(as_of_dt), input_ref=f"{path} sha256={file_sha256(str(path))}")
    interval = int(ds["interval_seconds"])
    latency = int(ds["assumed_latency_seconds"])
    ingest = now_str()
    counts = {"rows": len(rows), "inserted": 0, "revised": 0, "unchanged": 0, "data_conflict": 0,
              "forming": 0, "identical_duplicates": 0, "calendar_unavailable": 0}
    try:
        # Group rows by key to detect conflicting duplicates inside one batch.
        grouped: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            start_raw = r.get("start") or r.get("start_at_utc")
            if not sym or not start_raw:
                raise ValidationError(f"bar row missing symbol/start: {r}")
            start_dt, _ = parse_source_time(str(start_raw), ds["session_timezone"])
            grouped.setdefault((sym, fmt_utc(start_dt)), []).append(r)
        with tx(conn):
            for (sym, start_s), group in grouped.items():
                start_dt = parse_utc(start_s)
                first = group[0]
                reasons: list[str] = []
                if len(group) > 1:
                    sigs = {canonical_json({k: str(g.get(k)) for k in ("open", "high", "low", "close", "volume", "complete")}) for g in group}
                    if len(sigs) > 1:
                        reasons.append("conflicting_duplicate_rows_in_batch")
                    else:
                        counts["identical_duplicates"] += len(group) - 1
                o, h, l, c, v = (_num(first.get(k)) for k in ("open", "high", "low", "close", "volume"))
                complete = _truthy(first.get("complete", "true"))
                if complete is None:
                    reasons.append("unparseable_complete_flag")
                    complete = False
                end_dt = start_dt + timedelta(seconds=interval)
                try:
                    session_date, session_class, slot = cal.classify_bar(start_dt, interval)
                except cal.CalendarUnavailable:
                    counts["calendar_unavailable"] += 1
                    session_date, session_class, slot = cal.local_date(start_dt).isoformat(), "UNKNOWN_CALENDAR", start_dt.strftime("%H:%M")
                    reasons.append("calendar_unavailable")
                if None in (o, h, l, c, v):
                    reasons.append("missing_or_non_numeric_field")
                else:
                    if min(o, h, l, c) <= 0:
                        reasons.append("non_positive_price")
                    if v < 0:
                        reasons.append("negative_volume")
                    if h < max(o, c, l) or l > min(o, c, h):
                        reasons.append("high_low_inconsistent")
                if session_class == "RTH":
                    s = cal.session(cal.local_date(start_dt))
                    if s and int((start_dt - s.open_utc).total_seconds()) % interval != 0:
                        reasons.append("misaligned_to_session_grid")
                if complete and end_dt > as_of_dt:
                    reasons.append("complete_bar_ends_after_batch_as_of")
                # Availability: never before the bar's end.
                if first.get("available_at"):
                    avail_dt, _ = parse_source_time(str(first["available_at"]), ds["session_timezone"])
                    basis = "DECLARED_BY_SOURCE"
                    if complete and avail_dt < end_dt:
                        reasons.append("available_before_bar_end")
                elif not complete:
                    avail_dt, basis = as_of_dt, "FORMING_OBSERVED_AT_AS_OF"
                elif ds["availability_basis"] == "OBSERVED_RETRIEVAL":
                    avail_dt, basis = max(end_dt, as_of_dt), "OBSERVED_RETRIEVAL"
                else:
                    avail_dt = max(end_dt, min(end_dt + timedelta(seconds=latency), as_of_dt))
                    basis = "ASSUMED_BAR_END_PLUS_LATENCY"
                quality = "DATA_CONFLICT" if reasons else "OK"
                if quality == "DATA_CONFLICT":
                    counts["data_conflict"] += 1
                if not complete:
                    counts["forming"] += 1
                payload = {"open": o, "high": h, "low": l, "close": c, "volume": v,
                           "complete": bool(complete), "quality": quality, "reasons": sorted(set(reasons))}
                chash = content_hash(payload)
                cur = conn.execute(
                    "SELECT content_hash, revision_no FROM bars WHERE dataset_id=? AND symbol=? AND start_utc=?",
                    (dataset_id, sym, start_s),
                ).fetchone()
                if cur and cur["content_hash"] == chash:
                    counts["unchanged"] += 1
                    continue
                rev = (cur["revision_no"] + 1) if cur else 1
                vals = (dataset_id, sym, start_s, fmt_utc(end_dt), session_date, session_class, slot,
                        o, h, l, c, v, 1 if complete else 0, fmt_utc(avail_dt), basis, quality,
                        ",".join(payload["reasons"]) or None)
                conn.execute(
                    "INSERT INTO bar_revisions(dataset_id,symbol,start_utc,end_utc,session_date,session_class,clock_slot,"
                    "open,high,low,close,volume,source_complete,available_at_utc,availability_basis,quality,quality_reason,"
                    "revision_no,content_hash,ingested_at_utc,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*vals, rev, chash, ingest, run_id),
                )
                conn.execute(
                    "INSERT INTO bars(dataset_id,symbol,start_utc,end_utc,session_date,session_class,clock_slot,"
                    "open,high,low,close,volume,source_complete,available_at_utc,availability_basis,quality,quality_reason,"
                    "revision_no,content_hash,first_ingested_at_utc,last_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(dataset_id,symbol,start_utc) DO UPDATE SET open=excluded.open, high=excluded.high, "
                    "low=excluded.low, close=excluded.close, volume=excluded.volume, source_complete=excluded.source_complete, "
                    "available_at_utc=excluded.available_at_utc, availability_basis=excluded.availability_basis, "
                    "quality=excluded.quality, quality_reason=excluded.quality_reason, revision_no=excluded.revision_no, "
                    "content_hash=excluded.content_hash, last_run_id=excluded.last_run_id",
                    (*vals, rev, chash, ingest, run_id),
                )
                counts["revised" if cur else "inserted"] += 1
            finish_run(conn, run_id, "completed", summary=counts, in_tx=True)
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}", summary=counts)
        raise
    return {"run_id": run_id, "as_of": fmt_utc(as_of_dt), **counts}


# ---------------------------------------------------------------------- point-in-time loading
@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    session_date: str
    session_class: str
    clock_slot: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    complete: bool
    available_at: datetime
    quality: str
    quality_reason: str | None
    revision_no: int
    content_hash: str

    def as_evidence(self) -> dict:
        return {
            "start_utc": fmt_utc(self.start), "end_utc": fmt_utc(self.end), "session_date": self.session_date,
            "session_class": self.session_class, "clock_slot": self.clock_slot, "open": self.open,
            "high": self.high, "low": self.low, "close": self.close, "volume": self.volume,
            "available_at_utc": fmt_utc(self.available_at), "revision_no": self.revision_no,
            "content_hash": self.content_hash,
        }


def _row_to_bar(r: sqlite3.Row) -> Bar:
    return Bar(
        symbol=r["symbol"], start=parse_utc(r["start_utc"]), end=parse_utc(r["end_utc"]),
        session_date=r["session_date"], session_class=r["session_class"], clock_slot=r["clock_slot"],
        open=r["open"], high=r["high"], low=r["low"], close=r["close"], volume=r["volume"],
        complete=bool(r["source_complete"]), available_at=parse_utc(r["available_at_utc"]),
        quality=r["quality"], quality_reason=r["quality_reason"], revision_no=r["revision_no"],
        content_hash=r["content_hash"],
    )


class PointInTimeStore:
    """All bar revisions for a symbol set, loaded once, viewable at any cutoff.

    ``as_of(cutoff)`` returns, per symbol, the latest revision of each bar whose
    availability time is at or before the cutoff. Used by live/sample scans and replay.
    """

    def __init__(self, conn: sqlite3.Connection, dataset_id: str, symbols: Iterable[str], since: datetime,
                 until: datetime):
        self.symbols = sorted({s.upper() for s in symbols})
        self.revs: dict[str, list[Bar]] = {s: [] for s in self.symbols}
        if not self.symbols:
            return
        marks = ",".join("?" for _ in self.symbols)
        rows = conn.execute(
            f"SELECT * FROM bar_revisions WHERE dataset_id=? AND symbol IN ({marks}) AND start_utc >= ? "
            "AND available_at_utc <= ? ORDER BY symbol, start_utc, revision_no",
            (dataset_id, *self.symbols, fmt_utc(since), fmt_utc(until)),
        ).fetchall()
        for r in rows:
            self.revs[r["symbol"]].append(_row_to_bar(r))

    def as_of(self, cutoff: datetime) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for sym, revs in self.revs.items():
            latest: dict[datetime, Bar] = {}
            for b in revs:
                if b.available_at <= cutoff:
                    latest[b.start] = b  # revisions are ordered, so the last available one wins
            out[sym] = [latest[k] for k in sorted(latest)]
        return out


def usable(bar: Bar, cutoff: datetime) -> bool:
    """Completed-bar requirement: source-complete, quality OK, ended and available by cutoff."""
    return bar.complete and bar.quality == "OK" and bar.end <= cutoff and bar.available_at <= cutoff


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
