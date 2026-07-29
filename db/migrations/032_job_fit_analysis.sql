-- =========================================================
-- 032 — Job Fit Analysis
-- Purpose:
--   Store JD Analyzer / Fit Checker model outputs against real applications.
-- =========================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS job_fit_analyses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid NOT NULL
    REFERENCES applications(id)
    ON DELETE CASCADE,

  component_run_id uuid
    REFERENCES component_runs(id)
    ON DELETE SET NULL,

  analyzer_version text NOT NULL,
  analyzer_model text,
  profile_context_pack_id uuid
    REFERENCES profile_context_packs(id)
    ON DELETE SET NULL,

  fit_score integer,
  fit_decision text,
  model_fit_decision text,
  priority text,

  decision_reason text,
  role_family text,
  seniority_level text,
  work_mode text,
  location text,
  salary_range text,

  matched_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
  missing_or_weak_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
  quick_learn_targets jsonb NOT NULL DEFAULT '[]'::jsonb,
  hard_blockers jsonb NOT NULL DEFAULT '[]'::jsonb,
  risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  recommended_profile_brief_types jsonb NOT NULL DEFAULT '[]'::jsonb,
  extracted_job_fields jsonb NOT NULL DEFAULT '{}'::jsonb,

  next_step text,
  raw_model_output text,

  created_at timestamptz DEFAULT now(),

  UNIQUE (application_id, analyzer_version)
);

CREATE INDEX IF NOT EXISTS idx_job_fit_analyses_application
  ON job_fit_analyses(application_id);

CREATE INDEX IF NOT EXISTS idx_job_fit_analyses_decision
  ON job_fit_analyses(fit_decision, fit_score);

CREATE OR REPLACE VIEW v_job_fit_analysis_review AS
SELECT
  jfa.id AS job_fit_analysis_id,
  left(jfa.id::text, 8) AS analysis_short_id,

  a.id AS application_id,
  left(a.id::text, 8) AS application_short_id,
  a.company,
  a.job_title,
  a.job_url,
  a.status AS application_status,
  a.current_step,
  a.fit_score AS application_fit_score,
  a.fit_decision AS application_fit_decision,

  jfa.fit_score,
  jfa.fit_decision,
  jfa.model_fit_decision,
  jfa.priority,
  jfa.role_family,
  jfa.seniority_level,
  jfa.work_mode,
  jfa.location,
  jfa.salary_range,
  jfa.decision_reason,

  jsonb_array_length(jfa.matched_requirements) AS matched_count,
  jsonb_array_length(jfa.missing_or_weak_requirements) AS missing_count,
  jsonb_array_length(jfa.hard_blockers) AS hard_blocker_count,
  jsonb_array_length(jfa.risk_flags) AS risk_flag_count,

  jfa.quick_learn_targets,
  jfa.recommended_profile_brief_types,
  jfa.next_step,

  jfa.analyzer_version,
  jfa.analyzer_model,
  jfa.created_at

FROM job_fit_analyses jfa
JOIN applications a
  ON a.id = jfa.application_id;
