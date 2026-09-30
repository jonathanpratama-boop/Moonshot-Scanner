"""SEC adapter: configuration gate, real-format parsing, dedup/backlog, retries, failures."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from astra import sec
from astra.config import Settings
from astra.core import AstraError

from .conftest import ROOT, make_db

PAYLOAD = json.loads((ROOT / "fixtures" / "sec" / "CIK0000320193_submissions_trimmed.json").read_text())


@pytest.fixture
def issuers(tmp_path):
    p = tmp_path / "issuers.json"
    p.write_text(json.dumps({"format": "astra.sec_issuers.v1", "issuers": [
        {"cik": "320193", "ticker": "AAPL", "name": "Apple Inc."}]}))
    return p


def cfg(ua="ASTRA test harness test@example.com", **kw):
    s = Settings()
    s.sec_user_agent = ua
    s.sec_min_interval_seconds = 0.1
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def transport(responses, seen):
    def handler(request):
        seen.append(request)
        r = responses.pop(0) if len(responses) > 1 else responses[0]
        return r(request) if callable(r) else r
    return httpx.MockTransport(handler)


def test_missing_user_agent_sends_nothing_and_is_recorded(tmp_path, clock, issuers):
    conn = make_db(tmp_path, "live")
    seen = []
    with pytest.raises(sec.SecConfigError, match="SEC_USER_AGENT_NOT_CONFIGURED"):
        sec.collect(conn, issuers, cfg(ua=""), transport=transport([httpx.Response(200, json=PAYLOAD)], seen))
    assert seen == []
    run = conn.execute("SELECT * FROM runs WHERE kind='sec_collection'").fetchone()
    assert run["status"] == "failed" and run["coverage_status"] == "NOT_SCANNED" and "No request was sent" in run["error"]
    with pytest.raises(sec.SecConfigError, match="INVALID"):
        sec.collect(conn, issuers, cfg(ua="just-a-name"), transport=transport([httpx.Response(200, json=PAYLOAD)], seen))
    assert seen == []


def test_sample_database_refuses_sec(tmp_path, clock, issuers):
    conn = make_db(tmp_path, "sample")
    with pytest.raises(AstraError, match="live-mode"):
        sec.collect(conn, issuers, cfg())


def test_baseline_then_new_filing_then_duplicates(tmp_path, clock, issuers):
    conn = make_db(tmp_path, "live")
    seen, sleeps = [], []
    r1 = sec.collect(conn, issuers, cfg(), transport=transport([httpx.Response(200, json=PAYLOAD)], seen), sleep=sleeps.append)
    assert r1["coverage_status"] == "SCANNED" and r1["requests_sent"] == 1
    iss = r1["per_issuer"][0]
    assert iss["BASELINE_BACKLOG"] == 15 and iss["candidates_created"] == 0
    assert seen[0].headers["User-Agent"] == "ASTRA test harness test@example.com"
    assert str(seen[0].url) == "https://data.sec.gov/submissions/CIK0000320193.json"
    a = conn.execute("SELECT * FROM announcements WHERE form_type='8-K' ORDER BY published_at_utc DESC LIMIT 1").fetchone()
    assert a["published_time_kind"] == "SEC_ACCEPTANCE" and a["published_raw"].endswith(".000Z")
    assert a["published_at_utc"] == a["published_raw"][:19] + "Z"
    assert a["identity_status"] == "SEC_CIK_TICKER_MATCH" and a["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    src = conn.execute("SELECT coverage_label FROM sources").fetchone()[0]
    assert "FILINGS COVERAGE" in src and "does not establish complete or earliest issuer news" in src
    # a new 8-K appears after the watermark
    p2 = copy.deepcopy(PAYLOAD)
    rec = p2["filings"]["recent"]
    for k in rec:
        rec[k].insert(0, rec[k][0])
    rec["accessionNumber"][0] = "0000320193-26-999999"
    rec["form"][0] = "8-K"
    rec["items"][0] = "1.01,9.01"
    rec["acceptanceDateTime"][0] = "2026-09-30T20:30:00.000Z"
    rec["filingDate"][0] = "2026-09-30"
    rec["primaryDocument"][0] = "aapl-20260930.htm"
    clock.advance(hours=1)
    r2 = sec.collect(conn, issuers, cfg(), transport=transport([httpx.Response(200, json=p2)], seen), sleep=sleeps.append)
    iss2 = r2["per_issuer"][0]
    assert iss2["NEW_PUBLICATION"] == 1 and iss2["candidates_created"] == 1 and iss2["sightings_only"] == 15
    cand = conn.execute("SELECT * FROM candidates").fetchone()
    assert cand["route"] == "announcement" and cand["symbol"] == "AAPL" and cand["mode"] == "live"
    # repeated retrieval: no duplicate candidates or notifications
    n_notif = conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
    r3 = sec.collect(conn, issuers, cfg(), transport=transport([httpx.Response(200, json=p2)], seen), sleep=sleeps.append)
    assert r3["per_issuer"][0]["candidates_created"] == 0 and r3["per_issuer"][0]["new_items"] == 0
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == n_notif


def test_retry_after_429_and_rate_limit_floor(tmp_path, clock, issuers):
    conn = make_db(tmp_path, "live")
    seen, sleeps = [], []
    t = transport([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=PAYLOAD)], seen)
    r = sec.collect(conn, issuers, cfg(sec_min_interval_seconds=0.0), transport=t, sleep=sleeps.append)
    assert r["coverage_status"] == "SCANNED" and r["per_issuer"][0]["attempts"] == 2
    assert 3 in sleeps
    client = sec.SecClient("x y@z.com", min_interval=0.0, timeout=1, max_retries=0)
    assert client.min_interval == 0.1  # never faster than SEC's 10 requests/second


def test_failed_issuer_is_a_coverage_gap_not_a_negative(tmp_path, clock):
    conn = make_db(tmp_path, "live")
    p = tmp_path / "two.json"
    p.write_text(json.dumps({"format": "astra.sec_issuers.v1", "issuers": [
        {"cik": "320193", "ticker": "AAPL"}, {"cik": "9999999999", "ticker": "NOPE"}]}))
    seen = []

    def route(request):
        if "CIK0000320193" in str(request.url):
            return httpx.Response(200, json=PAYLOAD)
        return httpx.Response(404, text="Not Found")

    r = sec.collect(conn, p, cfg(), transport=httpx.MockTransport(lambda req: (seen.append(req), route(req))[1]), sleep=lambda s: None)
    assert r["status"] == "partial" and r["coverage_status"] == "PARTIAL"
    failed = [x for x in r["per_issuer"] if x["status"] == "FAILED"][0]
    assert failed["http_status"] == 404 and failed["attempts"] == 1  # 404 is not retried
    scope = conn.execute("SELECT * FROM source_scopes WHERE scope_key='sec:9999999999'").fetchone()
    assert scope["consecutive_failures"] == 1 and scope["first_success_at_utc"] is None


def test_issuer_bound_is_enforced(tmp_path):
    p = tmp_path / "many.json"
    p.write_text(json.dumps({"format": "astra.sec_issuers.v1", "issuers": [{"cik": str(i)} for i in range(1, 30)]}))
    with pytest.raises(Exception, match="ASTRA_SEC_MAX_ISSUERS"):
        sec.load_issuers(p, 25)
