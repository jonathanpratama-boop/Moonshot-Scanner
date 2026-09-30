# Import formats

All files are UTF-8. Timestamps are ISO-8601. A timestamp **with** an offset or `Z` keeps
that offset as its recorded source zone; a timestamp **without** one is only accepted when
the record declares an IANA zone (`published_tz`, `alert_time_tz`, or the dataset's
`session_timezone` for bars) — otherwise it is rejected (bars/alerts) or stored as
`UNPARSEABLE` publication time (announcements). Nothing is guessed. Stored times are UTC,
whole seconds, `YYYY-MM-DDTHH:MM:SSZ`; raw source strings are kept alongside.

Worked examples of every format live in `fixtures/sample/` (all synthetic).

## 1. Provider + dataset definition (`astra.market.dataset.v1`)

```json
{
  "format": "astra.market.dataset.v1",
  "provider": {"provider_id": "sample-synthetic", "name": "…", "is_sample": true, "description": "…"},
  "dataset": {
    "dataset_id": "sample-synthetic-5m-rth",
    "interval_seconds": 300,
    "price_adjustment": "unadjusted | split_adjusted | split_dividend_adjusted | unknown",
    "volume_scope": "consolidated | primary_venue | unknown",
    "volume_unit": "shares | unknown",
    "venue": "free text",
    "session_timezone": "America/New_York",
    "calendar_id": "XNYS-RTH-RULES-2019-2027-v1",
    "currency": "USD",
    "bar_time_convention": "start",
    "availability_basis": "ASSUMED_BAR_END_PLUS_LATENCY | OBSERVED_RETRIEVAL",
    "assumed_latency_seconds": 30
  }
}
```

A dataset fixes every property that must not be mixed inside one calculation (adjustment
basis, volume scope/unit, venue, session zone, calendar, currency, interval). Registering
the same `dataset_id` with different properties is refused; declare a new dataset instead.
All required fields must be present — write `unknown` explicitly. Unknown adjustment
makes every price detector `UNAVAILABLE`; unknown volume scope/unit makes volume-dependent
detectors (`IGNITION`, `EQ_GAP`) `UNAVAILABLE`.

`availability_basis`: imported history uses `ASSUMED_BAR_END_PLUS_LATENCY` (a bar is
assumed available at `min(end + latency, batch as_of)`, never before its end). A future
live provider must use `OBSERVED_RETRIEVAL` (available at the actual retrieval time).
Two exceptions always apply, whatever the basis (review round 1):

- a **revision** of an already-delivered bar (a correction, or a forming bar later completed)
  is available only from its own batch `as_of` (`REVISION_AT_BATCH_AS_OF`);
- a bar **missing from an earlier batch that covered its time** (between that symbol's first
  delivered bar and the latest earlier batch `as_of`) is a late gap fill, available only from
  its own batch `as_of` (`LATE_ARRIVAL_AT_BATCH_AS_OF`).

A batch run is classified `OBSERVED_MARKET` only when the dataset is `OBSERVED_RETRIEVAL` and the
batch `as_of` is at most 300 s old at import time; otherwise `RETROSPECTIVE` (or `SYNTHETIC`).

## 2. Instruments (`astra.instruments.v1`)

```json
{"format": "astra.instruments.v1", "is_sample": true, "instruments": [
  {"symbol": "ZSMPA", "name": "…", "exchange_mic": "XNAS", "currency": "USD",
   "security_type": "common_stock", "benchmark_symbol": "XBMK", "issuer_cik": null,
   "active_from": null, "active_to": null}
]}
```

`benchmark_symbol` must exist in the same dataset; without it the relative-strength
detectors are `UNAVAILABLE`. Changes are appended to `instrument_history`. Instrument
metadata is current-state, not point-in-time (a documented replay limitation).

## 3. Universe (`astra.universe.v1`)

```json
{"format": "astra.universe.v1", "universe_id": "SAMPLE-US-7@1", "name": "…", "is_sample": true,
 "members": ["ZSMPA", "ZSMPB"]}
```

`universe_id` must be `name@version`; a version's membership can never change. Each scan
copies the declared membership and its hash into its run record. The frozen v1 limit of
200 members per batch is enforced.

## 4. Bars (CSV or `astra.market.bars.v1` JSON)

CSV header: `symbol,start,open,high,low,close,volume,complete[,available_at]`

- `start` — bar start (the dataset's `bar_time_convention` is `start`); end = start + interval.
- `complete` — `true`/`false`. A forming bar must be sent as `false`; it is retained but never used.
- `available_at` — optional source-declared availability; must not precede the bar end.
- The batch **`as_of`** (provider snapshot time) is required: `--as-of`, or a sidecar
  `<file>.meta.json` containing `{"as_of": "…"}`, or the JSON field `as_of`.
  A future `as_of` is refused.

JSON form: `{"format": "astra.market.bars.v1", "dataset_id": "…", "as_of": "…", "bars": [ {…same fields…} ]}`.

Validation marks a bar `DATA_CONFLICT` (retained, never used) for: missing/non-numeric
fields, non-positive prices, negative volume, high/low inconsistent with open/close,
start not on the session's 5-minute grid, a "complete" bar ending after the batch `as_of`,
declared availability before the bar end, or conflicting duplicate rows in one batch.
Identical duplicates are dropped and counted.

Import is incremental: an unchanged bar writes nothing; a changed bar appends a new
revision to `bar_revisions` (all revisions kept) and updates the `bars` current-state
index. Bars are classified into `RTH`, `PRE`, `POST` or `OFF` using the exchange calendar.

## 5. Announcements (`astra.announcements.v1`)

```json
{
  "format": "astra.announcements.v1",
  "source": {"name": "sample-issuer-wire", "kind": "local_json", "is_sample": true,
             "coverage_label": "what this source does and does not cover"},
  "retrieved_at": "2026-09-17T12:10:00Z",
  "items": [
    {"external_id": "SW-0004", "symbol": "ZSMPF", "issuer_cik": "…", "issuer_name": "…",
     "title": "…", "url": "…", "published_at": "2026-09-17T07:45:00", "published_tz": "America/New_York",
     "time_kind": "SOURCE_STATED_PUBLICATION", "form_type": "8-K", "items": "1.01,9.01",
     "categories": ["m&a"], "material_hint": true, "body": "…"}
  ]
}
```

- `external_id` is the dedup key within a source. The whole item is the hashed content;
  any change appends a revision; an unchanged item records a sighting only.
- Every revision is re-screened (result stored with the revision). A revision that screens
  potentially material for an item without a candidate creates a `MATERIAL_REVISION`
  candidate from the revised content; a revision of an item with an open candidate attaches
  evidence and queues a research request.
- `retrieved_at` is stored as a source-declared (unverified) value. ASTRA's own first
  observation time is always the software clock at import.
- Classification per scope (`local:<source name>`): first successful collection →
  `BASELINE_BACKLOG`; later unseen items → `NEW_PUBLICATION` if published after the
  scope's prior publication watermark, `NEWLY_FOUND_OLD_INFORMATION` if at/before it,
  `NEW_OBSERVATION_TIME_UNKNOWN` if no usable time.
- Routing screen (`astra/config/screen-1.json`, `ASTRA-DISCLOSURE-SCREEN-1`): SEC form
  types, 8-K item numbers, local `categories`, or `material_hint`. Only non-backlog
  flagged items create candidates; `promote-announcement` creates one manually with a reason.
- All source text is untrusted data: stored and displayed escaped, never interpreted.

## 6. SEC issuers (`astra.sec_issuers.v1`)

```json
{"format": "astra.sec_issuers.v1", "issuers": [{"cik": "0000320193", "ticker": "AAPL", "name": "Apple Inc."}]}
```

Bounded by `ASTRA_SEC_MAX_ISSUERS` (default 25). Requires `ASTRA_SEC_USER_AGENT` containing
a name and contact email (SEC fair-access format) — without it no request is sent and the
run is recorded as failed with the exact reason. Only `filings.recent` metadata is read.
`acceptanceDateTime` is stored as the publication time with kind `SEC_ACCEPTANCE` (UTC).
A stock symbol is attached only when SEC's response confirms both the requested CIK and the
configured ticker (`SEC_CIK_TICKER_MATCH`). `TICKER_MISMATCH`, `CIK_MISMATCH` or no configured
ticker (`SEC_CIK_ONLY`) leave the symbol empty: candidates carry the issuer CIK only, and no
price data or outcome is attached to any stock.

## 7. Research record (`astra.research.v1`)

See `fixtures/sample/research/*.json` for complete examples and `astra/research.py` for
the validating model. Key rules:

- Unknown fields are rejected (no `target_price`, `expected_return`, `state`, orders, positions).
- Identify the candidate by `candidate_id` or `candidate_selector: {symbol, route}` (must
  match exactly one open candidate), or pass `--candidate`.
- `researcher.kind`: `manual`, `ai`, or `sample` (sample requires `is_sample: true`).
- `what_changed` (+ `when`, `when_basis`), `mechanism` ⊆ {ECON, PROB, NAV, FLOW, SUPPLY, TAPE}.
- `facts` (with `source_refs`), `inferences` (`based_on`), `unknowns` (`blocks_conclusions`),
  `sources` (optional `evidence_id` to cite an ASTRA evidence snapshot), `contrary_evidence`,
  `competing_explanations`. Records with `researcher.kind: ai` may cite only evidence ids or
  URLs present in the candidate's saved context and may not clear blockers.
- Sections `economic_materiality`, `valuation_scale`, `liquidity`, `financing`, `dilution`,
  `execution`: `status` = `assessed` | `unknown` | `not_applicable`; an unknown section may
  list the conclusions it blocks — it blocks nothing else. `valuation_scale` assessed requires
  contemporaneous price + time + economic shares + source.
- `blockers` (`key`, `description`, `blocks`, `clearing_evidence`) and `cleared_blockers`
  (`key`, `clearing_evidence`, `source_refs`). Every open blocker must be either re-listed or
  cleared with cited evidence.
- `recheck` / `expiry`: `{due_at | due_in (ISO-8601 duration), condition, priority}`.
- `proposed_disposition`: `researched` | `blocked` | `reject` (AI research may not reject).
- Revisions: `parent_research_id` (the latest id, or the literal `"latest"`) and
  `revision_reason_type` ∈ {NEW_EVENT, PRICE_OR_CONDITION_CHANGE, CORRECTED_INPUT,
  NEWLY_FOUND_OLD_INFORMATION, CHANGED_INFERENCE}.
- Optional `subjective_probabilities`: exact event, deadline, probability, label
  `UNCALIBRATED_JUDGMENT`. Never required.
- `completed_at` (declared) may not precede the candidate's detection or be in the future;
  if absent, the import time is used and labelled `IMPORT_TIME`. Replay candidates require
  `retrospective: true`.

## 8. Hourly alerts for comparison (CSV)

Header: `radar,symbol,alert_time,alert_time_tz,alert_time_basis,delivery_time,excerpt`

Import with the declared coverage window (and optional universe) the alerts are assumed
to cover:

```sh
astra --db DB import-hourly-alerts alerts.csv --source-name "Americas Early-Move Radar" \
  --coverage-start 2026-09-17T13:00:00Z --coverage-end 2026-09-17T21:00:00Z --universe AAPL,MSFT
```

`alert_time_basis` records what the time means (e.g. `message_timestamp`,
`scheduled_run_boundary`, `unknown`). A blank `delivery_time` stays UNKNOWN. Each import is one
source window: `export comparison` compares it only with scans, signals and candidates inside
that exact window, and only with that import's alerts (see `docs/OUTCOMES.md`).
