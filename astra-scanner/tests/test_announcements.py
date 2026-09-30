"""Duplicate announcements, revisions, backlog vs new, and time handling."""
from __future__ import annotations

import json

import pytest

from astra import announcements
from astra.core import ValidationError, parse_source_time

from .conftest import FIX, cand_by


def test_backlog_then_new_classification_and_candidates(sample_db, clock):
    first = announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    assert first["BASELINE_BACKLOG"] == 3
    assert first["candidates_created"] == 0  # history is not news, even when flagged
    clock.advance(minutes=5)
    second = announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    assert second["NEW_PUBLICATION"] == 1
    assert second["NEWLY_FOUND_OLD_INFORMATION"] == 1
    assert second["NEW_OBSERVATION_TIME_UNKNOWN"] == 1
    assert second["sightings_only"] == 1 and second["revisions"] == 1
    assert second["candidates_created"] == 2
    f = cand_by(sample_db, "ZSMPF", "announcement")
    assert f["observation_class"] == "NEW_PUBLICATION"
    g = cand_by(sample_db, "ZSMPG", "announcement")
    assert g["observation_class"] == "NEWLY_FOUND_OLD_INFORMATION"


def test_repeated_retrieval_creates_no_duplicates(sample_db, clock):
    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    n_cand = sample_db.execute("SELECT count(*) FROM candidates").fetchone()[0]
    n_notif = sample_db.execute("SELECT count(*) FROM notifications").fetchone()[0]
    n_ann = sample_db.execute("SELECT count(*) FROM announcements").fetchone()[0]
    for _ in range(3):
        clock.advance(minutes=10)
        again = announcements.import_local_json(sample_db, FIX / "announcements_new.json")
        assert again["new_items"] == 0 and again["revisions"] == 0 and again["candidates_created"] == 0
    assert sample_db.execute("SELECT count(*) FROM candidates").fetchone()[0] == n_cand
    assert sample_db.execute("SELECT count(*) FROM notifications").fetchone()[0] == n_notif
    assert sample_db.execute("SELECT count(*) FROM announcements").fetchone()[0] == n_ann
    # every retrieval is still recorded as a sighting
    sightings = sample_db.execute(
        "SELECT count(*) FROM announcement_sightings s JOIN announcements a USING(announcement_id) WHERE a.source_key='SW-0004'"
    ).fetchone()[0]
    assert sightings == 4


def test_revision_appended_original_preserved(sample_db, clock):
    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    aid = sample_db.execute("SELECT announcement_id FROM announcements WHERE source_key='SW-0003'").fetchone()[0]
    revs = sample_db.execute("SELECT revision_no, content_json FROM announcement_revisions WHERE announcement_id=? ORDER BY revision_no",
                             (aid,)).fetchall()
    assert [r[0] for r in revs] == [1, 2]
    assert "version 1" in json.loads(revs[0][1])["body"] and "version 2" in json.loads(revs[1][1])["body"]
    with pytest.raises(Exception, match="append-only"):
        sample_db.execute("UPDATE announcement_revisions SET content_json='{}' WHERE announcement_id=?", (aid,))
    with pytest.raises(Exception, match="append-only"):
        sample_db.execute("DELETE FROM announcements WHERE announcement_id=?", (aid,))


def test_revision_of_candidate_announcement_attaches_evidence_and_notifies(sample_db, clock, tmp_path):
    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    data = json.loads((FIX / "announcements_new.json").read_text())
    data["items"][0]["body"] = "SYNTHETIC. Amended terms."
    p = tmp_path / "amended.json"
    p.write_text(json.dumps(data))
    clock.advance(hours=1)
    res = announcements.import_local_json(sample_db, p)
    assert res["revisions"] == 1 and res["candidates_created"] == 0
    f = cand_by(sample_db, "ZSMPF", "announcement")
    roles = [r[0] for r in sample_db.execute("SELECT role FROM candidate_evidence WHERE candidate_id=?", (f["candidate_id"],))]
    assert sorted(roles) == ["announcement_revision", "origin"]
    kinds = [r[0] for r in sample_db.execute("SELECT kind FROM notifications WHERE candidate_id=?", (f["candidate_id"],))]
    assert "announcement_revised" in kinds


def test_publication_time_is_not_observation_time(sample_db, clock):
    announcements.import_local_json(sample_db, FIX / "announcements_backlog.json")
    clock.advance(minutes=3)
    announcements.import_local_json(sample_db, FIX / "announcements_new.json")
    a = sample_db.execute("SELECT * FROM announcements WHERE source_key='SW-0004'").fetchone()
    assert a["published_at_utc"] == "2026-09-17T11:45:00Z"  # 07:45 America/New_York (EDT)
    assert a["published_raw"] == "2026-09-17T07:45:00" and a["published_tz"] == "America/New_York"
    assert a["published_session_context"] == "PRE_MARKET"
    assert a["first_observed_at_utc"] == "2026-09-30T12:03:00Z"  # software clock, not publication
    c = cand_by(sample_db, "ZSMPF", "announcement")
    assert c["detected_at_utc"] == "2026-09-30T12:03:00Z"


def test_naive_time_without_zone_is_not_guessed():
    with pytest.raises(ValidationError):
        parse_source_time("2026-09-17T07:45:00")
    dt, label = parse_source_time("2026-09-17T07:45:00-04:00")
    assert dt.isoformat() == "2026-09-17T11:45:00+00:00" and label == "-04:00"


def test_sample_announcements_refused_in_live_db(tmp_path, clock):
    from astra.core import AstraError
    from .conftest import make_db

    live = make_db(tmp_path, "live")
    with pytest.raises(AstraError, match="sample"):
        announcements.import_local_json(live, FIX / "announcements_backlog.json")
