# Review round 1 (ASTRA, 2026-09-30): findings and corrections

ASTRA's independent review of commit `39c9c78` confirmed that the tests, the demo and the doctrine
hashes pass. It then reproduced integrity defects with additional tests and asked for corrections
before accepting the handoff.

The reviewer's own scripts were not shared. Each finding was reconstructed as a test in
`tests/test_review_round1.py`, confirmed to **fail on the original code**, and then fixed.
Every fix is in the commit that follows `39c9c78`.

| # | Finding | Root cause | Fix | Reproduction test(s) |
|---|---|---|---|---|
| 1 | Replay used a Sep-24 correction at a Sep-17 cutoff | A corrected bar revision got the assumed availability `bar end + latency`, i.e. the original bar's time | Revisions, and bars missing from an earlier batch that covered their time (late gap fills), become available only at their own batch `as_of` (`REVISION_AT_BATCH_AS_OF`, `LATE_ARRIVAL_AT_BATCH_AS_OF`). History outside earlier coverage keeps the documented assumed availability | `test_later_correction_is_not_visible_at_an_earlier_cutoff`, `test_gap_filled_later_is_available_only_from_its_batch` |
| 2 | Apple's filing created a GOOG candidate despite a recorded mismatch | The configured ticker was used as the symbol even when SEC's response contradicted it | A symbol is attached only when SEC confirms both the requested CIK and the configured ticker. A mismatch, a CIK conflict or no configured ticker leaves the symbol empty. The candidate (issuer CIK only) says "no stock association (identity conflict…)". `identity_status` is reported per issuer | `test_sec_ticker_mismatch_blocks_stock_association` |
| 3 | AI output with invented URLs cleared blockers → RESEARCHED | Source references were checked only against the record's own `sources` list, which the model wrote | AI research may cite only evidence ids or URLs present in the candidate's saved context (`allowed_sources`, also given to the model). AI may never clear blockers, because it retrieves no new evidence. Enforced in the research importer, so it applies to any AI-labelled record | `test_ai_cannot_clear_blockers_with_invented_sources`, `test_ai_invented_source_rejection_is_recorded`, `test_ai_may_cite_sources_present_in_its_context` |
| 4a | A Sep-30 observation got a "forward" outcome ending Sep 18 | The observation reference used the latest known bar, however old, and endpoints were counted from that bar | New definition `ASTRA-OUTCOME-EQ-2`. Each reference stores its reference time. The reference bar must be fresh at that time (≤ 900 s before the expected latest RTH bar end), otherwise UNAVAILABLE. An endpoint whose close is not after the reference time is UNKNOWN | `test_forward_outcomes_never_end_before_their_reference_time` |
| 4b | A later benchmark correction changed the frozen comparison price | The benchmark reference was read from the current-state bar table | Instrument and benchmark reference prices both come from the bar revisions available at the reference time (scan view, or `bar_known_at`). Their revision numbers are stored | `test_later_benchmark_correction_does_not_change_frozen_reference` |
| 5 | Comparison counted a scan outside a source's interval, attributed another source's alert, double-counted one alert | Windows were matched by calendar date; alerts were keyed only by (session, symbol) across all sources; rows were emitted per window without source filtering | Each alert import (source + declared window) is compared only with scans, signals and candidates whose time lies inside that exact window, and only with its own alerts. Each alert is attached once or listed as excluded with a reason. The summary is per window, never summed | `test_comparison_respects_each_source_window_and_attribution` |
| 6 | An unflagged item revised into a material contract produced no candidate or research | Revisions were stored but never re-screened | Every revision is screened and the result stored with it. A revision that screens potentially material, for an item with no candidate, creates a `MATERIAL_REVISION` candidate from the revised content. A revision of an item with an open candidate queues a research request | `test_revision_that_becomes_material_creates_candidate_and_research_request`, `test_revision_of_open_candidate_queues_research`, `test_material_revision_has_no_pre_event_reference` |
| 7a | Removing a recheck left its job active | A research revision with `recheck: null` cleared the displayed time but not the queued job | Removing a recheck cancels pending recheck jobs | `test_removing_a_recheck_cancels_the_pending_job` |
| 7b | Repeated crashes gave 4 attempts to a 2-attempt job | Reclaiming an expired lease incremented attempts without checking `max_attempts` | Claims require `attempts < max_attempts`. An expired lease at the limit is dead-lettered ("lease expired … worker interrupted") on the next claim pass | `test_repeated_crashes_never_exceed_max_attempts` |
| 8 | Two overlapping calls both passed a one-call daily limit | The limit was checked by counting finished calls before the request; the row was written after | The allowance is reserved atomically (count + insert under `BEGIN IMMEDIATE`) before any request. A reservation left by a crashed process keeps counting | `test_overlapping_ai_calls_cannot_exceed_the_daily_limit` |
| 9 | Historical imports labelled "prospective" in a live database | Cohort eligibility was derived from the database mode alone | A live scan is `PROSPECTIVE_ASTRA_EQ` only if the dataset is `OBSERVED_RETRIEVAL`, its latest batch was classified `OBSERVED_MARKET`, and the data cutoff is ≤ 300 s before the scan (frozen v1 `maximum_prospective_cutoff_age_seconds`). Otherwise `EXCLUDED_NOT_PROSPECTIVE: <reasons>`. Bar batches are `OBSERVED_MARKET` only when observed-retrieval and ≤ 300 s old at import | `test_historical_imports_are_not_labelled_prospective`, `test_fresh_observed_retrieval_scan_is_prospective` |

## Not fixed: COMPRESSION parity (plateau lows)

The reviewer reports that the COMPRESSION reimplementation differs from the original v1 detector
on plateau lows. This implementation treats a low as a pivot only if it is **strictly** lower than
the two bars on each side, so equal lows are never pivots. The original `trading/pilot/runner.py`
is not in the doctrine bundle, so its rule cannot be reproduced here without guessing, and guessing
would give a second unverified interpretation.

Status: **KNOWN_DIVERGENCE**. COMPRESSION results remain experimental and outside the authoritative
v1 cohort. Parity needs the original `runner.py` (or its pivot function and tests) and a
side-by-side run on identical inputs.

## Behaviour changes to be aware of

- **Sample demo:** it now shows 3 DETECTED candidates, not 2. ZSMPA's backlog supply agreement is
  revised in the sample feed and still screens material, so it becomes a `MATERIAL_REVISION`
  candidate.
- **Announcement observation references in sample mode:** these become UNKNOWN (stale reference),
  because ASTRA "observes" on the real date while the sample data ends 24 Sep.
- **Outcome definitions:** `ASTRA-OUTCOME-EQ-1` subjects in existing databases are kept as recorded
  but are no longer evaluated. New subjects use EQ-2. Anomaly subjects are created at scan time, so
  an upgraded database gets EQ-2 anomaly subjects only from new scans or a fresh replay.
- **Comparison export:** columns now include `alert_source`, `alert_import_run_id`, the window
  bounds and per-window counts. The summary JSON has `per_window` and `excluded_alerts`
  (previously totals).
- **AI context:** includes `allowed_sources`; AI records must cite through `evidence_id` or a listed
  URL.

## Scope note

The optional AI adapter works only from saved context. It does not search the web or fetch
filing documents. Paying for it would not address any of these integrity issues, and none of
these fixes required paid services or hosting.
