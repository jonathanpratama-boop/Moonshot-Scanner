# Database schema and migrations

SQLite, one file per database. Schema source: `astra/migrations/0001_initial.sql`.
Timestamps: UTC `YYYY-MM-DDTHH:MM:SSZ` (whole seconds, floor) so string order equals time
order; raw source strings and zones are kept in `*_raw` / `*_tz` columns. WAL journal
mode; foreign keys on.

**AO** = append-only (database triggers abort `UPDATE` and `DELETE`).
**Index** = mutable current-state table whose history lives in an AO table.

| Table | Kind | Purpose |
|---|---|---|
| `meta` | config | `db_mode` (sample/replay/live, fixed at init), `created_at_utc` |
| `schema_migrations` | config | applied migration versions + SHA-256 checksums |
| `runs` | ledger | every invocation: kind, mode, classification, status, actual start/completion, requested and data cutoffs, provider/dataset, config version/hash, declared scope (+hash), coverage status/JSON, cohort eligibility, input file hash, summary, error |
| `sources` | config | announcement sources with coverage labels, sample flag |
| `source_scopes` | Index | per-scope collection state: first/last success, publication watermark, failures |
| `announcements` | AO | first observation of each source item: identity, URL, form, publication time (UTC, raw, zone, kind, session context), first observed time/run, observation class, screen result, identity status |
| `announcement_revisions` | AO | every distinct content version (rev 1 = original) with hash and observation time |
| `announcement_sightings` | AO | every retrieval that saw an item |
| `providers`, `datasets` | config | dataset properties that must not be mixed |
| `instruments` | Index | current instrument metadata |
| `instrument_history` | AO | every metadata change |
| `universes` | AO | immutable membership per `name@version` |
| `bars` | Index | latest revision of each bar (indexed by dataset/symbol/session) |
| `bar_revisions` | AO | every bar version with its own availability time and quality |
| `detector_configs` | AO | frozen parameter sets per detector/rule/engine |
| `detector_results` | AO | one row per run × member × detector, all statuses |
| `evidence_snapshots` | AO | frozen detection/announcement inputs (content + hash) |
| `evidence_readbacks` | AO | separate read-back verification entries |
| `candidates` | Index | research state, owner, schedule, research status; reserved states blocked by triggers; one open anomaly episode per symbol (sample/live) via partial unique index |
| `candidate_evidence` | AO | evidence links (origin / co_detection / repeat_detection / announcement_revision) |
| `state_transitions` | AO | every state change with reason, evidence delta, remaining blockers |
| `candidate_events` | AO | non-state events (recheck processed, owner change, research update, repeat detection) |
| `research_records` | AO | each imported research record (content + hash, researcher, sample/retrospective flags, completion basis) |
| `blockers` | Index | open/cleared blockers with clearing evidence (history also in research records and transitions) |
| `work_items` | Index | rechecks, expiries, research requests; leases, attempts, errors |
| `work_attempts` | AO | every claim/done/failure |
| `notifications` | Index | preview queue with dedup key and frozen payload; queued/attempted/acknowledged |
| `notification_attempts` | AO | every delivery attempt and channel result |
| `outcome_definitions` | AO | frozen measurement specifications + hash |
| `outcome_subjects` | AO | what is measured from which reference |
| `outcome_observations` | AO | observations per horizon; one `MEASURED` max per subject/horizon |
| `external_alerts` | AO | retained hourly-alert rows for comparison |
| `external_alert_coverage` | config | declared coverage window/universe per alert import |
| `ai_calls` | audit | every AI gate decision and call (blocked, succeeded, refused, invalid…) |

## Migration `0002_review_round1.sql` (additive)

- `outcome_subjects`: `reference_time_utc`, `reference_revision_no`, `benchmark_reference_revision_no`.
- `outcome_observations`: `endpoint_revision_no`.
- `announcement_revisions`: `screen_version`, `screen_result`, `screen_reasons_json`.

Existing rows keep NULL in these columns. `ai_calls.status` gains the value `reserved`
(allowance reserved before a request; updated in place when the call finishes). Bar
`availability_basis` gains `REVISION_AT_BATCH_AS_OF` and `LATE_ARRIVAL_AT_BATCH_AS_OF`.
Verified on 2026-09-30 by upgrading a database created at `39c9c78`: both migrations are recorded,
the 117 EQ-1 subjects are retained and not evaluated, and new EQ-2 subjects are created.

## Migrations

- `astra init --mode …` creates the file and applies all migrations; every other command
  applies pending migrations on open.
- Migrations are ordered by the numeric prefix of `astra/migrations/NNNN_*.sql` and applied
  in a transaction each, recording a checksum. **Never edit an applied migration** — the
  runner refuses on checksum mismatch. Add `0002_…sql` instead.
- Because evidence tables are append-only via triggers, a migration that must rewrite them
  has to drop/recreate triggers explicitly inside the migration and should instead prefer
  additive changes (new columns/tables).
- Backups: copy the `.db` file while no command is running (or use `sqlite3 DB ".backup out.db"`).
