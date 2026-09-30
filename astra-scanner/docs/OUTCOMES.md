# Forward outcomes and comparison exports

## Frozen definition `ASTRA-OUTCOME-EQ-1`

The full specification is the `SPEC` dict in `astra/outcomes.py`; it is written to
`outcome_definitions` (with its hash) on first use. If the code's spec changes under the
same id, measurement refuses to run — create a new definition id instead.

- **Purpose**: exploratory forward path labels for discovery evaluation. Not detection,
  delivered, executable or fill prices; not trading returns (`scoring_baseline_eligible: false`).
- **Reference roles** (kept separate, doctrine vocabulary):
  - `SIGNAL_BAR_REFERENCE` — anomaly candidates and sampled non-signals: close of the latest
    usable RTH bar at the scan cutoff.
  - `PRE_EVENT_REFERENCE` — announcement candidates: close of the latest usable RTH bar
    ending at/before the source publication time. Measures event repricing only.
  - `OBSERVATION_BAR_REFERENCE` — announcement candidates: close of the latest usable RTH bar
    that ASTRA had actually ingested and that was available at its first observation (the
    simulated cutoff for replay).
- **Horizons**: +1, +3, +5 US sessions after the reference bar's session.
- **Endpoint**: close of the final regular-session 5-minute bar of that session (15:55 ET
  slot; 12:55 on early-close days). This may differ from the official closing auction price.
- **Return**: endpoint close / reference close − 1. **Benchmark**: the instrument's
  benchmark at identical reference and endpoint timestamps; otherwise the excess return is
  UNKNOWN while the raw return may still be measured.
- **Calendar**: `XNYS-RTH-RULES-2019-2027-v1` (weekends, NYSE holidays with observance rules,
  special closures, 13:00 early closes). Outside its range → `UNKNOWN(calendar_unavailable)`.
- **Extended hours**: excluded. Overnight and pre/post-market moves appear only through the
  next RTH close.
- **Missing bars**: never substituted by a neighbouring bar.
- **Excursions**: `NOT_MEASURED`. No MFE/MAE, no maximum highs and no target-before-stop
  ordering are derived from bar highs/lows. Later highs are not captured profits.

## Statuses

| Status | Rule |
|---|---|
| `PENDING_DATA` | endpoint session close is after the dataset's current data cutoff |
| `MEASURED` | endpoint bar exists, complete and `OK`; final, never rewritten |
| `UNKNOWN` | endpoint is due and later bars exist, but the endpoint bar is missing, forming or `DATA_CONFLICT`; or the reference is unavailable |
| `CENSORED` | endpoint is due but the symbol has no usable bar after the endpoint session (coverage ended) |

Observations are append-only; a new row is added only when status or reason changes.
`UNKNOWN`/`CENSORED` can later be superseded by an appended `MEASURED` row if late data
arrives. Rejected candidates and sampled non-signals are measured like any other subject.

## Separate scoreboards

- **Discovery and coverage**: `runs`, `detector_results`, `export runs`, `export detector-results`.
- **Research judgments**: `research_records`, `state_transitions`, `export candidates`.
- **Forward price outcomes**: `outcome_subjects`/`outcome_observations`, `export outcomes`.
- **Simulated trading**: none exists in this prototype.

## Comparison with hourly alerts and a detector-only baseline

`astra import-hourly-alerts` retains alert rows plus the declared coverage window and
universe. `astra export comparison --out comparison.csv` writes one row per
(session, symbol) inside **common coverage** — pairs with at least one fully evaluated
scanner run inside the alert window/universe — with:

- scanner scans and full evaluations,
- `detector_only_signal` (any SIGNAL, before research) and its first time and time basis
  (actual run completion for live; data/simulated cutoff for sample/replay),
- ASTRA candidates (route and current state),
- hourly alert presence, first alert time and basis, delivery time (UNKNOWN unless supplied).

`comparison.csv.summary.json` lists counts, every alert excluded from common coverage with
the reason, and the caveats. Counts are descriptive; they do not establish accuracy, lead
time or trading performance.
