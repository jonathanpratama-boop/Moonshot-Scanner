"""Generate the SYNTHETIC sample market fixtures (deterministic).

Nothing here is market data. Tickers ZSMPA..ZSMPG and benchmark XBMK are fictional.
Run from the project root:  python fixtures/generate_sample.py

Scenarios (all on 2026-09-17, stage-1 cutoff 11:00:30 America/New_York):
  ZSMPA  IGNITION impulse in the 10:45-10:55 bars (+~4.9%, ~6x matched volume)
  ZSMPB  COMPRESSION pattern over the 48 bars ending 10:55 (window spans the 16 Sep close)
  ZSMPC  quiet; stage-2 data stops after 2026-09-21 (outcomes become CENSORED)
  ZSMPD  two missing bars (10:10, 10:15) and a DATA_CONFLICT bar (10:50)
  ZSMPE  halted after the 10:15 bar (stale at cutoff); resumes 2026-09-18
  ZSMPF  quiet price; announcement-only issuer; stage-2 misses the 22 Sep 15:55 bar (UNKNOWN)
  ZSMPG  +7% regular-session opening gap with ~5x first-bar volume
  XBMK   synthetic benchmark (stands in for an index ETF)
Stage 1 also contains a FORMING 11:00 bar for several symbols; stage 2 supplies it complete.
"""
from __future__ import annotations

import csv
import json
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra import calendar as cal  # noqa: E402
from astra.core import UTC, fmt_utc  # noqa: E402

OUT = ROOT / "fixtures" / "sample"
FIRST = date(2026, 8, 26)
EVENT = date(2026, 9, 17)
LAST = date(2026, 9, 24)
STAGE1_ASOF = datetime(2026, 9, 17, 15, 0, 30, tzinfo=UTC)   # 11:00:30 ET
STAGE2_ASOF = datetime(2026, 9, 24, 20, 30, 0, tzinfo=UTC)   # 16:30 ET
SYMBOLS = {"XBMK": (500.0, 0.0004, 400000), "ZSMPA": (22.0, 0.0016, 30000), "ZSMPB": (49.0, 0.0012, 25000),
           "ZSMPC": (15.0, 0.0015, 40000), "ZSMPD": (31.0, 0.0014, 20000), "ZSMPE": (8.0, 0.0020, 60000),
           "ZSMPF": (64.0, 0.0010, 15000), "ZSMPG": (12.0, 0.0018, 50000)}


def slot_factor(i: int, n: int) -> float:
    return 1.0 + 1.5 * math.exp(-i / 6.0) + 1.0 * math.exp(-(n - 1 - i) / 6.0)


def compression_closes() -> list[float]:
    """48 closes: wide wave touching ~50.0, then a tighter wave with rising troughs."""
    out = []
    for i in range(24):
        out.append(49.2 + 0.78 * math.sin(2 * math.pi * i / 9 + 3.2))  # ends mid-range, rising
    for j in range(24):
        base = 49.40 + 0.017 * j
        amp = 0.26 - 0.007 * j
        out.append(base + amp * math.sin(2 * math.pi * j / 8))
    out[-1] = 49.84
    return out


def main() -> None:
    rng = random.Random(20260917)
    sessions = cal.sessions_between(FIRST, LAST)
    bars: dict[str, list[dict]] = {s: [] for s in SYMBOLS}
    comp = compression_closes()
    comp_starts = None
    for sym, (p0, sigma, vbase) in SYMBOLS.items():
        price = p0
        for d in sessions:
            s = cal.session(d)
            n = int((s.close_utc - s.open_utc).total_seconds() // 300)
            for i in range(n):
                start = s.open_utc + timedelta(minutes=5 * i)
                o = price
                if sym == "ZSMPG" and d == EVENT and i == 0:
                    o = price * 1.07
                c = o * (1 + rng.gauss(0, sigma)) if sym != "XBMK" else o * (1 + rng.gauss(0, sigma))
                vol = vbase * slot_factor(i, n) * math.exp(rng.gauss(0, 0.25))
                if sym == "ZSMPA" and d == EVENT and i in (15, 16, 17):
                    c = o * 1.016
                    vol *= 6.0
                if sym == "ZSMPG" and d == EVENT and i == 0:
                    vol *= 5.0
                if sym == "XBMK" and d == EVENT and i <= 18:
                    c = o * (1 + rng.gauss(0, 0.0001))
                wig = abs(rng.gauss(0, sigma / 2)) * o
                bars[sym].append({"start": start, "d": d, "i": i, "n": n, "open": o, "close": c,
                                  "high": max(o, c) + wig, "low": min(o, c) - wig, "volume": vol})
                price = c
    # Overwrite ZSMPB's 48-bar window ending at the 10:55 bar on EVENT with the designed pattern.
    b = bars["ZSMPB"]
    end_idx = next(k for k, x in enumerate(b) if x["d"] == EVENT and x["i"] == 17)
    win = b[end_idx - 47:end_idx + 1]
    prev_close = b[end_idx - 48]["close"]
    shift = comp[0] - prev_close
    for k in range(end_idx - 48, -1, -1):  # glide earlier history toward the window start
        b[k]["open"] += shift * max(0.0, 1 - (end_idx - 48 - k) / 60)
        b[k]["close"] += shift * max(0.0, 1 - (end_idx - 48 - k) / 60)
        b[k]["high"] = max(b[k]["high"], b[k]["open"], b[k]["close"])
        b[k]["low"] = min(b[k]["low"], b[k]["open"], b[k]["close"])
    prev = b[end_idx - 48]["close"]
    for k, x in enumerate(win):
        x["open"], x["close"] = prev, comp[k]
        x["high"], x["low"] = max(prev, comp[k]) + 0.02, min(prev, comp[k]) - 0.02
        if 0 < k < len(comp) - 1 and comp[k] < comp[k - 1] and comp[k] < comp[k + 1]:
            x["low"] = comp[k] - 0.05  # a distinct trough low (equal lows are not pivots)
        prev = comp[k]
    # after the window: shift the remaining path so it continues from the designed last close
    fix = comp[-1] - b[end_idx + 1]["open"]
    for x in b[end_idx + 1:]:
        for f in ("open", "close", "high", "low"):
            x[f] += fix
    comp_starts = (fmt_utc(win[0]["start"]), fmt_utc(win[-1]["start"]))

    stage1, stage2 = [], []
    for sym, rows in bars.items():
        for x in rows:
            start, d, i = x["start"], x["d"], x["i"]
            end = start + timedelta(minutes=5)
            complete = "true"
            conflict = False
            if sym == "ZSMPD" and d == EVENT and i in (8, 9):
                continue  # missing bars (never supplied)
            if sym == "ZSMPD" and d == EVENT and i == 16:
                conflict = True
            if sym == "ZSMPE" and d == EVENT and i >= 10:
                continue  # halted for the rest of the session
            if sym == "ZSMPC" and d > date(2026, 9, 21):
                continue  # coverage ends
            if sym == "ZSMPF" and d == date(2026, 9, 22) and i == x["n"] - 1:
                continue  # endpoint bar missing
            row = {"symbol": sym, "start": fmt_utc(start), "open": round(x["open"], 4),
                   "high": round(x["high"], 4), "low": round(x["low"], 4), "close": round(x["close"], 4),
                   "volume": int(x["volume"]), "complete": complete}
            if conflict:
                row["high"] = round(min(x["open"], x["close"]) - 0.05, 4)  # high below open/close
            if end <= STAGE1_ASOF:
                stage1.append(row)
            elif start < STAGE1_ASOF and sym not in ("ZSMPE",):
                forming = dict(row, complete="false", close=round((x["open"] + x["close"]) / 2, 4),
                               volume=int(x["volume"] * 0.3))
                forming["high"] = max(forming["high"], forming["close"])
                forming["low"] = min(forming["low"], forming["close"])
                stage1.append(forming)
                stage2.append(row)
            else:
                stage2.append(row)
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["symbol", "start", "open", "high", "low", "close", "volume", "complete"]
    for name, rows, asof in (("bars_stage1.csv", stage1, STAGE1_ASOF), ("bars_stage2.csv", stage2, STAGE2_ASOF)):
        with (OUT / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        (OUT / (name + ".meta.json")).write_text(json.dumps({
            "as_of": fmt_utc(asof), "synthetic": True,
            "note": "Batch snapshot time for this SYNTHETIC file. Not a real provider retrieval."}, indent=2) + "\n")
    print(f"stage1 rows={len(stage1)} stage2 rows={len(stage2)} compression window={comp_starts}")


if __name__ == "__main__":
    main()
