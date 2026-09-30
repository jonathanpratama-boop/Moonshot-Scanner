"""Chronology and replay cutoffs: no later information enters an earlier simulated decision."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from astra import announcements, market, research, scan
from astra.core import UTC, AstraError, parse_utc
from astra.market import PointInTimeStore
from astra.replay import run_replay

from .conftest import DATASET, FIX, UNIVERSE, make_db


@pytest.fixture
def replay_db(tmp_path, clock):
    conn = make_db(tmp_path, "replay")
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    market.import_instruments(conn, FIX / "instruments.json")
    market.import_universe(conn, FIX / "universe.json")
    market.import_bars(conn, DATASET, FIX / "bars_stage1.csv")
    clock.advance(minutes=1)
    market.import_bars(conn, DATASET, FIX / "bars_stage2.csv")
    announcements.import_local_json(conn, FIX / "announcements_backlog.json")
    announcements.import_local_json(conn, FIX / "announcements_new.json")
    return conn


def test_point_in_time_store_hides_later_bars_and_revisions(replay_db):
    store = PointInTimeStore(replay_db, DATASET, ["ZSMPA"], since=datetime(2026, 9, 1, tzinfo=UTC),
                             until=datetime(2026, 9, 30, tzinfo=UTC))
    at = datetime(2026, 9, 17, 15, 0, 30, tzinfo=UTC)
    bars = store.as_of(at)["ZSMPA"]
    assert all(b.available_at <= at for b in bars)
    assert max(b.start for b in bars) == datetime(2026, 9, 17, 15, 0, tzinfo=UTC)
    forming = [b for b in bars if b.start == datetime(2026, 9, 17, 15, 0, tzinfo=UTC)][0]
    assert forming.complete is False and forming.revision_no == 1  # the completed revision is not yet visible
    later = store.as_of(datetime(2026, 9, 24, 21, 0, tzinfo=UTC))["ZSMPA"]
    done = [b for b in later if b.start == datetime(2026, 9, 17, 15, 0, tzinfo=UTC)][0]
    assert done.complete is True and done.revision_no == 2


def test_replay_uses_only_information_available_at_each_cutoff(replay_db, clock):
    res = run_replay(replay_db, DATASET, UNIVERSE, date(2026, 9, 17), date(2026, 9, 17), every_minutes=15)
    assert res["cutoffs"] == 26  # 09:45 ... 16:00 ET every 15 minutes (close included once)
    rows = replay_db.execute("SELECT * FROM candidates WHERE mode='replay'").fetchall()
    assert rows, "replay produced candidates"
    for c in rows:
        # simulated cutoff kept distinct from actual software timestamps
        assert c["simulated_cutoff_utc"] and parse_utc(c["detected_at_utc"]) > parse_utc(c["simulated_cutoff_utc"])
        assert c["expires_at_utc"] is None
        ev = json.loads(replay_db.execute("SELECT content_json FROM evidence_snapshots WHERE evidence_id=?",
                                          (c["origin_evidence_id"],)).fetchone()[0])
        if c["route"] == "anomaly":
            cut = parse_utc(ev["requested_cutoff_utc"])
            for group in ("window_bars", "impulse_bars", "gap_bars", "benchmark_bars"):
                for b in (ev["inputs"] or {}).get(group, []):
                    assert parse_utc(b["available_at_utc"]) <= cut and parse_utc(b["end_utc"]) <= cut
        else:
            assert ev["announcement"]["published_at_utc"] <= c["simulated_cutoff_utc"]
    # the 07:45 ET announcement cannot be a replay candidate before its publication
    f = [c for c in rows if c["route"] == "announcement" and c["symbol"] == "ZSMPF"][0]
    assert f["simulated_cutoff_utc"] >= "2026-09-17T11:45:00Z"
    # the designed ignition (bars ending 11:00 ET) must not be detected at any earlier cutoff
    ign = replay_db.execute(
        "SELECT u.requested_cutoff_utc, d.status FROM detector_results d JOIN runs u USING(run_id) "
        "WHERE d.symbol='ZSMPA' AND d.detector='IGNITION' ORDER BY u.requested_cutoff_utc").fetchall()
    fired = [r[0] for r in ign if r[1] == "SIGNAL"]
    assert fired and min(fired) == "2026-09-17T15:00:30Z"
    # noise signals on synthetic random walks are retained, not suppressed (false-alert burden)
    assert replay_db.execute("SELECT count(*) FROM detector_results WHERE status='SIGNAL'").fetchone()[0] >= len(fired)
    # replay never notifies and is excluded from cohorts
    assert replay_db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    assert {r[0] for r in replay_db.execute("SELECT DISTINCT cohort_eligibility FROM runs WHERE kind='replay'")} == {"EXCLUDED_RETROSPECTIVE"}


def test_replay_research_must_be_retrospective(replay_db):
    run_replay(replay_db, DATASET, UNIVERSE, date(2026, 9, 17), date(2026, 9, 17), every_minutes=60)
    rec = json.loads((FIX / "research" / "ZSMPF_announcement.json").read_text())
    with pytest.raises(AstraError, match="retrospective"):
        research.import_research(replay_db, rec)
    rec["retrospective"] = True
    assert research.import_research(replay_db, rec)["state"] == "RESEARCHED"


def test_live_scan_refuses_explicit_cutoff_and_replay_requires_one(tmp_path, clock, replay_db):
    live = make_db(tmp_path, "live")
    with pytest.raises(AstraError):
        scan.resolve_cutoff(live, "live", DATASET, datetime(2026, 9, 17, tzinfo=UTC))
    with pytest.raises(AstraError, match="explicit simulated cutoff"):
        scan.scan(replay_db, DATASET, UNIVERSE)


def test_sample_scan_detection_time_is_actual_not_data_time(scanned_db):
    c = scanned_db.execute("SELECT * FROM candidates WHERE route='anomaly' AND symbol='ZSMPA'").fetchone()
    assert c["data_cutoff_utc"] == "2026-09-17T15:00:30Z"
    assert c["detected_at_utc"] == "2026-09-30T12:02:00Z"
    assert c["simulated_cutoff_utc"] is None
