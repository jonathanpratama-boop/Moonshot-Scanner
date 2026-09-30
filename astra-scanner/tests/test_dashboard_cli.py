"""Dashboard states (no signals vs incomplete vs not scanned), escaping, CLI and the full demo."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from astra import market, scan
from astra.cli import main
from astra.web.app import create_app

from .conftest import DATASET, FIX, make_db


def db_path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def test_dashboard_pages_render_and_escape(scanned_db):
    client = TestClient(create_app(db_path(scanned_db)))
    home = client.get("/")
    assert home.status_code == 200
    assert "SAMPLE DATA" in home.text and "COVERAGE INCOMPLETE" in home.text
    anns = client.get("/announcements").text
    assert "<script>alert" not in anns and "&lt;script&gt;alert" in anns and "<img src=x" not in anns
    cid = scanned_db.execute("SELECT candidate_id FROM candidates LIMIT 1").fetchone()[0]
    assert client.get(f"/candidates/{cid}").status_code == 200
    for path in ("/candidates", "/runs", "/outcomes", "/notifications", "/healthz"):
        assert client.get(path).status_code == 200, path
    run = scanned_db.execute("SELECT run_id FROM runs WHERE kind='anomaly_scan'").fetchone()[0]
    page = client.get(f"/runs/{run}").text
    assert "INSUFFICIENT_DATA" in page and "NO_SIGNAL" in page and "SIGNAL" in page
    st = client.get("/api/status").json()
    assert st["mode"] == "sample" and st["sample_data_present"] is True
    assert client.get("/candidates/nope").status_code == 404


def test_not_scanned_vs_no_signal_in_completed_coverage(tmp_path, clock):
    conn = make_db(tmp_path)
    client = TestClient(create_app(db_path(conn)))
    assert "NOT SCANNED" in client.get("/").text
    market.register_dataset(conn, json.loads((FIX / "dataset.json").read_text()))
    market.import_instruments(conn, FIX / "instruments.json")
    quiet = tmp_path / "quiet.json"
    quiet.write_text(json.dumps({"format": "astra.universe.v1", "universe_id": "QUIET@1", "members": ["ZSMPC", "ZSMPF"]}))
    market.import_universe(conn, quiet)
    market.import_bars(conn, DATASET, FIX / "bars_stage1.csv")
    res = scan.scan(conn, DATASET, "QUIET@1")
    assert res["coverage_status"] == "SCANNED" and not res["signals"]
    text = client.get("/").text
    assert "NO SIGNAL IN COMPLETED COVERAGE" in text and "2/2 declared members fully evaluated" in text


def test_cli_happy_path_and_errors(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "init", "--mode", "sample"]) == 0
    assert main(["--db", db, "dataset", str(FIX / "dataset.json")]) == 0
    assert main(["--db", db, "instruments", str(FIX / "instruments.json")]) == 0
    assert main(["--db", db, "universe", str(FIX / "universe.json")]) == 0
    assert main(["--db", db, "bars", str(FIX / "bars_stage1.csv"), "--dataset", DATASET]) == 0
    assert main(["--db", db, "scan", "--dataset", DATASET, "--universe", "SAMPLE-US-7@1"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "status"]) == 0
    out = capsys.readouterr().out
    assert "SAMPLE DATA" in out and "COVERAGE INCOMPLETE" in out
    assert main(["--db", db, "init", "--mode", "live"]) == 2  # mode is fixed per database
    assert main(["--db", db, "serve", "--host", "0.0.0.0"]) == 2  # no remote bind without --allow-remote
    assert main(["--db", db, "research-ai", "--candidate", "x"]) == 2
    err = capsys.readouterr().err
    assert "AI_RESEARCH_DISABLED" in err
    assert main(["--db", str(tmp_path / "missing.db"), "status"]) == 2


def test_full_demo_workflow(tmp_path, monkeypatch):
    from astra.demo import run_demo

    monkeypatch.setenv("ASTRA_OUTBOX_DIR", str(tmp_path / "outbox"))
    res = run_demo(tmp_path / "demo.db")
    assert res["states"] == {"DETECTED": 2, "REJECTED": 1, "RESEARCHED": 2}
    assert (tmp_path / "demo_exports" / "comparison.csv").exists()
