from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from astra import core
from astra.core import UTC

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "sample"
DATASET = "sample-synthetic-5m-rth"
UNIVERSE = "SAMPLE-US-7@1"


class FakeClock:
    source = "fake_test_clock"

    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_OUTBOX_DIR", str(tmp_path / "outbox"))
    for k in ("ASTRA_AI_PAID_CALLS", "ASTRA_AI_PROVIDER", "ASTRA_SEC_USER_AGENT"):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def clock():
    old = core.CLOCK
    c = FakeClock(datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC))
    core.CLOCK = c
    yield c
    core.CLOCK = old


def make_db(tmp_path: Path, mode: str = "sample"):
    from astra.db import init_db

    return init_db(tmp_path / f"{mode}.db", mode)


@pytest.fixture
def sample_db(tmp_path, clock):
    """Sample DB with dataset, instruments, universe and stage-1 bars."""
    from astra import market

    conn = make_db(tmp_path)
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    market.import_bars(conn, DATASET, FIX / "bars_stage1.csv")
    return conn


@pytest.fixture
def scanned_db(sample_db, clock):
    """sample_db + both announcement imports + one scan."""
    from astra import announcements, scan

    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    clock.advance(minutes=1)
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    clock.advance(minutes=1)
    scan.scan(sample_db, DATASET, UNIVERSE)
    clock.advance(minutes=1)
    return sample_db


def write_csv(path: Path, rows: list[dict], as_of: str) -> Path:
    import csv

    cols = ["symbol", "start", "open", "high", "low", "close", "volume", "complete"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "true" if c == "complete" else "") for c in cols})
    (path.parent / (path.name + ".meta.json")).write_text(json.dumps({"as_of": as_of}))
    return path


def cand_by(conn, symbol: str, route: str):
    return conn.execute("SELECT * FROM candidates WHERE symbol=? AND route=?", (symbol, route)).fetchone()
