"""Missing, stale, forming and conflicting bars; metadata gaps; frozen configs; calendar."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from astra import calendar as cal
from astra import detectors, market, scan
from astra.core import UTC, AstraError, ValidationError

from .conftest import DATASET, FIX, UNIVERSE, make_db, write_csv


def results(conn, run_id):
    return {(r["symbol"], r["detector"]): (r["status"], r["reason"]) for r in
            conn.execute("SELECT * FROM detector_results WHERE run_id=?", (run_id,))}


def test_every_member_gets_a_result_and_gaps_are_not_negative(sample_db):
    res = scan.scan(sample_db, DATASET, UNIVERSE)
    r = results(sample_db, res["run_id"])
    assert len(r) == 7 * 3
    assert r[("ZSMPA", "IGNITION")][0] == "SIGNAL"
    assert r[("ZSMPB", "COMPRESSION")][0] == "SIGNAL"
    assert r[("ZSMPG", "EQ_GAP")][0] == "SIGNAL"
    # missing bars -> INSUFFICIENT_DATA, never NO_SIGNAL
    assert r[("ZSMPD", "COMPRESSION")] == ("INSUFFICIENT_DATA", "window_bars_unusable:data_conflict+missing")
    # DATA_CONFLICT inside the impulse window
    assert r[("ZSMPD", "IGNITION")] == ("INSUFFICIENT_DATA", "impulse_bars_unusable:data_conflict")
    # halted symbol -> stale
    assert all(r[("ZSMPE", d)][0] == "INSUFFICIENT_DATA" and r[("ZSMPE", d)][1].startswith("stale_latest_bar")
               for d in detectors.DETECTORS)
    run = sample_db.execute("SELECT * FROM runs WHERE run_id=?", (res["run_id"],)).fetchone()
    cov = json.loads(run["coverage_json"])
    assert run["coverage_status"] == "PARTIAL"
    assert cov["per_symbol"]["ZSMPE"] == "NOT_EVALUATED" and cov["per_symbol"]["ZSMPD"] == "PARTIAL"
    assert "COVERAGE INCOMPLETE" in cov["headline"]
    scope = json.loads(run["declared_scope_json"])
    assert scope["members"] == ["ZSMPA", "ZSMPB", "ZSMPC", "ZSMPD", "ZSMPE", "ZSMPF", "ZSMPG"]
    assert run["data_cutoff_utc"] == "2026-09-17T15:00:30Z" and run["cohort_eligibility"] == "EXCLUDED_SYNTHETIC"


def test_forming_bar_is_retained_but_never_used(sample_db):
    forming = sample_db.execute("SELECT * FROM bars WHERE symbol='ZSMPA' AND source_complete=0").fetchone()
    assert forming["start_utc"] == "2026-09-17T15:00:00Z"
    scan.scan(sample_db, DATASET, UNIVERSE)
    ev = sample_db.execute("SELECT content_json FROM evidence_snapshots WHERE symbol='ZSMPA'").fetchone()
    content = json.loads(ev["content_json"])
    assert content["signal_bar_reference"]["bar_start_utc"] == "2026-09-17T14:55:00Z"  # last COMPLETED bar
    assert content["signal_bar_reference"]["scoring_baseline_eligible"] is False
    assert all(b["start_utc"] != "2026-09-17T15:00:00Z" for b in content["inputs"]["impulse_bars"])


def test_scan_outside_rth_is_insufficient_not_negative(sample_db):
    late = datetime(2026, 9, 17, 23, 0, tzinfo=UTC)  # 19:00 ET, after the close
    res = scan.scan(sample_db, DATASET, UNIVERSE, cutoff=late)
    run = sample_db.execute("SELECT * FROM runs WHERE run_id=?", (res["run_id"],)).fetchone()
    assert run["coverage_status"] == "NOT_SCANNED"
    reasons = {r["reason"] for r in sample_db.execute("SELECT reason FROM detector_results WHERE run_id=?", (res["run_id"],))}
    assert reasons == {"stale_latest_bar_market_not_in_rth"}
    assert not res["signals"]


def test_metadata_unknown_marks_dependent_calculations_unavailable(tmp_path, clock):
    conn = make_db(tmp_path)
    d = json.loads((FIX / "dataset.json").read_text())
    d["dataset"]["dataset_id"] = "vol-unknown"
    d["dataset"]["volume_scope"] = "unknown"
    market.register_dataset(conn, d)
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    market.import_bars(conn, "vol-unknown", FIX / "bars_stage1.csv")
    res = scan.scan(conn, "vol-unknown", UNIVERSE)
    r = results(conn, res["run_id"])
    assert r[("ZSMPA", "IGNITION")] == ("UNAVAILABLE", "volume_scope_or_unit_unknown")
    assert r[("ZSMPG", "EQ_GAP")] == ("UNAVAILABLE", "volume_scope_or_unit_unknown")
    assert r[("ZSMPB", "COMPRESSION")][0] == "SIGNAL"  # price-only detector still evaluates


def test_price_adjustment_unknown_blocks_all_price_detectors(tmp_path, clock):
    conn = make_db(tmp_path)
    d = json.loads((FIX / "dataset.json").read_text())
    d["dataset"]["dataset_id"] = "adj-unknown"
    d["dataset"]["price_adjustment"] = "unknown"
    market.register_dataset(conn, d)
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    market.import_bars(conn, "adj-unknown", FIX / "bars_stage1.csv")
    res = scan.scan(conn, "adj-unknown", UNIVERSE)
    assert {s for s, _ in results(conn, res["run_id"]).values()} == {"UNAVAILABLE"}


def test_dataset_properties_cannot_be_mixed(sample_db):
    d = json.loads((FIX / "dataset.json").read_text())
    d["dataset"]["price_adjustment"] = "split_adjusted"
    with pytest.raises(AstraError, match="different properties"):
        market.register_dataset(sample_db, d)


def test_bar_validation_conflicts_and_future_as_of(tmp_path, clock):
    conn = make_db(tmp_path)
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    rows = [
        {"symbol": "ZSMPA", "start": "2026-09-17T13:30:00Z", "open": 10, "high": 9, "low": 8, "close": 9.5, "volume": 100},
        {"symbol": "ZSMPA", "start": "2026-09-17T13:35:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": -1},
        {"symbol": "ZSMPA", "start": "2026-09-17T13:42:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"symbol": "ZSMPA", "start": "2026-09-17T14:00:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"symbol": "ZSMPA", "start": "2026-09-17T14:05:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"symbol": "ZSMPA", "start": "2026-09-17T14:05:00Z", "open": 10, "high": 12, "low": 9, "close": 11.5, "volume": 1},
    ]
    p = write_csv(tmp_path / "b.csv", rows, as_of="2026-09-17T14:02:00Z")
    res = market.import_bars(conn, DATASET, p)
    q = {r["start_utc"]: r["quality_reason"] for r in conn.execute("SELECT * FROM bars")}
    assert q["2026-09-17T13:30:00Z"] == "high_low_inconsistent"
    assert q["2026-09-17T13:35:00Z"] == "negative_volume"
    assert "misaligned_to_session_grid" in q["2026-09-17T13:42:00Z"]
    assert q["2026-09-17T14:00:00Z"] == "complete_bar_ends_after_batch_as_of"
    assert "conflicting_duplicate_rows_in_batch" in q["2026-09-17T14:05:00Z"]
    assert res["data_conflict"] == 5
    future = write_csv(tmp_path / "f.csv", rows[:1], as_of="2027-01-01T00:00:00Z")
    with pytest.raises(ValidationError, match="future"):
        market.import_bars(conn, DATASET, future)


def test_bar_revision_does_not_overwrite_history(sample_db, clock):
    before = sample_db.execute("SELECT count(*) FROM bar_revisions").fetchone()[0]
    clock.advance(days=1)
    res = market.import_bars(sample_db, DATASET, FIX / "bars_stage2.csv")
    assert res["revised"] == 7
    revs = sample_db.execute(
        "SELECT revision_no, source_complete FROM bar_revisions WHERE symbol='ZSMPA' AND start_utc='2026-09-17T15:00:00Z' ORDER BY revision_no"
    ).fetchall()
    assert [(r[0], r[1]) for r in revs] == [(1, 0), (2, 1)]
    assert sample_db.execute("SELECT count(*) FROM bar_revisions").fetchone()[0] == before + res["inserted"] + res["revised"]
    # re-import is incremental: nothing new
    again = market.import_bars(sample_db, DATASET, FIX / "bars_stage2.csv")
    assert again["unchanged"] == again["rows"]


def test_frozen_v1_config_tamper_is_refused(monkeypatch):
    original = detectors._read_config

    def tampered(name):
        text = original(name)
        return text.replace('"minimum_return_fraction": 0.03', '"minimum_return_fraction": 0.02') if name == "detectors-v1.json" else text

    monkeypatch.setattr(detectors, "_read_config", tampered)
    with pytest.raises(detectors.FrozenConfigError):
        detectors.load_v1()


def test_frozen_v1_parameters_unchanged():
    v1, digest = detectors.load_v1()
    assert digest == detectors.V1_SHA256
    assert v1["ignition"]["minimum_return_fraction"] == 0.03 and v1["compression"]["lookback_bars"] == 48


def test_universe_membership_immutable(sample_db, tmp_path):
    d = json.loads((FIX / "universe.json").read_text())
    d["members"].append("ZSMPQ")
    p = tmp_path / "u.json"
    p.write_text(json.dumps(d))
    with pytest.raises(AstraError, match="bump the version"):
        market.import_universe(sample_db, p)


def test_calendar_matches_published_nyse_dates():
    h26 = {d.isoformat() for d in cal.holidays(2026)}
    assert h26 == {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
                   "2026-09-07", "2026-11-26", "2026-12-25"}
    h27 = {d.isoformat() for d in cal.holidays(2027)}
    assert "2027-06-18" in h27 and "2027-07-05" in h27 and "2027-12-24" in h27 and "2027-12-31" not in h27
    assert set(cal.early_closes(2026)) == {date(2026, 11, 27), date(2026, 12, 24)}
    assert len(cal.rth_slots(date(2026, 11, 27))) == 42 and len(cal.rth_slots(date(2026, 9, 17))) == 78
    assert not cal.is_session(date(2025, 1, 9))  # special closure
    with pytest.raises(cal.CalendarUnavailable):
        cal.is_session(date(2028, 1, 3))
    # DST: 17 Sep is EDT (13:30Z open), 4 Dec is EST (14:30Z open)
    assert cal.session(date(2026, 9, 17)).open_utc.hour == 13 and cal.session(date(2026, 12, 4)).open_utc.hour == 14
