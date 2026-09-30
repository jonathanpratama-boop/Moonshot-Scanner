# Verification record (what was actually executed and observed)

Build environment: Linux cloud container (Claude Code on the web), Python 3.11.15, SQLite
bundled with CPython, dependency versions as pinned in `requirements.txt`; optional
`anthropic==1.9.0` installed for one SDK-level test. Clock: system UTC. Date: 2026-09-30.
**Not executed on macOS**; the target machine path `/Users/jonathanpratama/...` did not exist here.

## 1. Doctrine sources

- The original project path was not accessible. The user uploaded
  `ASTRA-doctrine-sources-2026-09-30.zip` mid-build; all 10 files matched `MANIFEST.json`.
- `python scripts/verify_doctrine.py <bundle>` → `ALL MATCH` (10 manifest files, both frozen
  config copies, runtime v1 config).

## 2. Automated tests

- Main environment: `python -m pytest -v` → **68 passed** in ~64 s. Full output:
  `docs/TEST_RESULTS.txt`.
- Clean reproducibility check: fresh copy of the project, `python3 -m venv .venv`,
  `pip install -r requirements.txt`, `pip install -e . --no-deps`, `pytest -q` →
  **67 passed, 1 skipped** (the real-SDK test skips without the optional `anthropic` extra),
  1 warning (`StarletteDeprecationWarning`: TestClient prefers `httpx2`; test-only).

Coverage of the required checks:

| Required check | Tests |
|---|---|
| Duplicate announcements and revisions | `test_announcements.py` (repeat retrieval ×3 → no new candidates/notifications; revision 2 appended, revision 1 kept; append-only triggers) |
| Backlog vs new | `test_backlog_then_new_classification_and_candidates`, SEC baseline/new tests |
| Missing, stale, forming bars; conflicts | `test_market_detectors.py` (missing → INSUFFICIENT, DATA_CONFLICT, stale/halted, after-hours NOT_SCANNED, forming excluded, bar validation rules, future `as_of`) |
| Metadata gaps | unknown volume scope → volume detectors UNAVAILABLE only; unknown adjustment → all UNAVAILABLE; dataset mixing refused |
| Chronology and replay cutoffs | `test_chronology_replay.py` (point-in-time store hides later bars/revisions; replay evidence inputs all available by cutoff; announcement candidates not before publication; ignition not before its bars; research cannot predate detection; replay research must be retrospective) |
| Invalid transitions | `test_state_research.py` (map enforced, reserved states blocked in code and DB, reason/evidence required, RESEARCHED blocked while blockers open, blockers need clearing evidence) |
| Interrupted jobs / restart recovery | `test_interrupted_lease_is_recovered_once`, cross-connection claim exclusivity, retry → dead-letter, lease lost |
| Duplicate notification prevention | dedup keys, coverage notice once per condition, re-delivery sends nothing |
| Missing/censored outcomes | `test_outcomes.py` (PENDING → MEASURED, UNKNOWN missing endpoint, CENSORED coverage ended, MEASURED final, definition frozen, no excursion fields) |
| Paid integrations disabled | `test_ai_gate.py` (API key alone → gate refuses, client never constructed; fake responses verify request limits, output validation, refusal/truncation handling, daily cap, input cap; real SDK + mock transport verifies request shape and one 429 retry). **No paid call was made.** |
| SEC adapter | `test_sec.py` with the real trimmed EDGAR payload through a mock transport |
| Dashboard | `test_dashboard_cli.py` (pages render, escaping, NOT SCANNED vs NO SIGNAL IN COMPLETED COVERAGE vs COVERAGE INCOMPLETE, localhost-only serve) |
| Full workflow | `test_full_demo_workflow` |

## 3. Demonstration workflow

`astra demo --reset` (final run 2026-09-30T11:30Z, after the last code change) → 26 internal checks `ok`, transcript
in `docs/DEMO_TRANSCRIPT.txt`, status in `docs/DEMO_STATUS.txt`:

sample import (9,501 stage-1 bars: 1 DATA_CONFLICT, 7 forming) → announcements (3 backlog;
then 1 NEW_PUBLICATION, 1 NEWLY_FOUND_OLD_INFORMATION, 1 time-unknown, 1 revision,
1 duplicate sighting; re-import adds nothing) → detection (IGNITION ZSMPA, COMPRESSION
ZSMPB, EQ_GAP ZSMPG; coverage PARTIAL because of ZSMPD/ZSMPE) → frozen evidence read back
and proven immutable → sample research (ZSMPA BLOCKED, ZSMPF RESEARCHED, ZSMPG REJECTED;
invalid transition, ENTRY_ELIGIBLE and disabled AI refused) → dashboard render check →
due urgent recheck processed → revision clears both blockers (BLOCKED → RESEARCHED) →
stage-2 bars (7 forming bars revised to complete) → outcomes 23 MEASURED, 2 UNKNOWN,
2 CENSORED → 11 notifications written to the local outbox, external `not_configured`, one
acknowledged → comparison export (5 common-coverage rows; 2 alerts excluded with reasons).

## 4. Dashboard (live server)

`astra --db var/demo.db serve --port 8765`: `/proc/net/tcp` showed the listener on
`127.0.0.1:8765` only. `/`, `/candidates`, `/announcements`, `/runs`, `/outcomes`,
`/notifications`, `/api/status` returned HTTP 200. Headless Chromium screenshots of the
overview and a candidate page were inspected (SAMPLE DATA banner, PARTIAL coverage banner,
actual vs data-cutoff times, UTC/ET/WIB, full transition chronology, evidence read-back).
One defect found that way (a literal `None` cell) was fixed; afterwards 26 pages (all
candidate and run pages) returned 200 with no leaked `None`.

## 5. Replay (CLI)

Replay-mode DB, both bar stages and both announcement files, then
`astra replay --start 2026-09-15 --end 2026-09-18 --every-minutes 30` → 52 simulated cutoffs,
25 signals, 6 replay candidates (incl. one announcement candidate), 1,092 detector result
rows (52 × 7 × 3, all statuses retained); `astra outcomes` → 117 subjects (285 MEASURED,
27 PENDING_DATA, 16 UNKNOWN, 23 CENSORED horizon observations). Wall time ≈ 2 s.
Some replay signals come from random-walk noise in the synthetic data (e.g. COMPRESSION on
ZSMPA at 10:30 ET) — retained, as the doctrine requires.

## 6. SEC EDGAR

- Connectivity: one manual request to `https://data.sec.gov/submissions/CIK0000320193.json`
  at 2026-09-30T10:26:06Z returned HTTP 200 (164,825 bytes). That request's User-Agent
  identified the tool but had **no contact email**, so it did not fully follow SEC's
  declared-UA format. It supplied the trimmed test fixture (`fixtures/sec/README.md`).
- Empirical check on that payload: 1,006 recent filings had acceptance hours 10:00–02:00 UTC
  (EDGAR's 06:00–22:00 ET window) and after-17:30-ET acceptances carried the next filing
  date → `acceptanceDateTime` is treated as genuine UTC.
- Adapter exercise through the CLI in a live-mode DB with the current configuration:
  `astra sec-collect --issuers config/sec_issuers.example.json` →
  `ERROR: SEC_USER_AGENT_NOT_CONFIGURED … No request was sent.` (exit 2). The run is recorded
  as failed/NOT_SCANNED and shown by `astra status`.
- **Not executed**: a live adapter collection with a compliant User-Agent, because no
  contact identity is configured (see HANDOFF §8). The adapter's request/parse/dedup/retry
  paths are verified only against the real payload through a mock transport.

## 7. Not executed / not established

Live market data collection (no provider exists), live AI research (no paid call made),
external notification delivery, continuous or scheduled operation, macOS installation,
performance at realistic universe sizes, predictive value of any detector.
