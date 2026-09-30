# Forward outcomes and comparison exports

## Frozen definition `ASTRA-OUTCOME-EQ-2`

The full specification is the `SPEC` dict in `astra/outcomes.py`. It is written to
`outcome_definitions` (with its hash) on first use. If the code's spec changes under the same
id, measurement refuses to run, so a change needs a new definition id.

EQ-2 replaced `ASTRA-OUTCOME-EQ-1` on 2026-09-30 after review round 1 (`docs/REVIEW_ROUND1.md`).
EQ-1 did not record reference times, so it could accept stale references and later corrections.
EQ-1 subjects in older databases are kept as recorded and are no longer evaluated.

- **Purpose**: exploratory forward path labels for discovery evaluation. These are not
  detection, delivered, executable or fill prices, and not trading returns
  (`scoring_baseline_eligible: false`).
- **Reference roles and reference times** (kept separate, doctrine vocabulary):
  - `SIGNAL_BAR_REFERENCE` — anomaly candidates and sampled non-signals. Reference time is the
    scan cutoff: actual time for live scans, data or simulated cutoff for sample and replay.
  - `PRE_EVENT_REFERENCE` — announcement candidates. Reference time is the source publication
    time; measures event repricing only. Unavailable for `MATERIAL_REVISION` candidates, because
    the source dates only the original item, not the revision.
  - `OBSERVATION_BAR_REFERENCE` — announcement candidates. Reference time is ASTRA's actual first
    observation (the simulated cutoff in replay). The bar must also have been ingested by then,
    except in replay.
- **Point in time**: instrument and benchmark reference prices come from the latest bar revision
  available at the reference time. Their revision numbers are stored. Later corrections never
  change a frozen reference.
- **Freshness**: the reference bar must end no more than 900 s (frozen v1
  `maximum_bar_age_seconds`) before the latest RTH bar end expected at the reference time. In
  session, that is the last completed 5-minute slot; outside RTH, it is the last session close.
  Otherwise the reference is `UNAVAILABLE` with the reason, and every horizon is `UNKNOWN`.
- **Horizons**: +1, +3, +5 US sessions after the reference bar's session.
- **Endpoint**: close of the final regular-session 5-minute bar of that session (15:55 ET slot;
  12:55 on early-close days). It uses the latest bar revision known when measured, and the
  revision number is stored. This may differ from the official closing auction price.
- **Endpoint after reference**: an endpoint whose session close is not after the reference time
  is `UNKNOWN`, never `MEASURED`.
- **Return**: endpoint close / reference close − 1.
- **Benchmark**: the instrument's benchmark at the identical reference bar (point in time) and at
  the endpoint. If either is unusable, the excess return is UNKNOWN while the raw return may
  still be measured.
- **Calendar**: `XNYS-RTH-RULES-2019-2027-v1`. Outside its range → `UNKNOWN(calendar_unavailable)`.
- **Extended hours**: excluded. Overnight moves appear only through the next RTH close.
- **Missing bars**: never substituted by a neighbouring bar.
- **Excursions**: `NOT_MEASURED`. No MFE/MAE, no maximum highs, no target-before-stop ordering.
  Later highs are not captured profits.

## Statuses

| Status | Rule |
|---|---|
| `PENDING_DATA` | endpoint session close is after the dataset's current data cutoff |
| `MEASURED` | endpoint close is after the reference time, and the endpoint bar exists, complete and `OK`. Final, never rewritten |
| `UNKNOWN` | reference unavailable (missing, stale, no publication time); endpoint not after the reference time; or endpoint due while later bars exist but the endpoint bar is missing, forming or `DATA_CONFLICT` |
| `CENSORED` | endpoint is due but the symbol has no usable bar after the endpoint session (coverage ended) |

Observations are append-only. A row is added only when the status or reason changes.
`UNKNOWN`/`CENSORED` can later be superseded by an appended `MEASURED` row. Rejected candidates
and sampled non-signals are measured like any other subject.

Publication-time and replay references are created only once the dataset has been delivered past
the reference time, so a subject is never frozen as unavailable merely because data had not
arrived yet.

## Separate scoreboards

- **Discovery and coverage**: `runs`, `detector_results`, `export runs`, `export detector-results`.
- **Research judgments**: `research_records`, `state_transitions`, `export candidates`.
- **Forward price outcomes**: `outcome_subjects`/`outcome_observations`, `export outcomes`.
- **Simulated trading**: none exists in this prototype.

## Comparison with hourly alerts and a detector-only baseline

`astra import-hourly-alerts` retains the alert rows plus one declared source window (and an
optional universe) per import. `astra export comparison --out comparison.csv` compares **each
import separately**. It uses:

- only scanner runs whose time lies inside that exact window: actual completion time for live
  runs, data or simulated cutoff for sample and replay;
- only detector signals and ASTRA candidates whose time lies inside the window;
- only that import's alerts.

Rows are (window, session, symbol) pairs with at least one fully evaluated scan inside the window
and universe. Columns:
- source, window and import run id;
- scans and full evaluations inside the window;
- `detector_only_signal` with its first time and time basis;
- ASTRA candidates;
- hourly alert count, first alert time and basis, and delivery time (UNKNOWN unless supplied).

Each alert is attached to at most one row. Otherwise it appears in
`comparison.csv.summary.json` → `excluded_alerts` with the reason: time unknown, outside the
window, outside the universe, or no fully evaluated scan for that symbol and session in the
window. `per_window` reports counts for each window. Windows from different sources are never
summed. All counts are descriptive; they do not establish accuracy, lead time or trading
performance.
