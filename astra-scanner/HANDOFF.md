# ASTRA scanner — handoff for Codex

Prepared 2026-09-30 by Claude Code (cloud session). Scope: free local research prototype.
No purchases, subscriptions, hosting, ChatGPT alert changes or trades were made. Research
quality and trading performance remain **unproven**.

Machine-readable summary: `IMPLEMENTATION_STATUS.json`. Evidence: `docs/VERIFICATION.md`.

> **Status after review round 1 (2026-09-30).** ASTRA's independent review of `39c9c78` found
> nine integrity defects: replay chronology, SEC identity, AI evidence validation, outcome
> timing, alert comparison, material revisions, work recovery, the AI daily cap, and cohort
> labels. All nine were reproduced as failing tests and corrected
> (`docs/REVIEW_ROUND1.md`, `tests/test_review_round1.py`).
>
> **Not resolved:** COMPRESSION parity with the original v1 engine. The reported plateau-low
> divergence needs the original `trading/pilot/runner.py`. Historical results and the alert
> comparison should not be trusted until parity is established and a prospective comparison
> runs against real retained alerts.

---

## 1. What works, with exact commands

From the `astra-scanner/` directory (Python ≥ 3.11):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest -q                      # 85 tests (84 + 1 skipped without the optional SDK)
.venv/bin/astra demo --reset                       # complete SAMPLE workflow, self-checking
.venv/bin/astra --db var/demo.db status            # text summary (SAMPLE banner, coverage, backlog)
.venv/bin/astra --db var/demo.db serve             # read-only dashboard, http://127.0.0.1:8765
```

Working capabilities (all offline unless noted):

- **Announcement route**: retained local JSON import; bounded SEC EDGAR submissions adapter
  (`sec-collect`, live/replay DB, needs `ASTRA_SEC_USER_AGENT`); source identity, URL,
  publication time (raw + zone + kind + session context), actual first observation,
  content hash, appended revisions, sightings; backlog vs new vs newly-found-old
  classification; routing screen; candidates without any price move; no duplicate
  candidates/notifications on repeated retrieval.
- **Market-anomaly route**: documented CSV/JSON bar batches with dataset/provider/session
  metadata; incremental store with revisions; DATA_CONFLICT quarantine; forming bars kept but
  unusable; `MarketProvider` interface for a later live feed; detectors COMPRESSION and
  IGNITION (frozen v1 parameters, hash-checked) and EQ_GAP (new experimental version); every
  member × detector result retained (SIGNAL / NO_SIGNAL / INSUFFICIENT_DATA / UNAVAILABLE /
  FAILED); declared membership, cutoffs, config versions and coverage per run.
- **Evidence**: frozen before research, hashed, append-only (SQLite triggers), read back and
  verified in a separate table.
- **Research**: structured manual import (`research-import`), schema `astra.research.v1`,
  facts/inferences/unknowns, mechanisms, sources/contrary evidence, materiality, valuation
  scale, liquidity/financing/dilution/execution, blockers with clearing evidence, recheck and
  expiry; unknown fields rejected; revisions with reason types.
- **States**: DETECTED / RESEARCHED / BLOCKED / REJECTED / EXPIRED with reasons, evidence
  deltas and remaining blockers; CONDITIONAL_READY / ENTRY_ELIGIBLE reserved and unreachable.
- **Work**: `process-due` and `cycle` (doctrine priority order), leased queue with restart
  recovery, retries, dead-letter; research backlog; ownership.
- **Notifications**: local preview queue with dedup and r1.2 decision rows; queued →
  attempted (local outbox) → acknowledged; external channel `not_configured`.
- **Outcomes**: frozen definition `ASTRA-OUTCOME-EQ-2` (reference time recorded, fresh and
  point-in-time references, endpoints strictly after the reference time), reference roles, +1/+3/+5 sessions,
  MEASURED / PENDING_DATA / UNKNOWN / CENSORED, rejected candidates and sampled non-signals
  included, no excursions.
- **Replay**: `replay` with point-in-time bar revisions and simulated cutoffs kept separate
  from actual timestamps.
- **Exports**: candidates, detector-only baseline, outcomes, runs, and a comparison with
  retained hourly alerts, per source window with exact-time matching (`import-hourly-alerts`,
  `export comparison`).
- **Dashboard**: mode, SAMPLE DATA banner, last successful collections and data cutoffs,
  coverage (NO SIGNAL IN COMPLETED COVERAGE vs COVERAGE INCOMPLETE vs NOT SCANNED),
  candidates with evidence and unresolved items, next recheck/expiry, failures, backlog,
  notifications, outcomes. Localhost-only, read-only, escaped output.

## 2. What was actually executed and observed

Details and numbers: `docs/VERIFICATION.md`; raw outputs: `docs/TEST_RESULTS.txt`,
`docs/DEMO_TRANSCRIPT.txt`, `docs/DEMO_STATUS.txt`. Summary:

- After review round 1: `pytest` **85 passed** (main env), clean `pip` env **84 passed, 1 skipped**;
  `astra demo --reset` **27 checks**, states DETECTED 3 / RESEARCHED 2 / REJECTED 1, outcomes
  18 MEASURED / 13 UNKNOWN / 2 CENSORED; 28 dashboard pages HTTP 200; `0001` database upgraded
  through `0002`. The bullets below describe the original build at `39c9c78`.
- `pytest`: **68 passed** (main env); clean `pip` env: **67 passed, 1 skipped**.
- `astra demo --reset`: 26 self-checks passed; 3 synthetic detector signals; coverage PARTIAL
  with the reasons; research moved ZSMPA BLOCKED → RESEARCHED after a processed due recheck;
  outcomes 23 MEASURED / 2 UNKNOWN / 2 CENSORED after stage-2 data; 11 local notifications.
- Live dashboard served on `127.0.0.1:8765` only; all pages HTTP 200; screenshots inspected.
- CLI replay over 4 sample sessions: 52 cutoffs, 1,092 detector rows retained.
- SEC: network reachable (one manual HTTP 200 at 10:26:06Z, sent without a contact email).
  Adapter exercised via CLI with the current configuration → `SEC_USER_AGENT_NOT_CONFIGURED`,
  no request sent, failure recorded. A compliant live adapter run was **not** performed.
- No paid API call was made. Nothing ran on macOS.

## 3. Live integrations, sample data, mocks and untested components

| Component | Status |
|---|---|
| SEC EDGAR submissions adapter | implemented; verified with the **real** trimmed Apple payload through a **mock** transport; live run blocked by missing identity configuration |
| Market data | import-only; **no live provider**; `MarketProvider` is an interface only |
| Sample data | `fixtures/sample/*` are **synthetic** (fictional tickers ZSMPA–ZSMPG, benchmark XBMK); sample research is labelled SAMPLE RESEARCH |
| AI researcher (Anthropic, official SDK) | implemented, **disabled by default**; verified with fake clients and the real SDK against a mock transport; **never called live** |
| Notification delivery | local outbox only; no external channel |
| Hourly ChatGPT alert comparison | import format + export implemented and run on **synthetic** alerts; no real alert export imported |
| Untested | macOS runtime, live SEC with compliant UA, live market data, real-size universes, long-running operation, v1 engine equivalence with the original `runner.py` |

## 4. Doctrine files read and unresolved conflicts

Read in full from the user-uploaded bundle (hash-verified): `AGENTS.override.md`,
`TRADING_DOCTRINE.md` (GD-1.1), `trading/astra-operations-20260929.txt` (r1 + r1.2),
`trading/event-readiness-GD-1.1.txt`, `trading/STATE.md`, `trading/records/README.md`,
`trading/pilot/README.md`, `trading/pilot/detectors-v1.json`, `trading/live/README.md`,
`trading/live/v2-rules.json`, plus `READ_FIRST.md` and `MANIFEST.json`. The original project
directory itself was not accessible, and no original code was available.

Adopted rules and all open conflicts are listed in `doctrine/DOCTRINE_REFERENCE.md`.
Outstanding reconciliation, in short: (1) v1 engine equivalence unverified → ASTRA runs are
excluded from the authoritative v1 cohort; (2) ASTRA's SQLite ledger vs the file-based
`trading/records/` + `record_control.py` procedure; (3) BLOCKED used as a research state
here vs a plan status in event-readiness; (4) Mac-off monitoring requirement vs the later
"ChatGPT only, skip hosting" direction — this build is local and invocation-only; (5) alert
budget (0–1 urgent, ≤3 candidates) not yet a digest; (6) Saturday/Sunday cadence not
scheduled; (7) NYSE July-2026 footnote conflict; (8) STATE history not re-audited.

## 5. Reused components and provenance

| Reused | Provenance |
|---|---|
| v1 COMPRESSION/IGNITION parameters | `trading/pilot/detectors-v1.json`, byte-identical copy, SHA-256 `09297ed3303ebd5f0801d756cb6c46bc4966044b7ad2b51a30e648eaa8a21ddd`, checked at runtime |
| v1 rule semantics | reimplemented from `trading/pilot/README.md` (original engine not available) |
| Vocabulary and controls (reference roles, revision reason types, coverage labels, r1.2 alert row, outcome statuses, run priority) | GD-1.1 and ASTRA-OPS-20260929-r1.2 |
| SEC access rules | sec.gov "Accessing EDGAR Data" (10 req/s, declared User-Agent) and "EDGAR APIs" pages, read 2026-09-30 |
| NYSE holidays/early closes 2026–2027 | nyse.com hours-calendars page, read 2026-09-30 |
| Claude API usage (optional adapter) | Claude API guidance bundled with Claude Code, read 2026-09-30 |
| Crypto collector/event code (`trading/live/*.py`), `record_control.py`, templates | **not available**, not reused |

## 6. Database schema and migration instructions

`docs/SCHEMA.md` lists every table, which ones are append-only (enforced by triggers), and
the migration procedure. Summary: SQLite file per database, mode fixed at `astra init
--mode sample|replay|live`; migrations in `astra/migrations/NNNN_*.sql` apply automatically
on open, each checksummed; never edit an applied migration — add `0002_….sql`.

## 7. Known defects and operational limitations

- **COMPRESSION known divergence** from the original v1 detector on plateau lows (reported by
  ASTRA's review). This implementation requires strictly lower pivot lows. Parity is unverified
  for COMPRESSION and IGNITION. Do not treat ASTRA COMPRESSION results as v1 results.
- **Detectors are unvalidated**; v1 engine equivalence is unverified (interpretations listed
  in `astra/config/astra-eq-experimental-1.json`). Synthetic random walks already trigger noise
  signals in replay — expect false alerts.
- The nine review-round-1 defects are corrected; their reproductions are permanent regression
  tests. Parts of the corrected rules are deliberately conservative:
  - a corrected bar is only visible from its batch time;
  - a revision of a backlog item that still screens material becomes a candidate;
  - an AI reservation left by a crashed process keeps counting toward the daily cap.
- The optional AI adapter works only from saved context. It does not search the web or fetch
  filing documents, and it may not clear blockers.
- **Corporate actions are not modelled**: splits in unadjusted data look like moves; adjusted
  data embeds later corporate actions (hindsight in replay). Instrument metadata is current,
  not point-in-time.
- Outcome endpoints are the final RTH 5-minute bar close, not the official closing auction.
- Calendar is rule-based for 2019–2027 only; 2019–2025 not verified against an official
  archive; 2 July 2026 footnote conflict unresolved.
- Imported-history availability is **assumed** (bar end + declared latency); replay cannot
  prove historical live detection, delivery or executable prices.
- Sample mode deliberately mixes real operational timestamps with historical synthetic data
  (detection "now" on data through 17 Sep); expiries count from the real detection time.
- Live scanning of imported batches will correctly report `NOT_SCANNED` (stale) — a live
  provider adapter is needed for live anomaly scanning.
- Notifications are per event (deduplicated) without a capped digest; no external delivery.
- SEC: metadata only (documents not fetched); candidates without a configured ticker have no
  symbol and therefore no outcome subjects; filings coverage is not issuer-news coverage.
- Evidence read-back happens inside the same SQLite transaction (verifies stored bytes, not
  an independent archive). Timestamps are whole seconds (floor).
- Dashboard has no authentication (localhost bind only; `--allow-remote` must stay off).
- Outcome measurement re-scans all subjects each run (fine for small databases).
- Test-only `StarletteDeprecationWarning` (TestClient prefers `httpx2`).
- The single manual SEC connectivity request did not include a contact email.

## 8. Required accounts, credentials and activation steps

| Capability | Needed | Activation |
|---|---|---|
| Demo, tests, dashboard, manual research, replay | nothing | — |
| SEC filings (free) | no account/key; **a contact identity** | `ASTRA_SEC_USER_AGENT="Your Name or Org you@example.com"`; edit `config/sec_issuers.example.json`; `astra init --mode live` on a new DB; `astra --db DB sec-collect --issuers FILE` |
| Live market anomaly scanning | a market-data provider (not chosen) | implement `MarketProvider` → bar batches with `OBSERVED_RETRIEVAL`; new dataset id |
| Optional AI research (paid) | Anthropic API account and key | `pip install -r requirements-ai.txt`; `ANTHROPIC_API_KEY`; `ASTRA_AI_PAID_CALLS=enabled`; `ASTRA_AI_PROVIDER=anthropic`; keep caps; then `astra research-ai --candidate ID` or `process-due --with-ai`. Requires the user's explicit authorization. |
| External notifications | none configured | requires a channel decision and authorization |

## 9. Optional costs

| Item | Cost |
|---|---|
| SEC EDGAR APIs | **$0** — "These APIs do not require any authentication or API keys" (sec.gov EDGAR APIs page, read 2026-09-30); fair-access limit 10 req/s |
| This prototype, sample demo, replay, local dashboard | **$0** |
| Anthropic API (optional) | Listed price for `claude-opus-5-5`: **$4 / $20 per million input/output tokens** (Claude API model table bundled with Claude Code, cached 2026-09-25 — confirm on Anthropic's pricing page before activation). Per-call cost **UNKNOWN**; at default caps (≤16,000 output tokens, ≤60,000 input characters) a rough ceiling is ≈ $0.40 per call and ≈ $2/day at 5 calls/day (estimate assumes ~4 characters per token; the server-side refusal fallback, if triggered, bills the fallback model at its own rates) |
| Live market data provider | **UNKNOWN** (not selected) |
| Hosting / scheduler | **UNKNOWN**; not authorized (STATE: user chose ChatGPT-only, skip hosting) |

## 10. Prioritized next tasks for Codex

Order follows ASTRA's review: integrity fixes first (done in round 1 — re-verify them),
then detector parity, then a prospective comparison against real retained alerts.

1. **Re-verify round-1 corrections** on the Mac: clone this branch into
   `/Users/jonathanpratama/Documents/Codex/astra-scanner`, run setup, tests and demo, and rerun
   the reviewer's own reproduction scripts against it.
2. **Detector parity (COMPRESSION first)**: run the original `trading/pilot/runner.py` and ASTRA
   on identical 5-minute inputs, including plateau-low cases. Diff statuses and features, adopt the
   original rule where it differs (engine version bump; earlier ASTRA runs stay labelled), and
   settle the listed interpretations.
3. **Prospective comparison against real alerts**: export the existing hourly radar outputs to the
   alert CSV with their declared windows, and compare only over common prospective coverage.
   This needs live data (task 6).
4. **SEC live check**: with the user's chosen contact in `ASTRA_SEC_USER_AGENT`, run
   `sec-collect` twice on 2–3 issuers in a live DB; confirm baseline backlog then no duplicates;
   record the actual output.
5. **Records reconciliation**: decide whether ASTRA emits `trading/records/` coverage/decision
   records via `record_control.py`; implement the exporter if so.
6. **Live market data**: choose a provider (cost decision by the user), implement
   `MarketProvider` with observed retrieval times, consolidated volume and RTH 5-minute bars.
7. **Corporate actions + point-in-time metadata** (split handling, instrument history as-of).
8. **Alert digest** per ops r1 (0–1 urgent, ≤3 candidates) and a delivery-channel proposal
   (needs authorization).
9. **Calendar**: extend beyond 2027 from an authoritative source; resolve the July 2026 footnote.
10. **AI adapter** (only if the user authorizes): provider choice (an OpenAI/ChatGPT adapter can
    implement the same interface), structured-output hardening, a capped live test.

## 11. File map and deviations from the brief

```
astra-scanner/
  HANDOFF.md, IMPLEMENTATION_STATUS.json, README.md
  pyproject.toml, requirements.txt, requirements-ai.txt, .env.example, .gitignore
  astra/
    core.py          clock, IDs, hashing, time parsing
    db.py            connection, migrations, transactions
    migrations/0001_initial.sql   schema + append-only triggers
    calendar.py      NYSE sessions 2019-2027
    config.py        settings from environment
    runs.py          run ledger
    market.py        dataset/instrument/universe/bar import, point-in-time store, provider interface
    detectors.py     COMPRESSION, IGNITION (frozen v1), EQ_GAP
    scan.py          scan runs, coverage, evidence, anomaly candidates, sampled non-signals
    announcements.py local JSON import, classification, screen, announcement candidates
    sec.py           SEC submissions adapter
    evidence.py      freeze + read-back
    candidates.py    state machine, ownership
    research.py      research schema + import, blockers
    work.py          leased work queue
    processing.py    process-due / cycle
    notifications.py preview queue, r1.2 decision rows
    outcomes.py      frozen outcome definition + measurement
    replay.py        historical replay
    exports.py       CSV exports, hourly-alert comparison (per source window)
    ai.py            optional gated AI researcher
    status.py        shared status summary
    demo.py          sample workflow
    cli.py, __main__.py
    web/app.py, web/templates/*.html   read-only dashboard
    config/          detectors-v1.json (frozen), astra-eq-experimental-1.json, screen-1.json
  config/sec_issuers.example.json
  fixtures/generate_sample.py, fixtures/sample/* (synthetic), fixtures/sec/* (real trimmed payload + provenance)
  tests/             85 tests (tests/test_review_round1.py: review reproductions)
  docs/              IMPORT_FORMATS, DETECTORS, STATE_MACHINE, OUTCOMES, CALENDAR, SCHEMA, VERIFICATION,
                     TEST_RESULTS.txt, DEMO_TRANSCRIPT.txt, DEMO_STATUS.txt
  doctrine/          DOCTRINE_REFERENCE.md, frozen/{detectors-v1.json, v2-rules.json, SOURCE_MANIFEST.json}
  scripts/           verify_doctrine.py, package_transfer.py
```

Deviations:

- **Location**: `/Users/jonathanpratama/...` does not exist in the build container, so the app
  was built in `astra-scanner/` inside the `Moonshot-Scanner` repository (branch
  `claude/loving-gauss-1o1ldm`). Task 1 above moves it to the requested path.
- **Doctrine snapshots**: only the frozen configurations and the source manifest are
  committed; the prose doctrine files contain private account data and this repository is
  public. `scripts/verify_doctrine.py` re-verifies a local bundle.
- **Bar interval**: detectors and outcomes use 5-minute RTH bars because the frozen v1
  definitions require them; daily bars can be imported but no detector uses them.
- **Holdings/simulated positions**: kept distinct by not modelling them at all.
- **Dashboard** is read-only; operator actions (transitions, ownership, acknowledgements) are CLI commands.
- **AI adapter** implements one provider (Anthropic, official SDK); provider choice remains open.
- **SEC live exercise** stopped at the identification gate (no contact configured).

## 12. Git commit / status

- Repository: `jonathanpratama-boop/Moonshot-Scanner` (public), branch `claude/loving-gauss-1o1ldm`.
- Application commit: `72a4d0261327eed5ac2e96780f29e0a20fa82029` ("Add ASTRA local US-equities research scanner prototype"),
  pushed 2026-09-30. The commit containing this line only records that hash.
- Working tree at that commit: clean (untracked `var/`, `.venv/`, `dist/` are git-ignored).
- No pull request has been opened.
- Transfer package: `python scripts/package_transfer.py` →
  `dist/astra-scanner-transfer-<date>.zip` (tracked files + `PACKAGE_MANIFEST.json` with SHA-256 per file).
