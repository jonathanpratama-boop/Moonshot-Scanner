"""Bounded SEC EDGAR submissions adapter (free, no API key).

Follows SEC's published access guidance (checked 2026-09-30):
- https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
  "Current max request rate: 10 requests/second"; declare a User-Agent such as
  "Sample Company Name AdminContact@<sample company domain>.com".
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
  data.sec.gov/submissions/CIK##########.json; no authentication; `filings.recent` holds at
  least one year or 1,000 filings; typical processing delay under a second.

`acceptanceDateTime` is treated as UTC: on 2026-09-30, 1,006 recent AAPL filings showed
acceptance hours 10:00-02:00 UTC (= EDGAR's 06:00-22:00 ET window), and acceptances after
17:30 ET carried the next filing date, consistent with genuine UTC.

COVERAGE: filings for explicitly configured issuers only. This is not complete or earliest
issuer-news coverage. Only metadata is fetched; filing documents are not downloaded.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from .announcements import Item, get_or_create_source, ingest
from .config import Settings
from .core import AstraError, ValidationError, file_sha256, now_str
from .db import db_mode, tx
from .runs import finish_run, start_run

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SOURCE_NAME = "sec-edgar-submissions"
COVERAGE_LABEL = (
    "SEC EDGAR filings (submissions API, filings.recent) for explicitly configured issuers only. FILINGS COVERAGE: "
    "does not establish complete or earliest issuer news; press releases, wires and exchange notices may precede or "
    "never appear as filings. Metadata only; documents not fetched."
)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
UA_RE = re.compile(r"^\S.*\s\S+@\S+\.\S+$")


class SecConfigError(AstraError):
    pass


def validate_user_agent(ua: str) -> str:
    ua = (ua or "").strip()
    if not ua:
        raise SecConfigError(
            "SEC_USER_AGENT_NOT_CONFIGURED: set ASTRA_SEC_USER_AGENT to your organisation/name and contact email, "
            "e.g. 'Example Research admin@example.com', as SEC's fair-access policy requires. No request was sent."
        )
    if not UA_RE.match(ua) or len(ua) > 200:
        raise SecConfigError(
            "SEC_USER_AGENT_INVALID: ASTRA_SEC_USER_AGENT must contain a name followed by a contact email "
            "(SEC format 'Sample Company Name AdminContact@<domain>.com'). No request was sent."
        )
    return ua


def load_issuers(path: str | Path, max_issuers: int) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "astra.sec_issuers.v1":
        raise ValidationError("issuer file must have format 'astra.sec_issuers.v1'")
    out = []
    for it in data.get("issuers", []):
        try:
            cik = int(str(it["cik"]).lstrip("0") or "0")
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"issuer entry needs a numeric cik: {it}") from exc
        if cik <= 0:
            raise ValidationError(f"invalid cik in {it}")
        out.append({"cik": cik, "ticker": (it.get("ticker") or "").upper() or None, "name": it.get("name")})
    if not out:
        raise ValidationError("issuer list is empty")
    if len(out) > max_issuers:
        raise ValidationError(f"{len(out)} issuers configured; bound is ASTRA_SEC_MAX_ISSUERS={max_issuers}")
    return out


class SecClient:
    def __init__(self, user_agent: str, *, min_interval: float, timeout: float, max_retries: int,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.min_interval = max(min_interval, 0.1)  # never faster than 10 requests/second
        self.max_retries = max_retries
        self.sleep = sleep
        self._last = 0.0
        self.requests_sent = 0
        self.client = httpx.Client(headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                                   timeout=timeout, transport=transport, follow_redirects=False)

    def _throttle(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def get_submissions(self, cik: int) -> dict:
        url = SUBMISSIONS_URL.format(cik=cik)
        attempt, last_error, status = 0, None, None
        while attempt <= self.max_retries:
            attempt += 1
            self._throttle()
            self.requests_sent += 1
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as exc:
                last_error, status = f"{type(exc).__name__}: {exc}", None
                self.sleep(min(2 ** attempt, 8))
                continue
            status = resp.status_code
            if status == 200:
                if len(resp.content) > MAX_RESPONSE_BYTES:
                    return {"ok": False, "status": status, "error": "response exceeds size bound", "attempts": attempt, "url": url}
                try:
                    return {"ok": True, "status": status, "json": resp.json(), "attempts": attempt, "url": url}
                except ValueError as exc:
                    return {"ok": False, "status": status, "error": f"invalid JSON: {exc}", "attempts": attempt, "url": url}
            last_error = f"HTTP {status}: {resp.text[:200]!r}"
            if status in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After", "")
                self.sleep(min(float(retry_after) if retry_after.isdigit() else 2 ** attempt, 30))
                continue
            break  # 403/404 etc. are not retried
        return {"ok": False, "status": status, "error": last_error, "attempts": attempt, "url": url}

    def close(self) -> None:
        self.client.close()


def filing_items(payload: dict, cik: int, configured_ticker: str | None) -> list[Item]:
    try:
        recent = payload["filings"]["recent"]
        n = len(recent["accessionNumber"])
        cols = {k: recent[k] for k in ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument")}
    except (KeyError, TypeError) as exc:
        raise AstraError(f"SEC_SCHEMA_UNEXPECTED: submissions payload missing field {exc}") from exc
    tickers = [t.upper() for t in payload.get("tickers") or []]
    if configured_ticker is None:
        identity = "SEC_CIK_ONLY"
    elif configured_ticker in tickers:
        identity = "SEC_CIK_TICKER_MATCH"
    else:
        identity = f"TICKER_MISMATCH(configured {configured_ticker}, SEC lists {tickers})"
    items = []
    for i in range(n):
        row = {k: (recent[k][i] if isinstance(recent.get(k), list) and i < len(recent[k]) else None) for k in recent}
        acc = cols["accessionNumber"][i]
        acc_nodash = acc.replace("-", "")
        doc = cols["primaryDocument"][i]
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}" if doc
               else f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm")
        form = cols["form"][i]
        desc = row.get("primaryDocDescription") or ""
        items_str = row.get("items") or ""
        title = f"{form}: {desc}".strip(": ") + (f" (items {items_str})" if items_str else "")
        items.append(Item(
            source_key=acc, scope_key=f"sec:{cik:010d}", title=title,
            content={"cik": cik, "entity_name": payload.get("name"), "tickers": tickers, **row},
            symbol=configured_ticker, issuer_cik=f"{cik:010d}", issuer_name=payload.get("name"), form_type=form,
            sec_items=items_str or None, url=url, published_raw=cols["acceptanceDateTime"][i] or None,
            published_tz=None, published_time_kind="SEC_ACCEPTANCE", identity_status=identity,
        ))
    return items


def collect(conn: sqlite3.Connection, issuers_path: str | Path, cfg: Settings,
            transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> dict[str, Any]:
    mode = db_mode(conn)
    if mode == "sample":
        raise AstraError("SEC collection records real observations; use a live-mode (or replay-mode) database, not sample")
    issuers = load_issuers(issuers_path, cfg.sec_max_issuers)
    scope = {"issuers": issuers, "source": SOURCE_NAME, "endpoint": SUBMISSIONS_URL}
    run_id = start_run(conn, "sec_collection", mode, source_classification="OBSERVED_SOURCE", declared_scope=scope,
                       input_ref=f"{issuers_path} sha256={file_sha256(str(issuers_path))}")
    try:
        ua = validate_user_agent(cfg.sec_user_agent)
    except SecConfigError as exc:
        finish_run(conn, run_id, "failed", error=str(exc), coverage_status="NOT_SCANNED",
                   coverage={"status": "NOT_SCANNED", "reason": str(exc), "issuers_declared": len(issuers)})
        raise
    client = SecClient(ua, min_interval=cfg.sec_min_interval_seconds, timeout=cfg.sec_timeout_seconds,
                       max_retries=cfg.sec_max_retries, transport=transport, sleep=sleep)
    per_issuer, totals = [], {"candidates_created": 0, "new_items": 0, "revisions": 0, "sightings_only": 0}
    try:
        with tx(conn):
            source_id = get_or_create_source(conn, "sec_submissions", SOURCE_NAME, COVERAGE_LABEL, False)
        for iss in issuers:
            res = client.get_submissions(iss["cik"])
            entry = {"cik": f"{iss['cik']:010d}", "ticker": iss["ticker"], "http_status": res.get("status"),
                     "attempts": res.get("attempts"), "url": res.get("url")}
            if not res["ok"]:
                entry.update(status="FAILED", error=res.get("error"))
                with tx(conn):
                    conn.execute("INSERT OR IGNORE INTO source_scopes(scope_key, source_id) VALUES (?, ?)",
                                 (f"sec:{iss['cik']:010d}", source_id))
                    conn.execute("UPDATE source_scopes SET last_attempt_at_utc=?, last_error=?, "
                                 "consecutive_failures=consecutive_failures+1 WHERE scope_key=?",
                                 (now_str(), res.get("error"), f"sec:{iss['cik']:010d}"))
                per_issuer.append(entry)
                continue
            try:
                items = filing_items(res["json"], iss["cik"], iss["ticker"])
                with tx(conn):
                    summary = ingest(conn, source_id=source_id, run_id=run_id, items=items,
                                     scopes=[f"sec:{iss['cik']:010d}"], mode=mode, is_sample=False)
                entry.update(status="OK", filings_in_response=len(items), **{k: summary[k] for k in (
                    "new_items", "sightings_only", "revisions", "BASELINE_BACKLOG", "NEW_PUBLICATION",
                    "NEWLY_FOUND_OLD_INFORMATION", "candidates_created")})
                for k in totals:
                    totals[k] += summary[k]
            except AstraError as exc:
                entry.update(status="FAILED", error=str(exc))
            per_issuer.append(entry)
    except Exception as exc:
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}",
                   summary={"per_issuer": per_issuer, "requests_sent": client.requests_sent})
        raise
    finally:
        client.close()
    ok = sum(1 for e in per_issuer if e["status"] == "OK")
    cov_status = "SCANNED" if ok == len(issuers) else ("NOT_SCANNED" if ok == 0 else "PARTIAL")
    status = "completed" if cov_status == "SCANNED" else ("failed" if ok == 0 else "partial")
    coverage = {"status": cov_status, "issuers_declared": len(issuers), "issuers_ok": ok, "label": COVERAGE_LABEL,
                "failed": [e for e in per_issuer if e["status"] != "OK"],
                "note": "a failed request is a coverage gap, not a negative result"}
    finish_run(conn, run_id, status, summary={"per_issuer": per_issuer, "requests_sent": client.requests_sent, **totals},
               coverage=coverage, coverage_status=cov_status)
    return {"run_id": run_id, "status": status, "coverage_status": cov_status, "requests_sent": client.requests_sent,
            "per_issuer": per_issuer, **totals}
