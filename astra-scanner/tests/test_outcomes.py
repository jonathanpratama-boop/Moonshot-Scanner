"""Forward outcomes: frozen definition, pending/measured/unknown/censored, no excursions."""
from __future__ import annotations

import pytest

from astra import market, outcomes, processing
from astra.core import AstraError

from .conftest import DATASET, FIX


def current(conn):
    return {(o["symbol"], o["subject_type"], o["reference_role"], o["horizon_sessions"]): o for o in outcomes.current_observations(conn)}


def test_pending_then_measured_unknown_censored(scanned_db, clock):
    processing.process_due(scanned_db, worker="w")
    before = current(scanned_db)
    assert before[("ZSMPA", "candidate", "SIGNAL_BAR_REFERENCE", 1)]["status"] == "PENDING_DATA"
    clock.advance(hours=1)
    market.import_bars(scanned_db, DATASET, FIX / "bars_stage2.csv")
    processing.process_due(scanned_db, worker="w")
    now = current(scanned_db)
    a1 = now[("ZSMPA", "candidate", "SIGNAL_BAR_REFERENCE", 1)]
    assert a1["status"] == "MEASURED" and a1["endpoint_session_date"] == "2026-09-18"
    assert a1["reference_session_date"] == "2026-09-17" and a1["excess_return"] is not None
    assert now[("ZSMPA", "candidate", "SIGNAL_BAR_REFERENCE", 5)]["endpoint_session_date"] == "2026-09-24"
    c3 = now[("ZSMPC", "sampled_non_signal", "SIGNAL_BAR_REFERENCE", 3)]
    assert c3["status"] == "CENSORED" and "coverage ended" in c3["reason"]
    f3 = now[("ZSMPF", "candidate", "OBSERVATION_BAR_REFERENCE", 3)]
    assert f3["status"] == "UNKNOWN" and f3["reason"] == "endpoint_bar_missing"
    # PRE_EVENT reference uses the last close before the 07:45 ET publication
    pre = now[("ZSMPF", "candidate", "PRE_EVENT_REFERENCE", 1)]
    assert pre["reference_session_date"] == "2026-09-16" and pre["status"] == "MEASURED"
    # history kept: the earlier PENDING observation still exists
    n = scanned_db.execute("SELECT count(*) FROM outcome_observations WHERE status='PENDING_DATA'").fetchone()[0]
    assert n > 0


def test_measured_is_final_and_idempotent(scanned_db, clock):
    market.import_bars(scanned_db, DATASET, FIX / "bars_stage2.csv")
    processing.process_due(scanned_db, worker="w")
    n1 = scanned_db.execute("SELECT count(*) FROM outcome_observations").fetchone()[0]
    processing.process_due(scanned_db, worker="w")
    assert scanned_db.execute("SELECT count(*) FROM outcome_observations").fetchone()[0] == n1
    with pytest.raises(Exception, match="append-only"):
        scanned_db.execute("UPDATE outcome_observations SET return_value=1.0")


def test_definition_is_frozen(scanned_db, monkeypatch):
    processing.process_due(scanned_db, worker="w")
    monkeypatch.setitem(outcomes.SPEC, "horizons_sessions", [1, 2])
    with pytest.raises(AstraError, match="spec changed"):
        outcomes.ensure_definition(scanned_db)


def test_no_excursions_or_trading_returns(scanned_db):
    assert outcomes.SPEC["excursions"].startswith("NOT_MEASURED")
    cols = {r[1] for r in scanned_db.execute("PRAGMA table_info(outcome_observations)")}
    assert not {"mfe", "mae", "max_high", "pnl", "fill_price"} & cols
    assert outcomes.SPEC["scoring_baseline_eligible"] is False
