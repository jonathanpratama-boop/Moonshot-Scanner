-- Corrections from the ASTRA review of 2026-09-30 (round 1). Additive only.

-- Outcomes (ASTRA-OUTCOME-EQ-2): the time each reference represents, and which bar revisions
-- supplied the frozen reference prices.
ALTER TABLE outcome_subjects ADD COLUMN reference_time_utc TEXT;
ALTER TABLE outcome_subjects ADD COLUMN reference_revision_no INTEGER;
ALTER TABLE outcome_subjects ADD COLUMN benchmark_reference_revision_no INTEGER;
ALTER TABLE outcome_observations ADD COLUMN endpoint_revision_no INTEGER;

-- Announcement revisions are re-screened; the screen applied to each revision is kept with it.
ALTER TABLE announcement_revisions ADD COLUMN screen_version TEXT;
ALTER TABLE announcement_revisions ADD COLUMN screen_result TEXT;
ALTER TABLE announcement_revisions ADD COLUMN screen_reasons_json TEXT;
