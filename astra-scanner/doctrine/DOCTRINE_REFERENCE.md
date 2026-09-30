# Doctrine reference (GD-1.1 + ASTRA-OPS-20260929-r1.2)

## Source and integrity

- Bundle: `ASTRA-doctrine-sources-2026-09-30.zip`, uploaded by the user during the build on
  2026-09-30 (zip SHA-256 `7300519abf37cd4df84edfbec1a9b535145f45b7d79dc50223b14ba26d9b7f6f`).
  Its `MANIFEST.json` (copied here as `frozen/SOURCE_MANIFEST.json`) records the source root
  `/Users/jonathanpratama/.codex/.chatgpt-projects/g-p-6a9fd0f810148191b107e45a07720dd3` and
  `copied_at_utc 2026-09-30T10:28:14Z`.
- All 10 files matched their manifest SHA-256 values when checked in the build container.
- The original project directory was **not** accessible from the build environment; only
  these bundle copies were read. Referenced code (`trading/record_control.py`,
  `trading/pilot/runner.py`, `trading/live/*.py`), templates, historical records and
  deployment receipts were not in the bundle and were **not inspected**.

Files read in full: `READ_FIRST.md`, `MANIFEST.json`, `AGENTS.override.md`,
`TRADING_DOCTRINE.md` (GD-1.1), `trading/astra-operations-20260929.txt` (r1 + r1.2 addendum),
`trading/event-readiness-GD-1.1.txt`, `trading/STATE.md`, `trading/records/README.md`,
`trading/pilot/README.md`, `trading/pilot/detectors-v1.json`, `trading/live/README.md`,
`trading/live/v2-rules.json` (`ALL_DOCTRINE_SOURCES.md` was confirmed to be only
`READ_FIRST.md` plus these ten files).

## What is retained in this repository and why

| Retained | Why |
|---|---|
| `frozen/detectors-v1.json` (byte-identical, SHA-256 `09297ed3…a21ddd`) and `astra/config/detectors-v1.json` (same bytes, hash-checked at runtime) | frozen v1 parameters reused by COMPRESSION/IGNITION |
| `frozen/v2-rules.json` (byte-identical, SHA-256 `82dcb1f4…8cacc78`) | reference only; crypto-specific, not ported |
| `frozen/SOURCE_MANIFEST.json` | source versions and hashes for later verification |

**Not retained**: the prose doctrine files. This repository is public and those files contain
private account and deployment details (holdings history, fills, message/task identifiers,
local paths). Keep them in the original project; to re-verify against this build, place the
bundle folder under `doctrine/local/` (git-ignored) and run
`python scripts/verify_doctrine.py doctrine/local/ASTRA-doctrine-sources-2026-09-30`.

## Rules adopted in this prototype (with source)

| Rule | Source | Where implemented |
|---|---|---|
| Research lifecycle separate from positions/actions; rank/research is never entry permission | GD-1.1 "Keep research, position and action separate" | `candidates.py`; no position tables |
| DETECTED/RESEARCHED/REJECTED/EXPIRED; CONDITIONAL_READY & ENTRY_ELIGIBLE not granted by model output | GD-1.1, ops r1 | reserved states blocked in code + triggers |
| Promotion/demotion needs timestamped evidence delta and remaining blockers | GD-1.1 | `state_transitions` columns |
| Revision reason types NEW_EVENT … CHANGED_INFERENCE; originals preserved | GD-1.1 "Freeze, revise and score", ops r1 | research revisions, append-only tables |
| Event-first and anomaly-first lanes; a material event can enter research without a price threshold; TAPE needs no news | GD-1.1, ops r1, live README | two independent routes |
| Initial feed backlog is not newly occurring news; newly learned ≠ newly occurring | GD-1.1, live README | `BASELINE_BACKLOG`, `NEWLY_FOUND_OLD_INFORMATION` |
| Declared membership, feed, completed-bar cutoff, gaps per run; a failed request is not a negative result; "NO … IN COMPLETED COVERAGE" vs "COVERAGE INCOMPLETE"; SCANNED/PARTIAL/NOT_SCANNED | GD-1.1 "Prove discovery coverage", ops r1 | `runs`, scan coverage |
| Retain all signals, sampled non-signals (≥1 h apart) and missing-data results | GD-1.1, pilot README, live README | `detector_results`, sampled subjects |
| Frozen v1 COMPRESSION/IGNITION thresholds; changes need new versions/cohorts | pilot README, `detectors-v1.json` | hash-checked config; EQ_GAP as a new version |
| Completed vs forming bars; availability ≤ cutoff; latest bar ≤ 15 min old; matched same-slot volume baseline 10–20 sessions; no mixed session classes/venues/scopes | pilot README | `detectors.py`, dataset model |
| Missing inputs block only dependent claims; missing = UNKNOWN, never zero/PASS | GD-1.1, ops r1 | research sections, detector statuses |
| DATA_CONFLICT quarantines affected conclusions | event-readiness §2 | bar validation |
| Separate publication, public availability, observation, feature availability, detection, research completion, delivery times; never borrow timestamps | GD-1.1, ops r1.2 | timestamp columns; software clock only |
| Reference roles PRE_EVENT / DETECTION / DELIVERED / EXECUTABLE / FILL; SIGNAL_BAR_REFERENCE not a scoring baseline | GD-1.1, records README, pilot README | outcome reference roles |
| Missing outcomes are UNKNOWN; unknown ordering CENSORED; MFE/MAE are not profits | GD-1.1, ops r1 | outcome statuses; excursions not measured |
| Separate discovery, forecast/path and confirmed-trade scoreboards | pilot README | separate tables/exports; no trades |
| Candidate record fields (owner, source/availability/observation times, reference role, blockers + clearing evidence, recheck/condition/expiry with zone) | ops r1 RECORDS | candidates + decision row |
| Alert completeness decision row, UNKNOWN + reason never omitted, repeated alert needs new evidence/state | ops r1.2 | `notifications.decision_row`, dedup keys |
| Show exchange time and Asia/Jakarta | event-readiness §1 | display zones UTC/ET/WIB |
| Run priority: urgent risk/due catalysts → reserved disclosure AND anomaly sweep → routine rechecks | ops r1 RUN PRIORITY | `processing.process_due` / `astra cycle` |
| Low nominal share price is not small equity value; need price × verified economic shares | ops r1.2 | `valuation_scale` validation |
| Source content is untrusted data, never instructions | live README | escaping, schema, AI prompt delimiters |
| Maximum planned price-stop distance 5% (not a loss budget or guarantee) | GD-1.1, ops r1 | recorded here only; no plan logic exists |
| Local code is not a hosted service; never claim continuous monitoring | GD-1.1, STATE | status/dashboard wording |

## Crypto assumptions reconsidered for US equities

| Crypto pilot/live assumption | Equity treatment here |
|---|---|
| 24/7 UTC sessions, elapsed-hour horizons (1h/4h/24h/120h) | exchange sessions from the NYSE calendar, RTH only; horizons in sessions (+1/+3/+5) |
| BTC fixed benchmark | per-instrument declared benchmark from the same dataset |
| Binance spot venue volume; CoinGecko turnover | dataset-declared volume scope (consolidated or primary venue) and unit; unknown → dependent detectors UNAVAILABLE |
| Reception-time venue mid quotes as outcome endpoints | final RTH 5-minute bar closes; no quote feed; no executable claims |
| Governance-feed event inbox | SEC filings (filings coverage only) + retained local announcements |
| v2 EVENT_IGNITION / RS_SHOCK / SECOND_LEG | **not ported** — crypto-specific; an equity version would need its own new rule version and cohort |

## Unresolved conflicts / reconciliation outstanding

1. **v1 engine equivalence.** COMPRESSION/IGNITION are reimplemented from the README; the
   original `runner.py` was unavailable. Interpretations (touch separation, strict pivots,
   baseline walk-back, session-break handling) are listed in
   `astra/config/astra-eq-experimental-1.json` and need comparison against the original
   engine on identical inputs. Until then, ASTRA runs are excluded from the authoritative v1
   cohort.
2. **Record ledger.** GD-1.1 names file-based append-only records under `trading/records/`
   checked by `record_control.py`. This prototype keeps its own append-only SQLite ledger and
   does not write `trading/records/` files. Whether ASTRA's database becomes canonical or must
   export into the records format is undecided (templates/validator not in the bundle).
3. **BLOCKED semantics.** The brief lists BLOCKED as a research state; the event-readiness
   supplement uses BLOCKED for a *plan* status (vs CONDITIONAL_READY). Here BLOCKED means
   "research cannot conclude until named evidence arrives". Plan states remain reserved.
4. **Deployment scope.** STATE records a user requirement for Mac-off monitoring and a later
   final direction to use ChatGPT only and skip external hosting. This build is local-only,
   runs only when invoked and creates no scheduler, monitor or hosting; any off-device
   operation needs a new explicit authorization.
5. **Alert budget.** Ops r1 prefers 0–1 urgent alerts and at most 3 candidates per alert.
   This prototype queues one preview per material event (deduplicated) and does not yet
   build a capped digest.
6. **Cadence.** Saturday scoring / Sunday broad discovery and pre-session refresh are
   operating procedures, not implemented schedules.
7. **Calendar footnote conflict** for Independence Day 2026 (see `docs/CALENDAR.md`).
8. **Historical STATE claims** (deployments, holdings, test counts) were not re-audited; they
   are dated context, not current account or deployment evidence.
