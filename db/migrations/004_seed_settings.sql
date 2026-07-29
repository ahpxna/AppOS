-- Seed initial settings for Job Apply OS
-- Phase DB-2: budgets, sensitive defaults, state enums, policies

-- =========================
-- DAILY BUDGET DEFAULT
-- =========================

INSERT INTO daily_budgets (
  date,
  max_cost_usd,
  current_cost_usd,
  max_jobs_full_pipeline,
  max_browser_tasks
)
VALUES (
  current_date,
  3.00,
  0,
  5,
  25
)
ON CONFLICT (date)
DO UPDATE SET
  max_cost_usd = EXCLUDED.max_cost_usd,
  max_jobs_full_pipeline = EXCLUDED.max_jobs_full_pipeline,
  max_browser_tasks = EXCLUDED.max_browser_tasks;

-- =========================
-- SENSITIVE ANSWERS DEFAULTS
-- =========================
-- These are safe defaults. You can change them later.
-- The important part: all require review.

INSERT INTO sensitive_answers (
  field_name,
  answer,
  requires_review,
  approved_by_user,
  notes
)
VALUES
  (
    'hispanic_or_latino',
    'Prefer not to answer',
    true,
    false,
    'Default placeholder. User must approve before use.'
  ),
  (
    'gender',
    'Prefer not to answer',
    true,
    false,
    'Default placeholder. User must approve before use.'
  ),
  (
    'race',
    'Prefer not to answer',
    true,
    false,
    'Default placeholder. User must approve before use.'
  ),
  (
    'disability',
    'Prefer not to answer',
    true,
    false,
    'Default placeholder. User must approve before use.'
  ),
  (
    'veteran_status',
    'Prefer not to answer',
    true,
    false,
    'Default placeholder. User must approve before use.'
  ),
  (
    'work_authorization',
    'ASK_USER',
    true,
    false,
    'Legal/work authorization answer must always be confirmed by user.'
  ),
  (
    'visa_sponsorship',
    'ASK_USER',
    true,
    false,
    'Visa sponsorship answer must always be confirmed by user.'
  )
ON CONFLICT (field_name)
DO UPDATE SET
  answer = EXCLUDED.answer,
  requires_review = EXCLUDED.requires_review,
  approved_by_user = EXCLUDED.approved_by_user,
  notes = EXCLUDED.notes,
  updated_at = now();

-- =========================
-- SYSTEM SETTINGS
-- =========================

INSERT INTO system_settings (key, value)
VALUES
  (
    'application_states',
    '{
      "states": [
        "found",
        "jd_extracted",
        "fit_checked",
        "rejected_by_fit",
        "research_done",
        "context_pack_created",
        "resume_drafted",
        "cover_letter_drafted",
        "short_answers_drafted",
        "qa_failed",
        "user_revision_requested",
        "qa_passed",
        "waiting_user_review",
        "approved_for_autofill",
        "autofill_in_progress",
        "autofill_paused",
        "waiting_sensitive_fields",
        "waiting_final_submit",
        "submitted",
        "interview_scheduled",
        "rejected_by_company",
        "withdrawn",
        "failed",
        "paused"
      ]
    }'::jsonb
  ),
  (
    'browser_task_policy',
    '{
      "default_timeout_seconds": 120,
      "default_max_retries": 2,
      "single_active_browser_task": true,
      "dead_letter_on_exhausted_retries": true,
      "require_domain_whitelist": true,
      "require_approval_for_submit": true,
      "require_approval_for_send_message": true
    }'::jsonb
  ),
  (
    'approval_policy',
    '{
      "one_time_use": true,
      "token_ttl_minutes": 30,
      "store_token_hash_only": true,
      "final_submit_requires_separate_approval": true,
      "message_send_requires_approval": true,
      "sensitive_fields_require_approval": true
    }'::jsonb
  ),
  (
    'cost_policy',
    '{
      "daily_default_budget_usd": 3.00,
      "max_jobs_full_pipeline_per_day": 5,
      "max_browser_tasks_per_day": 25,
      "low_fit_threshold": 60,
      "auto_research_threshold": 75,
      "stop_generation_when_budget_exceeded": true
    }'::jsonb
  ),
  (
    'profile_policy',
    '{
      "facts_require_user_approval": true,
      "conflict_resolution_priority": [
        "manual_user_input",
        "newer_file",
        "higher_confidence",
        "ask_user"
      ],
      "briefs_mark_stale_on_new_approved_fact": true,
      "context_pack_uses_approved_facts_only": true
    }'::jsonb
  ),
  (
    'architecture_version',
    '{"version":"db-2","notes":"Core database, profile layer, queues, approvals, settings seeded."}'::jsonb
  )
ON CONFLICT (key)
DO UPDATE SET
  value = EXCLUDED.value,
  updated_at = now();

