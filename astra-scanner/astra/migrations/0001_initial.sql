-- ASTRA scanner schema v1.
-- All *_utc columns hold UTC ISO-8601 strings in the single canonical form YYYY-MM-DDTHH:MM:SSZ
-- (whole seconds, floor), so lexicographic comparison equals chronological comparison.
-- Original source time strings and time zones are preserved in *_raw / *_tz columns.
-- Tables marked APPEND-ONLY have triggers that abort UPDATE and DELETE.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------- runs
-- One row per invocation that collects, imports, scans, processes or delivers.
CREATE TABLE runs (
  run_id                TEXT PRIMARY KEY,
  kind                  TEXT NOT NULL CHECK (kind IN (
                          'announcement_import','sec_collection','bar_import','instrument_import',
                          'anomaly_scan','replay','process_due','outcome_measure','research_import',
                          'ai_research','notification_delivery','external_alert_import','cycle')),
  mode                  TEXT NOT NULL CHECK (mode IN ('sample','replay','live')),
  source_classification TEXT NOT NULL CHECK (source_classification IN (
                          'SYNTHETIC','RETROSPECTIVE','OBSERVED_MARKET','OBSERVED_SOURCE','OPERATOR_IMPORT','INTERNAL')),
  status                TEXT NOT NULL CHECK (status IN ('started','completed','partial','failed')),
  started_at_utc        TEXT NOT NULL,
  completed_at_utc      TEXT,
  clock_source          TEXT NOT NULL DEFAULT 'system_utc',
  worker_id             TEXT,
  provider_id           TEXT,
  dataset_id            TEXT,
  requested_cutoff_utc  TEXT,   -- simulated/declared decision cutoff (replay, sample)
  data_cutoff_utc       TEXT,   -- latest data time actually available to this run
  config_version        TEXT,
  config_hash           TEXT,
  declared_scope_json   TEXT,   -- declared universe / issuer list, retained per run
  declared_scope_hash   TEXT,
  coverage_status       TEXT CHECK (coverage_status IN ('SCANNED','PARTIAL','NOT_SCANNED') OR coverage_status IS NULL),
  coverage_json         TEXT,
  cohort_eligibility    TEXT,
  input_ref             TEXT,   -- file path / URL list and hash of imported input
  summary_json          TEXT,
  error                 TEXT
);
CREATE INDEX runs_kind_started ON runs(kind, started_at_utc);

-- ---------------------------------------------------------------- announcements
CREATE TABLE sources (
  source_id      TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('local_json','sec_submissions')),
  name           TEXT NOT NULL UNIQUE,
  coverage_label TEXT NOT NULL,
  is_sample      INTEGER NOT NULL,
  created_at_utc TEXT NOT NULL
);

-- Per-scope collection state (e.g. one SEC issuer, one local feed). Mutable index of current state.
CREATE TABLE source_scopes (
  scope_key                 TEXT PRIMARY KEY,
  source_id                 TEXT NOT NULL REFERENCES sources(source_id),
  first_success_at_utc      TEXT,
  last_success_run_id       TEXT,
  last_success_at_utc       TEXT,
  publication_watermark_utc TEXT,   -- latest source publication time seen in a successful collection
  last_attempt_at_utc       TEXT,
  last_error                TEXT,
  consecutive_failures      INTEGER NOT NULL DEFAULT 0
);

-- APPEND-ONLY. First observation of a source item. Never rewritten; content changes go to revisions.
CREATE TABLE announcements (
  announcement_id           TEXT PRIMARY KEY,
  source_id                 TEXT NOT NULL REFERENCES sources(source_id),
  source_key                TEXT NOT NULL,
  scope_key                 TEXT NOT NULL,
  issuer_cik                TEXT,
  symbol                    TEXT,
  issuer_name               TEXT,
  form_type                 TEXT,
  title                     TEXT,
  url                       TEXT,
  published_at_utc          TEXT,
  published_raw             TEXT,
  published_tz              TEXT,
  published_time_kind       TEXT NOT NULL,
  published_session_context TEXT,
  first_observed_at_utc     TEXT NOT NULL,
  first_run_id              TEXT NOT NULL REFERENCES runs(run_id),
  observation_class         TEXT NOT NULL CHECK (observation_class IN (
                              'BASELINE_BACKLOG','NEW_PUBLICATION','NEWLY_FOUND_OLD_INFORMATION',
                              'NEW_OBSERVATION_TIME_UNKNOWN')),
  declared_retrieved_raw    TEXT,
  screen_version            TEXT NOT NULL,
  screen_result             TEXT NOT NULL CHECK (screen_result IN ('POTENTIALLY_MATERIAL','NOT_FLAGGED')),
  screen_reasons_json       TEXT NOT NULL,
  identity_status           TEXT NOT NULL,
  is_sample                 INTEGER NOT NULL,
  UNIQUE (source_id, source_key)
);
CREATE INDEX announcements_symbol ON announcements(symbol);

-- APPEND-ONLY. Every distinct content version (revision 1 = original).
CREATE TABLE announcement_revisions (
  revision_id     TEXT PRIMARY KEY,
  announcement_id TEXT NOT NULL REFERENCES announcements(announcement_id),
  revision_no     INTEGER NOT NULL,
  content_hash    TEXT NOT NULL,
  content_json    TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  UNIQUE (announcement_id, revision_no)
);

-- APPEND-ONLY. Every retrieval that saw the item (duplicates do not create candidates).
CREATE TABLE announcement_sightings (
  announcement_id  TEXT NOT NULL REFERENCES announcements(announcement_id),
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  observed_at_utc  TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  PRIMARY KEY (announcement_id, run_id)
);

-- ---------------------------------------------------------------- market data
CREATE TABLE providers (
  provider_id    TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  is_sample      INTEGER NOT NULL,
  description    TEXT,
  created_at_utc TEXT NOT NULL
);

-- A dataset fixes every property that must not be mixed inside one calculation.
CREATE TABLE datasets (
  dataset_id               TEXT PRIMARY KEY,
  provider_id              TEXT NOT NULL REFERENCES providers(provider_id),
  interval_seconds         INTEGER NOT NULL,
  price_adjustment         TEXT NOT NULL,   -- unadjusted | split_adjusted | split_dividend_adjusted | unknown
  volume_scope             TEXT NOT NULL,   -- consolidated | primary_venue | unknown
  volume_unit              TEXT NOT NULL,   -- shares | unknown
  venue                    TEXT NOT NULL,
  session_timezone         TEXT NOT NULL,
  calendar_id              TEXT NOT NULL,
  currency                 TEXT NOT NULL,
  bar_time_convention      TEXT NOT NULL,   -- start (bar labelled by its start time)
  availability_basis       TEXT NOT NULL,   -- ASSUMED_BAR_END_PLUS_LATENCY | OBSERVED_RETRIEVAL
  assumed_latency_seconds  INTEGER NOT NULL,
  is_sample                INTEGER NOT NULL,
  metadata_json            TEXT NOT NULL,
  created_at_utc           TEXT NOT NULL
);

-- Mutable current-state index of instrument metadata; history kept in instrument_history.
CREATE TABLE instruments (
  symbol           TEXT PRIMARY KEY,
  name             TEXT,
  exchange_mic     TEXT,
  currency         TEXT,
  security_type    TEXT,
  benchmark_symbol TEXT,
  issuer_cik       TEXT,
  active_from      TEXT,
  active_to        TEXT,
  is_synthetic     INTEGER NOT NULL,
  metadata_json    TEXT NOT NULL,
  updated_at_utc   TEXT NOT NULL
);
-- APPEND-ONLY.
CREATE TABLE instrument_history (
  symbol          TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  content_json    TEXT NOT NULL,
  content_hash    TEXT NOT NULL
);

CREATE TABLE universes (
  universe_id    TEXT PRIMARY KEY,   -- name@version, immutable membership
  name           TEXT NOT NULL,
  members_json   TEXT NOT NULL,
  members_hash   TEXT NOT NULL,
  description    TEXT,
  is_sample      INTEGER NOT NULL,
  declared_at_utc TEXT NOT NULL
);

-- Current state of each bar (latest revision). Indexed for window reads.
CREATE TABLE bars (
  dataset_id           TEXT NOT NULL REFERENCES datasets(dataset_id),
  symbol               TEXT NOT NULL,
  start_utc            TEXT NOT NULL,
  end_utc              TEXT NOT NULL,
  session_date         TEXT NOT NULL,
  session_class        TEXT NOT NULL,   -- RTH | PRE | POST | OFF
  clock_slot           TEXT NOT NULL,   -- exchange-local HH:MM of bar start
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  source_complete      INTEGER NOT NULL,
  available_at_utc     TEXT NOT NULL,
  availability_basis   TEXT NOT NULL,
  quality              TEXT NOT NULL CHECK (quality IN ('OK','DATA_CONFLICT')),
  quality_reason       TEXT,
  revision_no          INTEGER NOT NULL,
  content_hash         TEXT NOT NULL,
  first_ingested_at_utc TEXT NOT NULL,
  last_run_id          TEXT NOT NULL,
  PRIMARY KEY (dataset_id, symbol, start_utc)
);
CREATE INDEX bars_session ON bars(dataset_id, symbol, session_date, session_class);

-- APPEND-ONLY. Every distinct version of every bar, with its own availability time.
CREATE TABLE bar_revisions (
  dataset_id          TEXT NOT NULL,
  symbol              TEXT NOT NULL,
  start_utc           TEXT NOT NULL,
  revision_no         INTEGER NOT NULL,
  end_utc             TEXT NOT NULL,
  session_date        TEXT NOT NULL,
  session_class       TEXT NOT NULL,
  clock_slot          TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  source_complete     INTEGER NOT NULL,
  available_at_utc    TEXT NOT NULL,
  availability_basis  TEXT NOT NULL,
  quality             TEXT NOT NULL,
  quality_reason      TEXT,
  content_hash        TEXT NOT NULL,
  ingested_at_utc     TEXT NOT NULL,
  run_id              TEXT NOT NULL,
  PRIMARY KEY (dataset_id, symbol, start_utc, revision_no)
);
CREATE INDEX bar_revisions_avail ON bar_revisions(dataset_id, symbol, available_at_utc);

-- ---------------------------------------------------------------- detection
-- APPEND-ONLY. Frozen detector parameter sets.
CREATE TABLE detector_configs (
  config_id      TEXT PRIMARY KEY,   -- detector@rule_version+engine
  detector       TEXT NOT NULL,
  rule_version   TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  params_json    TEXT NOT NULL,
  params_hash    TEXT NOT NULL,
  source_ref     TEXT NOT NULL,
  frozen_at_utc  TEXT NOT NULL
);

-- APPEND-ONLY. One row per (run, symbol, detector) including no-signal / insufficient / failed.
CREATE TABLE detector_results (
  result_id                TEXT PRIMARY KEY,
  run_id                   TEXT NOT NULL REFERENCES runs(run_id),
  symbol                   TEXT NOT NULL,
  detector                 TEXT NOT NULL,
  config_id                TEXT NOT NULL REFERENCES detector_configs(config_id),
  status                   TEXT NOT NULL CHECK (status IN (
                             'SIGNAL','NO_SIGNAL','INSUFFICIENT_DATA','UNAVAILABLE','FAILED')),
  reason                   TEXT,
  features_json            TEXT NOT NULL,
  signal_bar_end_utc       TEXT,
  feature_available_at_utc TEXT,
  evidence_id              TEXT,
  UNIQUE (run_id, symbol, detector)
);
CREATE INDEX detector_results_symbol ON detector_results(symbol, status);

-- APPEND-ONLY. Frozen detection / announcement inputs saved before any research.
CREATE TABLE evidence_snapshots (
  evidence_id    TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('anomaly_detection','announcement','manual')),
  created_at_utc TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  symbol         TEXT,
  content_json   TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  is_sample      INTEGER NOT NULL
);
-- APPEND-ONLY. Separate read-back verification entries (the frozen record is never rewritten).
CREATE TABLE evidence_readbacks (
  evidence_id     TEXT NOT NULL REFERENCES evidence_snapshots(evidence_id),
  verified_at_utc TEXT NOT NULL,
  ok              INTEGER NOT NULL,
  observed_hash   TEXT NOT NULL
);

-- ---------------------------------------------------------------- candidates
CREATE TABLE candidates (
  candidate_id           TEXT PRIMARY KEY,
  route                  TEXT NOT NULL CHECK (route IN ('announcement','anomaly')),
  mode                   TEXT NOT NULL CHECK (mode IN ('sample','replay','live')),
  symbol                 TEXT,
  issuer_cik             TEXT,
  dedup_key              TEXT NOT NULL UNIQUE,
  state                  TEXT NOT NULL CHECK (state IN (
                           'DETECTED','RESEARCHED','BLOCKED','REJECTED','EXPIRED',
                           'CONDITIONAL_READY','ENTRY_ELIGIBLE')),
  owner                  TEXT NOT NULL,
  created_at_utc         TEXT NOT NULL,
  detected_at_utc        TEXT NOT NULL,
  simulated_cutoff_utc   TEXT,
  data_cutoff_utc        TEXT,
  origin_run_id          TEXT NOT NULL REFERENCES runs(run_id),
  origin_evidence_id     TEXT NOT NULL REFERENCES evidence_snapshots(evidence_id),
  announcement_id        TEXT,
  observation_class      TEXT,
  mechanism_hint         TEXT,
  next_recheck_at_utc    TEXT,
  next_recheck_condition TEXT,
  expires_at_utc         TEXT,
  expiry_condition       TEXT,
  research_status        TEXT NOT NULL CHECK (research_status IN ('NOT_STARTED','QUEUED','COMPLETED','FAILED')),
  latest_research_id     TEXT,
  updated_at_utc         TEXT NOT NULL,
  version                INTEGER NOT NULL DEFAULT 1
);
-- One open anomaly episode per symbol (sample/live); later detections attach as repeat evidence.
-- Replay candidates are one episode per symbol and simulated session (dedup_key), so they are excluded.
CREATE UNIQUE INDEX candidates_one_open_anomaly ON candidates(route, mode, symbol)
  WHERE route = 'anomaly' AND mode <> 'replay' AND state IN ('DETECTED','RESEARCHED','BLOCKED');
CREATE INDEX candidates_state ON candidates(state);

-- APPEND-ONLY.
CREATE TABLE candidate_evidence (
  candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
  evidence_id   TEXT NOT NULL REFERENCES evidence_snapshots(evidence_id),
  role          TEXT NOT NULL CHECK (role IN ('origin','co_detection','repeat_detection','announcement_revision')),
  linked_at_utc TEXT NOT NULL,
  PRIMARY KEY (candidate_id, evidence_id)
);

-- APPEND-ONLY. Every state change with reason, evidence delta and remaining blockers.
CREATE TABLE state_transitions (
  transition_id           TEXT PRIMARY KEY,
  candidate_id            TEXT NOT NULL REFERENCES candidates(candidate_id),
  from_state              TEXT,
  to_state                TEXT NOT NULL,
  at_utc                  TEXT NOT NULL,
  actor                   TEXT NOT NULL,
  reason                  TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  reason_type             TEXT,
  evidence_json           TEXT NOT NULL CHECK (evidence_json <> '{}' AND evidence_json <> '[]'),
  remaining_blockers_json TEXT NOT NULL,
  run_id                  TEXT
);
CREATE INDEX state_transitions_candidate ON state_transitions(candidate_id, at_utc);

-- APPEND-ONLY. Non-state events: ownership, schedule, repeat detections, revisions, notes.
CREATE TABLE candidate_events (
  event_id     TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  at_utc       TEXT NOT NULL,
  kind         TEXT NOT NULL,
  actor        TEXT NOT NULL,
  detail_json  TEXT NOT NULL
);
CREATE INDEX candidate_events_candidate ON candidate_events(candidate_id, at_utc);

-- ---------------------------------------------------------------- research
-- APPEND-ONLY. Each imported research record (revisions are new rows linked to a parent).
CREATE TABLE research_records (
  research_id              TEXT PRIMARY KEY,
  candidate_id             TEXT NOT NULL REFERENCES candidates(candidate_id),
  parent_research_id       TEXT,
  revision_reason_type     TEXT,
  researcher_kind          TEXT NOT NULL CHECK (researcher_kind IN ('manual','ai','sample')),
  researcher_name          TEXT NOT NULL,
  model                    TEXT,
  is_sample                INTEGER NOT NULL,
  retrospective            INTEGER NOT NULL,
  declared_completed_raw   TEXT,
  completed_at_utc         TEXT NOT NULL,
  completed_at_basis       TEXT NOT NULL,
  imported_at_utc          TEXT NOT NULL,
  content_json             TEXT NOT NULL,
  content_hash             TEXT NOT NULL,
  run_id                   TEXT NOT NULL,
  UNIQUE (candidate_id, content_hash)
);

CREATE TABLE blockers (
  blocker_id                 TEXT PRIMARY KEY,
  candidate_id               TEXT NOT NULL REFERENCES candidates(candidate_id),
  key                        TEXT NOT NULL,
  description                TEXT NOT NULL,
  blocks_json                TEXT NOT NULL,
  clearing_evidence_required TEXT NOT NULL,
  owner                      TEXT,
  opened_by_research_id      TEXT NOT NULL,
  opened_at_utc              TEXT NOT NULL,
  status                     TEXT NOT NULL CHECK (status IN ('open','cleared')),
  cleared_by_research_id     TEXT,
  cleared_at_utc             TEXT,
  clearing_evidence_json     TEXT
);
CREATE UNIQUE INDEX blockers_one_open_key ON blockers(candidate_id, key) WHERE status = 'open';

-- ---------------------------------------------------------------- work queue
CREATE TABLE work_items (
  work_id              TEXT PRIMARY KEY,
  dedup_key            TEXT NOT NULL UNIQUE,
  kind                 TEXT NOT NULL CHECK (kind IN ('recheck','expiry','outcome_measure','research_request')),
  priority             TEXT NOT NULL CHECK (priority IN ('urgent','routine')),
  candidate_id         TEXT,
  due_at_utc           TEXT NOT NULL,
  condition            TEXT,
  status               TEXT NOT NULL CHECK (status IN ('pending','leased','done','dead','cancelled')),
  attempts             INTEGER NOT NULL DEFAULT 0,
  max_attempts         INTEGER NOT NULL DEFAULT 3,
  lease_owner          TEXT,
  lease_expires_at_utc TEXT,
  last_error           TEXT,
  result_json          TEXT,
  created_at_utc       TEXT NOT NULL,
  updated_at_utc       TEXT NOT NULL
);
CREATE INDEX work_items_due ON work_items(status, due_at_utc);

-- APPEND-ONLY.
CREATE TABLE work_attempts (
  attempt_id      TEXT PRIMARY KEY,
  work_id         TEXT NOT NULL REFERENCES work_items(work_id),
  worker_id       TEXT NOT NULL,
  started_at_utc  TEXT NOT NULL,
  outcome         TEXT NOT NULL,
  detail          TEXT,
  run_id          TEXT
);

-- ---------------------------------------------------------------- notifications
CREATE TABLE notifications (
  notification_id       TEXT PRIMARY KEY,
  dedup_key             TEXT NOT NULL UNIQUE,
  candidate_id          TEXT,
  kind                  TEXT NOT NULL,
  priority              TEXT NOT NULL CHECK (priority IN ('urgent','routine')),
  mode                  TEXT NOT NULL,
  payload_json          TEXT NOT NULL,
  created_at_utc        TEXT NOT NULL,
  status                TEXT NOT NULL CHECK (status IN ('queued','attempted','acknowledged','failed')),
  last_attempt_at_utc   TEXT,
  acknowledged_at_utc   TEXT,
  acknowledged_by       TEXT
);
-- APPEND-ONLY.
CREATE TABLE notification_attempts (
  attempt_id      TEXT PRIMARY KEY,
  notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
  channel         TEXT NOT NULL,
  attempted_at_utc TEXT NOT NULL,
  result          TEXT NOT NULL CHECK (result IN ('written_to_local_outbox','not_configured','failed')),
  detail          TEXT
);

-- ---------------------------------------------------------------- outcomes
-- APPEND-ONLY. Measurement definitions are frozen before any outcome is recorded.
CREATE TABLE outcome_definitions (
  definition_id TEXT PRIMARY KEY,
  spec_json     TEXT NOT NULL,
  spec_hash     TEXT NOT NULL,
  frozen_at_utc TEXT NOT NULL
);
-- APPEND-ONLY. What is measured, from which retained reference.
CREATE TABLE outcome_subjects (
  subject_id              TEXT PRIMARY KEY,
  subject_type            TEXT NOT NULL CHECK (subject_type IN ('candidate','sampled_non_signal')),
  candidate_id            TEXT,
  run_id                  TEXT NOT NULL,
  symbol                  TEXT NOT NULL,
  dataset_id              TEXT NOT NULL,
  definition_id           TEXT NOT NULL REFERENCES outcome_definitions(definition_id),
  mode                    TEXT NOT NULL,
  reference_role          TEXT NOT NULL,
  reference_session_date  TEXT,
  reference_bar_start_utc TEXT,
  reference_price         REAL,
  reference_status        TEXT NOT NULL CHECK (reference_status IN ('AVAILABLE','UNAVAILABLE')),
  reference_reason        TEXT,
  benchmark_symbol        TEXT,
  benchmark_reference_price REAL,
  created_at_utc          TEXT NOT NULL,
  UNIQUE (subject_type, candidate_id, run_id, symbol, definition_id, reference_role)
);
-- APPEND-ONLY. Latest row per (subject, horizon) is the current observation.
CREATE TABLE outcome_observations (
  observation_id         TEXT PRIMARY KEY,
  subject_id             TEXT NOT NULL REFERENCES outcome_subjects(subject_id),
  horizon_sessions       INTEGER NOT NULL,
  endpoint_session_date  TEXT,
  status                 TEXT NOT NULL CHECK (status IN ('MEASURED','PENDING_DATA','UNKNOWN','CENSORED')),
  reason                 TEXT,
  endpoint_bar_start_utc TEXT,
  endpoint_price         REAL,
  return_value           REAL,
  benchmark_return       REAL,
  excess_return          REAL,
  benchmark_status       TEXT,
  data_cutoff_utc        TEXT,
  observed_at_utc        TEXT NOT NULL,
  run_id                 TEXT NOT NULL
);
-- A MEASURED value is final; UNKNOWN/CENSORED may later be superseded by an appended observation
-- (e.g. late data backfill), never overwritten.
CREATE UNIQUE INDEX outcome_one_measured ON outcome_observations(subject_id, horizon_sessions)
  WHERE status = 'MEASURED';

-- ---------------------------------------------------------------- external comparison
-- APPEND-ONLY. Retained hourly ChatGPT alert rows for comparison exports.
CREATE TABLE external_alerts (
  alert_row_id         TEXT PRIMARY KEY,
  source_name          TEXT NOT NULL,
  radar                TEXT,
  symbol               TEXT NOT NULL,
  alert_time_raw       TEXT,
  alert_time_utc       TEXT,
  alert_time_basis     TEXT NOT NULL,
  delivery_time_utc    TEXT,
  excerpt              TEXT,
  imported_at_utc      TEXT NOT NULL,
  run_id               TEXT NOT NULL,
  content_hash         TEXT NOT NULL UNIQUE
);
CREATE TABLE external_alert_coverage (
  run_id             TEXT PRIMARY KEY,
  source_name        TEXT NOT NULL,
  coverage_start_utc TEXT NOT NULL,
  coverage_end_utc   TEXT NOT NULL,
  universe_json      TEXT,
  notes              TEXT
);

-- ---------------------------------------------------------------- AI research audit
CREATE TABLE ai_calls (
  call_id          TEXT PRIMARY KEY,
  candidate_id     TEXT,
  provider         TEXT NOT NULL,
  model            TEXT NOT NULL,
  requested_at_utc TEXT NOT NULL,
  completed_at_utc TEXT,
  status           TEXT NOT NULL,
  http_status      INTEGER,
  attempts         INTEGER NOT NULL,
  input_chars      INTEGER,
  output_tokens    INTEGER,
  error            TEXT,
  response_excerpt TEXT,
  run_id           TEXT
);

-- ---------------------------------------------------------------- append-only enforcement
CREATE TRIGGER ao_announcements_u BEFORE UPDATE ON announcements BEGIN SELECT RAISE(ABORT, 'append-only: announcements'); END;
CREATE TRIGGER ao_announcements_d BEFORE DELETE ON announcements BEGIN SELECT RAISE(ABORT, 'append-only: announcements'); END;
CREATE TRIGGER ao_ann_rev_u BEFORE UPDATE ON announcement_revisions BEGIN SELECT RAISE(ABORT, 'append-only: announcement_revisions'); END;
CREATE TRIGGER ao_ann_rev_d BEFORE DELETE ON announcement_revisions BEGIN SELECT RAISE(ABORT, 'append-only: announcement_revisions'); END;
CREATE TRIGGER ao_ann_sight_u BEFORE UPDATE ON announcement_sightings BEGIN SELECT RAISE(ABORT, 'append-only: announcement_sightings'); END;
CREATE TRIGGER ao_ann_sight_d BEFORE DELETE ON announcement_sightings BEGIN SELECT RAISE(ABORT, 'append-only: announcement_sightings'); END;
CREATE TRIGGER ao_instr_hist_u BEFORE UPDATE ON instrument_history BEGIN SELECT RAISE(ABORT, 'append-only: instrument_history'); END;
CREATE TRIGGER ao_instr_hist_d BEFORE DELETE ON instrument_history BEGIN SELECT RAISE(ABORT, 'append-only: instrument_history'); END;
CREATE TRIGGER ao_universes_u BEFORE UPDATE ON universes BEGIN SELECT RAISE(ABORT, 'append-only: universes'); END;
CREATE TRIGGER ao_universes_d BEFORE DELETE ON universes BEGIN SELECT RAISE(ABORT, 'append-only: universes'); END;
CREATE TRIGGER ao_bar_rev_u BEFORE UPDATE ON bar_revisions BEGIN SELECT RAISE(ABORT, 'append-only: bar_revisions'); END;
CREATE TRIGGER ao_bar_rev_d BEFORE DELETE ON bar_revisions BEGIN SELECT RAISE(ABORT, 'append-only: bar_revisions'); END;
CREATE TRIGGER ao_det_cfg_u BEFORE UPDATE ON detector_configs BEGIN SELECT RAISE(ABORT, 'append-only: detector_configs'); END;
CREATE TRIGGER ao_det_cfg_d BEFORE DELETE ON detector_configs BEGIN SELECT RAISE(ABORT, 'append-only: detector_configs'); END;
CREATE TRIGGER ao_det_res_u BEFORE UPDATE ON detector_results BEGIN SELECT RAISE(ABORT, 'append-only: detector_results'); END;
CREATE TRIGGER ao_det_res_d BEFORE DELETE ON detector_results BEGIN SELECT RAISE(ABORT, 'append-only: detector_results'); END;
CREATE TRIGGER ao_evidence_u BEFORE UPDATE ON evidence_snapshots BEGIN SELECT RAISE(ABORT, 'append-only: evidence_snapshots'); END;
CREATE TRIGGER ao_evidence_d BEFORE DELETE ON evidence_snapshots BEGIN SELECT RAISE(ABORT, 'append-only: evidence_snapshots'); END;
CREATE TRIGGER ao_ev_rb_u BEFORE UPDATE ON evidence_readbacks BEGIN SELECT RAISE(ABORT, 'append-only: evidence_readbacks'); END;
CREATE TRIGGER ao_ev_rb_d BEFORE DELETE ON evidence_readbacks BEGIN SELECT RAISE(ABORT, 'append-only: evidence_readbacks'); END;
CREATE TRIGGER ao_cand_ev_u BEFORE UPDATE ON candidate_evidence BEGIN SELECT RAISE(ABORT, 'append-only: candidate_evidence'); END;
CREATE TRIGGER ao_cand_ev_d BEFORE DELETE ON candidate_evidence BEGIN SELECT RAISE(ABORT, 'append-only: candidate_evidence'); END;
CREATE TRIGGER ao_transitions_u BEFORE UPDATE ON state_transitions BEGIN SELECT RAISE(ABORT, 'append-only: state_transitions'); END;
CREATE TRIGGER ao_transitions_d BEFORE DELETE ON state_transitions BEGIN SELECT RAISE(ABORT, 'append-only: state_transitions'); END;
CREATE TRIGGER ao_cand_events_u BEFORE UPDATE ON candidate_events BEGIN SELECT RAISE(ABORT, 'append-only: candidate_events'); END;
CREATE TRIGGER ao_cand_events_d BEFORE DELETE ON candidate_events BEGIN SELECT RAISE(ABORT, 'append-only: candidate_events'); END;
CREATE TRIGGER ao_research_u BEFORE UPDATE ON research_records BEGIN SELECT RAISE(ABORT, 'append-only: research_records'); END;
CREATE TRIGGER ao_research_d BEFORE DELETE ON research_records BEGIN SELECT RAISE(ABORT, 'append-only: research_records'); END;
CREATE TRIGGER ao_work_att_u BEFORE UPDATE ON work_attempts BEGIN SELECT RAISE(ABORT, 'append-only: work_attempts'); END;
CREATE TRIGGER ao_work_att_d BEFORE DELETE ON work_attempts BEGIN SELECT RAISE(ABORT, 'append-only: work_attempts'); END;
CREATE TRIGGER ao_notif_att_u BEFORE UPDATE ON notification_attempts BEGIN SELECT RAISE(ABORT, 'append-only: notification_attempts'); END;
CREATE TRIGGER ao_notif_att_d BEFORE DELETE ON notification_attempts BEGIN SELECT RAISE(ABORT, 'append-only: notification_attempts'); END;
CREATE TRIGGER ao_out_def_u BEFORE UPDATE ON outcome_definitions BEGIN SELECT RAISE(ABORT, 'append-only: outcome_definitions'); END;
CREATE TRIGGER ao_out_def_d BEFORE DELETE ON outcome_definitions BEGIN SELECT RAISE(ABORT, 'append-only: outcome_definitions'); END;
CREATE TRIGGER ao_out_subj_u BEFORE UPDATE ON outcome_subjects BEGIN SELECT RAISE(ABORT, 'append-only: outcome_subjects'); END;
CREATE TRIGGER ao_out_subj_d BEFORE DELETE ON outcome_subjects BEGIN SELECT RAISE(ABORT, 'append-only: outcome_subjects'); END;
CREATE TRIGGER ao_out_obs_u BEFORE UPDATE ON outcome_observations BEGIN SELECT RAISE(ABORT, 'append-only: outcome_observations'); END;
CREATE TRIGGER ao_out_obs_d BEFORE DELETE ON outcome_observations BEGIN SELECT RAISE(ABORT, 'append-only: outcome_observations'); END;
CREATE TRIGGER ao_ext_alerts_u BEFORE UPDATE ON external_alerts BEGIN SELECT RAISE(ABORT, 'append-only: external_alerts'); END;
CREATE TRIGGER ao_ext_alerts_d BEFORE DELETE ON external_alerts BEGIN SELECT RAISE(ABORT, 'append-only: external_alerts'); END;

-- Reserved readiness states are unreachable in this prototype, even through direct SQL.
CREATE TRIGGER reserved_states_block_insert BEFORE INSERT ON candidates
  WHEN NEW.state IN ('CONDITIONAL_READY','ENTRY_ELIGIBLE')
  BEGIN SELECT RAISE(ABORT, 'reserved state: not implemented in prototype'); END;
CREATE TRIGGER reserved_states_block_update BEFORE UPDATE OF state ON candidates
  WHEN NEW.state IN ('CONDITIONAL_READY','ENTRY_ELIGIBLE')
  BEGIN SELECT RAISE(ABORT, 'reserved state: not implemented in prototype'); END;
