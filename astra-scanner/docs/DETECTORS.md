# Detectors (experimental, unvalidated)

Every detector returns exactly one status per member per run:
`SIGNAL`, `NO_SIGNAL`, `INSUFFICIENT_DATA`, `UNAVAILABLE` (a required input or metadata is
not available), or `FAILED` (the calculation crashed). Only `SIGNAL` and `NO_SIGNAL` count as
an evaluation. Missing, stale, forming or conflicting data is never turned into `NO_SIGNAL`.
None of them grants entry permission; a signal creates a research candidate (route
`anomaly`, mechanism hint `TAPE`) that needs no catalyst.

## Versions and provenance

| Detector | Rule version | Parameters | Engine |
|---|---|---|---|
| COMPRESSION | `EXPERIMENTAL-DETECTORS-1` | frozen `astra/config/detectors-v1.json`, SHA-256 `09297ed3…d21ddd` (byte-identical copy of `trading/pilot/detectors-v1.json`) | `astra-eq-engine-0.1.0` |
| IGNITION | `EXPERIMENTAL-DETECTORS-1` | same frozen file | `astra-eq-engine-0.1.0` |
| EQ_GAP | `ASTRA-EQ-EXPERIMENTAL-1` (new, 2026-09-30) | `astra/config/astra-eq-experimental-1.json` | `astra-eq-engine-0.1.0` |

The v1 file's hash is checked on every load; if it differs, v1 detectors refuse to run.
Each run freezes the parameter sets in `detector_configs` (config id =
`DETECTOR@RULE_VERSION+ENGINE`); a changed parameter set under the same id is refused.

**The engine is an independent reimplementation** of the v1 rules from
`trading/pilot/README.md`; the original `runner.py` was not in the doctrine bundle, so
equivalence is **unverified**. Runs from this app are therefore excluded from the
authoritative v1 pilot cohort (`trading/pilot/runs/`). Interpretations that need review
against the original engine are listed in `astra-eq-experimental-1.json`
(`interpretations_needing_review`).

## Shared input rules (all detectors)

- **Dataset**: 300-second bars, `session_timezone = America/New_York`, calendar
  `XNYS-RTH-RULES-2019-2027-v1`, declared (not `unknown`) price adjustment. Otherwise every
  detector is `UNAVAILABLE` with the reason.
- **Session**: regular trading hours only (09:30–16:00 ET; 13:00 on early-close days).
  `PRE`/`POST` bars never enter windows or baselines. Exchange time zone and DST come from
  the IANA database; the expected bar grid comes from the calendar, not from the data.
- **Completed-bar requirement**: a bar is usable only if the source marked it complete,
  its quality is `OK` (not `DATA_CONFLICT`), and both its end and its availability time are
  at or before the scan cutoff. Forming bars are kept for audit but excluded.
- **Freshness**: the latest usable RTH bar must have ended within 900 s of the cutoff
  (v1 `maximum_bar_age_seconds`). Otherwise `INSUFFICIENT_DATA:stale_latest_bar` — with the
  suffix `_market_not_in_rth` when the cutoff is outside the regular session (e.g. an
  evening scan). This makes an after-hours scan `NOT_SCANNED`, not "no signal".
- **Benchmark**: the instrument's declared `benchmark_symbol` from the same dataset. Every
  timestamp used must have a usable benchmark bar, otherwise `INSUFFICIENT_DATA` (missing
  benchmark bars) or `UNAVAILABLE` (no benchmark declared).
- **Currency**: instrument currency must equal the dataset currency.
- **Scan cutoff**: live = current clock; sample = the dataset's latest batch `as_of`
  (unless `--cutoff` is given); replay = explicit simulated cutoff.

## COMPRESSION (v1 parameters)

- Window: the last 48 expected RTH bars ending at the latest usable bar. The window may
  cross one declared session break only at exact session boundaries (last slot → first slot
  of the next session). Any missing/forming/conflicting bar in the window →
  `INSUFFICIENT_DATA` with the kind (`missing`, `data_conflict`, `forming`).
- Ceiling = max high of the preceding 47 bars. At least two highs within 0.75% of the
  ceiling whose bar indices differ by ≥ 3.
- Pivot low: strictly lower than each of the 2 bars on either side (equal lows are not
  pivots); confirmation time = availability of the right-hand confirmation bars. The last
  two pivot lows must rise by ≥ 0.3%.
- Second-half (bars 24–47) high–low range ≤ 80% of first-half range (zero first-half range →
  `INSUFFICIENT_DATA`).
- Newest close between 2% below and 0.5% above the ceiling.
- Window return (first open → last close) minus benchmark window return ≥ 0.0 pp.

## IGNITION (v1 parameters)

- Latest three contiguous usable RTH bars in the same session (15 minutes).
- Return first open → final close ≥ +3%; minus the benchmark's return over the same bars
  ≥ +1.5 pp. An opening gap alone cannot pass (the return starts at the first bar's open).
- Volume: the three bars' summed volume ≥ 3× the median of the same three clock slots over
  the most recent 10–20 prior sessions in which all three slots are usable (walking back at
  most 40 sessions). Units = the dataset's declared volume unit/scope; one dataset never
  mixes scopes. Fewer than 10 windows or a zero median → `INSUFFICIENT_DATA`. Unknown
  volume scope/unit → `UNAVAILABLE`.
- No pullback or second leg is required.

## EQ_GAP (new, `ASTRA-EQ-EXPERIMENTAL-1`)

Covers the "gap" lane that IGNITION deliberately excludes.

- Gap = open of the first RTH bar of the latest usable bar's session ÷ close of the final
  RTH bar of the previous session − 1 ≥ +5%; minus the benchmark's gap ≥ +3 pp.
- First-bar volume ≥ 3× the median first-slot volume over 10–20 prior sessions.
- Upside only. Premarket prints are excluded. Same freshness rule.
- Thresholds were chosen before any outcome was observed and are unvalidated.

## Known divergence: COMPRESSION plateau lows

ASTRA's review (2026-09-30) found that this COMPRESSION implementation differs from the original
v1 detector on plateau lows. Here a pivot low must be **strictly** lower than the two bars on each
side, so equal lows are never pivots. The original `trading/pilot/runner.py` is not in the doctrine
bundle, so its exact rule cannot be reproduced without guessing. Status: `KNOWN_DIVERGENCE`,
parity pending the original code and a side-by-side run on identical inputs.

## Cohort eligibility

- Sample runs: `EXCLUDED_SYNTHETIC`.
- Replay runs: `EXCLUDED_RETROSPECTIVE`.
- A live-database scan is `PROSPECTIVE_ASTRA_EQ` only if all of these hold:
  - the dataset is `OBSERVED_RETRIEVAL`;
  - its latest bar batch was classified `OBSERVED_MARKET` (observed retrieval, at most 300 s old
    at import);
  - the data cutoff is at most 300 s before the scan (frozen v1
    `maximum_prospective_cutoff_age_seconds`).
- Otherwise it is `EXCLUDED_NOT_PROSPECTIVE: <reasons>` and the run is classified
  `RETROSPECTIVE`. Importing history into a live database never makes it prospective.

## Run records and coverage

Each scan run stores: declared universe membership + hash, benchmarks, provider and
dataset, requested cutoff and data cutoff, detector config versions and hash, cohort
eligibility (see above; prospective runs are separate from the authoritative v1 pilot cohort),
and one
`detector_results` row per member × detector.

Per member: `EVALUATED` (all detectors SIGNAL/NO_SIGNAL), `PARTIAL`, `NOT_EVALUATED`, or
`FAILED`. Per run: `SCANNED` (all members evaluated), `PARTIAL`, or `NOT_SCANNED`. Headlines:

- `NO SIGNAL IN COMPLETED COVERAGE` — only when every declared member was evaluated.
- `COVERAGE INCOMPLETE: n of N members not fully evaluated; absence of signals for them is
  not a negative market result`.
- `NOT_SCANNED: no member could be evaluated`.

Incomplete coverage queues one `coverage_incomplete` notification per distinct gap set per
session (not per run).

## Evidence

A signal freezes, before any candidate or research exists: the exact input bars (values,
revision numbers, content hashes, availability times), benchmark bars, baseline windows,
dataset metadata, instrument metadata (flagged as current, not point-in-time), config id and
parameter hash, cutoffs, calendar id, features, and the `SIGNAL_BAR_REFERENCE` (the last
completed close; `scoring_baseline_eligible: false`; `detection_reference: null`). The
snapshot is hashed, stored in an append-only table, read back and verified in a separate
`evidence_readbacks` row. Identical inputs reuse the same snapshot.

Later detections on an open anomaly candidate attach as `repeat_detection` evidence (one
notification only when a *different* detector fires). Multiple detectors firing in the same
scan attach as `co_detection`. Sampled non-signals (default 3 per run, deterministic by run
id, at least one hour apart per symbol) get outcome subjects for a detector-only baseline.
