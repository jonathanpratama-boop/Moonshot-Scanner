"""Experimental anomaly detectors for US equities (research signals only).

COMPRESSION and IGNITION reuse the frozen EXPERIMENTAL-DETECTORS-1 parameters
(trading/pilot/detectors-v1.json, verified by SHA-256). EQ_GAP is a new detector in
ASTRA-EQ-EXPERIMENTAL-1. All are UNVALIDATED and grant no entry permission.

Every detector returns exactly one of SIGNAL / NO_SIGNAL / INSUFFICIENT_DATA /
UNAVAILABLE / FAILED. Missing, forming or conflicting bars never become NO_SIGNAL.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from importlib import resources
from typing import Any

from . import ENGINE_VERSION
from . import calendar as cal
from .core import UTC, AstraError, content_hash, fmt_utc, get_zone, sha256_text
from .market import Bar, usable

V1_SHA256 = "09297ed3303ebd5f0801d756cb6c46bc4966044b7ad2b51a30e648eaa8a21ddd"
INTERVAL = 300
MAX_BASELINE_LOOKBACK_SESSIONS = 40
DETECTORS = ("COMPRESSION", "IGNITION", "EQ_GAP")


class FrozenConfigError(AstraError):
    pass


def _read_config(name: str) -> str:
    return (resources.files("astra") / "config" / name).read_text(encoding="utf-8")


def load_v1() -> tuple[dict, str]:
    text = _read_config("detectors-v1.json")
    digest = sha256_text(text)
    if digest != V1_SHA256:
        raise FrozenConfigError(
            f"detectors-v1.json hash {digest} differs from frozen {V1_SHA256}; v1 detectors refuse to run. "
            "Changed rules require a new version."
        )
    return json.loads(text), digest


def load_eq() -> tuple[dict, str]:
    text = _read_config("astra-eq-experimental-1.json")
    return json.loads(text), sha256_text(text)


def config_records() -> list[dict]:
    """Detector config rows to freeze in detector_configs."""
    v1, v1h = load_v1()
    eq, eqh = load_eq()
    shared = {k: v1[k] for k in ("bar_interval_seconds", "maximum_bar_age_seconds", "volume_baseline")}
    recs = [
        {"detector": "COMPRESSION", "rule_version": v1["rule_version"],
         "params": {"compression": v1["compression"], **shared}, "source_ref": f"trading/pilot/detectors-v1.json sha256={v1h}"},
        {"detector": "IGNITION", "rule_version": v1["rule_version"],
         "params": {"ignition": v1["ignition"], **shared}, "source_ref": f"trading/pilot/detectors-v1.json sha256={v1h}"},
        {"detector": "EQ_GAP", "rule_version": eq["rule_version"],
         "params": {"eq_gap": eq["eq_gap"], "maximum_bar_age_seconds": v1["maximum_bar_age_seconds"]},
         "source_ref": f"astra/config/astra-eq-experimental-1.json sha256={eqh}"},
    ]
    for r in recs:
        r["engine_version"] = ENGINE_VERSION
        r["config_id"] = f"{r['detector']}@{r['rule_version']}+{ENGINE_VERSION}"
        r["params_hash"] = content_hash(r["params"])
    return recs


# ---------------------------------------------------------------------- views
@dataclass
class SeriesView:
    symbol: str
    usable: list[Bar]                         # usable RTH bars, ascending
    by_start: dict[datetime, Bar]             # usable RTH bars
    unusable_by_start: dict[datetime, Bar]    # forming / DATA_CONFLICT / not yet available


def make_view(symbol: str, bars: list[Bar], cutoff: datetime) -> SeriesView:
    ok, bad = [], {}
    for b in bars:
        if b.session_class != "RTH":
            continue
        if usable(b, cutoff):
            ok.append(b)
        else:
            bad[b.start] = b
    ok.sort(key=lambda b: b.start)
    return SeriesView(symbol, ok, {b.start: b for b in ok}, bad)


@dataclass
class DetectorResult:
    detector: str
    status: str
    reason: str | None = None
    features: dict = field(default_factory=dict)
    signal_bar_end: datetime | None = None
    feature_available_at: datetime | None = None
    inputs: dict | None = None


def _insufficient(det: str, reason: str, **features: Any) -> DetectorResult:
    return DetectorResult(det, "INSUFFICIENT_DATA", reason, features)


def _unavailable(det: str, reason: str, **features: Any) -> DetectorResult:
    return DetectorResult(det, "UNAVAILABLE", reason, features)


# ---------------------------------------------------------------------- grid helpers
def _prev_grid_start(start: datetime) -> datetime:
    d = cal.local_date(start)
    s = cal.session(d)
    if s is None:
        raise cal.CalendarUnavailable(f"bar start {fmt_utc(start)} is not inside a session")
    if start > s.open_utc:
        return start - timedelta(seconds=INTERVAL)
    prev = cal.session(cal.previous_session(d))
    return prev.close_utc - timedelta(seconds=INTERVAL)


def grid_back(latest_start: datetime, n: int, allow_session_break: bool) -> list[datetime] | None:
    """The n expected bar starts ending at latest_start (oldest first), per the calendar."""
    starts = [latest_start]
    while len(starts) < n:
        p = _prev_grid_start(starts[-1])
        if not allow_session_break and cal.local_date(p) != cal.local_date(latest_start):
            return None
        starts.append(p)
    return list(reversed(starts))


def _missing_reason(view: SeriesView, missing: list[datetime], label: str) -> str:
    kinds = set()
    for s in missing:
        b = view.unusable_by_start.get(s)
        if b is None:
            kinds.add("missing")
        elif b.quality == "DATA_CONFLICT":
            kinds.add("data_conflict")
        elif not b.complete:
            kinds.add("forming")
        else:
            kinds.add("not_yet_available")
    return f"{label}_bars_unusable:" + "+".join(sorted(kinds))


def _slot_start(d: date, slot: str) -> datetime | None:
    s = cal.session(d)
    if s is None:
        return None
    hh, mm = (int(x) for x in slot.split(":"))
    start = datetime.combine(d, time(hh, mm), tzinfo=get_zone(cal.EXCHANGE_TZ)).astimezone(UTC)
    if start < s.open_utc or start + timedelta(seconds=INTERVAL) > s.close_utc:
        return None
    return start


def matched_baseline(view: SeriesView, before_session: date, slots: list[str], min_n: int, max_n: int) -> dict:
    """Median of summed volume over the same exact RTH clock slots in prior sessions."""
    windows, skipped = [], []
    d = before_session
    for _ in range(MAX_BASELINE_LOOKBACK_SESSIONS):
        try:
            d = cal.previous_session(d)
        except cal.CalendarUnavailable:
            break
        starts = [_slot_start(d, sl) for sl in slots]
        bars = [view.by_start.get(s) if s else None for s in starts]
        if any(b is None or b.volume is None for b in bars):
            skipped.append(d.isoformat())
            continue
        windows.append({
            "session_date": d.isoformat(),
            "volume": sum(b.volume for b in bars),
            "bar_hashes": [b.content_hash for b in bars],
            "available_at_utc": fmt_utc(max(b.available_at for b in bars)),
        })
        if len(windows) >= max_n:
            break
    out = {"windows": windows, "skipped_sessions": skipped, "n": len(windows), "median": None}
    if len(windows) >= min_n:
        out["median"] = statistics.median(w["volume"] for w in windows)
    return out


def _fresh(view: SeriesView, cutoff: datetime, max_age: int, det: str) -> DetectorResult | None:
    if not view.usable:
        return _insufficient(det, "no_usable_rth_bars")
    latest = view.usable[-1]
    age = (cutoff - latest.end).total_seconds()
    if age > max_age:
        ctx = cal.session_context(cutoff)
        return _insufficient(
            det, "stale_latest_bar" + ("_market_not_in_rth" if ctx != "RTH" else ""),
            latest_bar_end_utc=fmt_utc(latest.end), age_seconds=int(age), max_age_seconds=max_age,
            cutoff_session_context=ctx,
        )
    return None


def _ret(first_open: float, last_close: float) -> float:
    return last_close / first_open - 1.0


# ---------------------------------------------------------------------- COMPRESSION
def compression(view: SeriesView, bench: SeriesView | None, cutoff: datetime, params: dict) -> DetectorResult:
    det, p = "COMPRESSION", params["compression"]
    stale = _fresh(view, cutoff, params["maximum_bar_age_seconds"], det)
    if stale:
        return stale
    n = p["lookback_bars"]
    latest = view.usable[-1]
    starts = grid_back(latest.start, n, allow_session_break=True)
    missing = [s for s in starts if s not in view.by_start]
    if missing:
        return _insufficient(det, _missing_reason(view, missing, "window"), missing_count=len(missing),
                             first_missing_utc=fmt_utc(missing[0]))
    if bench is None:
        return _unavailable(det, "benchmark_not_declared")
    bmissing = [s for s in starts if s not in bench.by_start]
    if bmissing:
        return _insufficient(det, _missing_reason(bench, bmissing, "benchmark"), missing_count=len(bmissing))
    bars = [view.by_start[s] for s in starts]
    bbars = [bench.by_start[s] for s in starts]
    prior = bars[:-1]
    ceiling = max(b.high for b in prior)
    tol = p["ceiling_touch_tolerance_fraction"]
    touches = [i for i, b in enumerate(prior) if b.high >= ceiling * (1 - tol)]
    sep = p["touch_separation_bars"]
    separated = any(j - i >= sep for i in touches for j in touches if j > i)
    touches_ok = len(touches) >= p["minimum_separated_ceiling_touches"] and separated
    left, right = p["pivot_left_bars"], p["pivot_right_bars"]
    pivots = []
    for k in range(left, len(bars) - right):
        neighbours = [bars[k + o].low for o in range(-left, right + 1) if o != 0]
        if all(bars[k].low < x for x in neighbours):
            pivots.append({"index": k, "low": bars[k].low,
                           "confirmed_at_utc": fmt_utc(max(bars[k + o].available_at for o in range(0, right + 1)))})
    rising_ok = False
    rise = None
    if len(pivots) >= p["minimum_confirmed_rising_lows"]:
        a, b = pivots[-2]["low"], pivots[-1]["low"]
        rise = b / a - 1.0
        rising_ok = rise >= p["minimum_rise_between_last_two_lows_fraction"]
    half = n // 2
    r1 = max(b.high for b in bars[:half]) - min(b.low for b in bars[:half])
    r2 = max(b.high for b in bars[half:]) - min(b.low for b in bars[half:])
    if r1 <= 0:
        return _insufficient(det, "zero_first_half_range")
    range_ratio = r2 / r1
    range_ok = range_ratio <= p["maximum_second_half_range_to_first_half_range"]
    close = bars[-1].close
    dist = close / ceiling - 1.0
    close_ok = -p["maximum_distance_below_ceiling_fraction"] <= dist <= p["maximum_distance_above_ceiling_fraction"]
    ret = _ret(bars[0].open, close)
    bret = _ret(bbars[0].open, bbars[-1].close)
    rel_pp = (ret - bret) * 100.0
    rel_ok = rel_pp >= p["minimum_relative_return_percentage_points"]
    checks = {"ceiling_touches": touches_ok, "rising_pivot_lows": rising_ok, "range_contraction": range_ok,
              "close_near_ceiling": close_ok, "relative_performance": rel_ok}
    features = {
        "ceiling": ceiling, "touch_indices": touches, "pivots": pivots, "last_pivot_rise_fraction": rise,
        "first_half_range": r1, "second_half_range": r2, "range_ratio": range_ratio,
        "close": close, "close_vs_ceiling_fraction": dist, "window_return": ret, "benchmark_return": bret,
        "relative_return_pp": rel_pp, "checks": checks, "window_start_utc": fmt_utc(starts[0]),
        "window_end_utc": fmt_utc(bars[-1].end),
    }
    status = "SIGNAL" if all(checks.values()) else "NO_SIGNAL"
    failed = [k for k, v in checks.items() if not v]
    avail = max(max(b.available_at for b in bars), max(b.available_at for b in bbars))
    return DetectorResult(det, status, None if status == "SIGNAL" else "failed:" + ",".join(failed), features,
                          bars[-1].end, avail,
                          {"window_bars": [b.as_evidence() for b in bars], "benchmark_bars": [b.as_evidence() for b in bbars]})


# ---------------------------------------------------------------------- IGNITION
def ignition(view: SeriesView, bench: SeriesView | None, cutoff: datetime, params: dict, volume_ok: bool) -> DetectorResult:
    det, p, vb = "IGNITION", params["ignition"], params["volume_baseline"]
    if not volume_ok:
        return _unavailable(det, "volume_scope_or_unit_unknown")
    stale = _fresh(view, cutoff, params["maximum_bar_age_seconds"], det)
    if stale:
        return stale
    latest = view.usable[-1]
    starts = grid_back(latest.start, p["impulse_bars"], allow_session_break=False)
    if starts is None:
        return _insufficient(det, "fewer_than_impulse_bars_in_current_session")
    missing = [s for s in starts if s not in view.by_start]
    if missing:
        return _insufficient(det, _missing_reason(view, missing, "impulse"))
    if bench is None:
        return _unavailable(det, "benchmark_not_declared")
    bmissing = [s for s in starts if s not in bench.by_start]
    if bmissing:
        return _insufficient(det, _missing_reason(bench, bmissing, "benchmark"))
    bars = [view.by_start[s] for s in starts]
    bbars = [bench.by_start[s] for s in starts]
    ret = _ret(bars[0].open, bars[-1].close)
    bret = _ret(bbars[0].open, bbars[-1].close)
    rel_pp = (ret - bret) * 100.0
    slots = [b.clock_slot for b in bars]
    base = matched_baseline(view, date.fromisoformat(latest.session_date), slots,
                            vb["minimum_prior_sessions"], vb["maximum_prior_sessions"])
    if base["median"] is None:
        return _insufficient(det, "baseline_fewer_than_minimum_sessions", baseline_sessions=base["n"],
                             required=vb["minimum_prior_sessions"])
    if base["median"] <= 0:
        return _insufficient(det, "zero_baseline_median")
    impulse_vol = sum(b.volume for b in bars)
    multiple = impulse_vol / base["median"]
    checks = {
        "return": ret >= p["minimum_return_fraction"],
        "relative_return": rel_pp >= p["minimum_relative_return_percentage_points"],
        "matched_volume": multiple >= p["minimum_matched_volume_multiple"],
    }
    features = {"impulse_return": ret, "benchmark_return": bret, "relative_return_pp": rel_pp,
                "impulse_volume": impulse_vol, "baseline_median": base["median"], "baseline_sessions": base["n"],
                "volume_multiple": multiple, "clock_slots": slots, "checks": checks,
                "requires_successful_pullback": p["requires_successful_pullback"]}
    status = "SIGNAL" if all(checks.values()) else "NO_SIGNAL"
    failed = [k for k, v in checks.items() if not v]
    avail = max([b.available_at for b in bars + bbars])
    return DetectorResult(det, status, None if status == "SIGNAL" else "failed:" + ",".join(failed), features,
                          bars[-1].end, avail,
                          {"impulse_bars": [b.as_evidence() for b in bars], "benchmark_bars": [b.as_evidence() for b in bbars],
                           "baseline": base})


# ---------------------------------------------------------------------- EQ_GAP
def eq_gap(view: SeriesView, bench: SeriesView | None, cutoff: datetime, params: dict, volume_ok: bool) -> DetectorResult:
    det, p = "EQ_GAP", params["eq_gap"]
    if not volume_ok:
        return _unavailable(det, "volume_scope_or_unit_unknown")
    stale = _fresh(view, cutoff, params["maximum_bar_age_seconds"], det)
    if stale:
        return stale
    today = date.fromisoformat(view.usable[-1].session_date)
    s_today = cal.session(today)
    prev = cal.previous_session(today)
    s_prev = cal.session(prev)
    first_start = s_today.open_utc
    prior_start = s_prev.close_utc - timedelta(seconds=INTERVAL)
    first, prior = view.by_start.get(first_start), view.by_start.get(prior_start)
    if first is None or prior is None:
        missing = [s for s, b in ((first_start, first), (prior_start, prior)) if b is None]
        return _insufficient(det, _missing_reason(view, missing, "gap"))
    if bench is None:
        return _unavailable(det, "benchmark_not_declared")
    bf, bp = bench.by_start.get(first_start), bench.by_start.get(prior_start)
    if bf is None or bp is None:
        missing = [s for s, b in ((first_start, bf), (prior_start, bp)) if b is None]
        return _insufficient(det, _missing_reason(bench, missing, "benchmark"))
    gap = first.open / prior.close - 1.0
    bgap = bf.open / bp.close - 1.0
    excess_pp = (gap - bgap) * 100.0
    vcfg = p["first_bar_matched_volume"]
    base = matched_baseline(view, today, [first.clock_slot], vcfg["minimum_prior_sessions"], vcfg["maximum_prior_sessions"])
    if base["median"] is None:
        return _insufficient(det, "baseline_fewer_than_minimum_sessions", baseline_sessions=base["n"])
    if base["median"] <= 0:
        return _insufficient(det, "zero_baseline_median")
    multiple = first.volume / base["median"]
    checks = {"gap": gap >= p["minimum_gap_fraction"],
              "gap_excess": excess_pp >= p["minimum_gap_excess_over_benchmark_percentage_points"],
              "first_bar_volume": multiple >= vcfg["minimum_multiple"]}
    features = {"gap": gap, "benchmark_gap": bgap, "gap_excess_pp": excess_pp, "prior_close": prior.close,
                "first_open": first.open, "first_bar_volume": first.volume, "baseline_median": base["median"],
                "baseline_sessions": base["n"], "volume_multiple": multiple, "checks": checks,
                "session_date": today.isoformat(), "previous_session_date": prev.isoformat()}
    status = "SIGNAL" if all(checks.values()) else "NO_SIGNAL"
    failed = [k for k, v in checks.items() if not v]
    avail = max(b.available_at for b in (first, prior, bf, bp))
    return DetectorResult(det, status, None if status == "SIGNAL" else "failed:" + ",".join(failed), features,
                          first.end, avail,
                          {"gap_bars": [prior.as_evidence(), first.as_evidence()],
                           "benchmark_bars": [bp.as_evidence(), bf.as_evidence()], "baseline": base})


# ---------------------------------------------------------------------- dispatcher
def dataset_problems(dataset: Any) -> dict[str, str]:
    """Detector-level UNAVAILABLE reasons caused by dataset metadata."""
    problems: dict[str, str] = {}
    base_issue = None
    if dataset["interval_seconds"] != INTERVAL:
        base_issue = f"interval_{dataset['interval_seconds']}s_not_300s"
    elif dataset["session_timezone"] != cal.EXCHANGE_TZ:
        base_issue = "session_timezone_not_America/New_York"
    elif dataset["calendar_id"] != cal.CALENDAR_ID:
        base_issue = "calendar_mismatch"
    elif dataset["price_adjustment"] == "unknown":
        base_issue = "price_adjustment_unknown"
    if base_issue:
        return {d: base_issue for d in DETECTORS}
    return problems


def run_detectors(view: SeriesView, bench: SeriesView | None, cutoff: datetime, dataset: Any,
                  instrument: Any | None) -> list[DetectorResult]:
    v1, _ = load_v1()
    eq, _ = load_eq()
    shared = {k: v1[k] for k in ("bar_interval_seconds", "maximum_bar_age_seconds", "volume_baseline")}
    problems = dataset_problems(dataset)
    volume_ok = dataset["volume_scope"] != "unknown" and dataset["volume_unit"] != "unknown"
    results = []
    for det in DETECTORS:
        if det in problems:
            results.append(_unavailable(det, problems[det]))
            continue
        if instrument is None:
            results.append(_unavailable(det, "instrument_metadata_missing"))
            continue
        if instrument["currency"] and instrument["currency"] != dataset["currency"]:
            results.append(_unavailable(det, "instrument_currency_differs_from_dataset"))
            continue
        try:
            if det == "COMPRESSION":
                r = compression(view, bench, cutoff, {"compression": v1["compression"], **shared})
            elif det == "IGNITION":
                r = ignition(view, bench, cutoff, {"ignition": v1["ignition"], **shared}, volume_ok)
            else:
                r = eq_gap(view, bench, cutoff, {"eq_gap": eq["eq_gap"], "maximum_bar_age_seconds": v1["maximum_bar_age_seconds"]}, volume_ok)
        except cal.CalendarUnavailable as exc:
            r = _unavailable(det, f"calendar_unavailable: {exc}")
        except Exception as exc:  # a detector crash is a FAILED result, never a negative market result
            r = DetectorResult(det, "FAILED", f"{type(exc).__name__}: {exc}")
        results.append(r)
    return results
