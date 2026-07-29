-- Debug views for Job Apply OS
-- Phase DB-3: operational visibility

-- =========================
-- APPLICATION OVERVIEW
-- =========================

CREATE OR REPLACE VIEW v_application_overview AS
SELECT
  a.id,
  a.company,
  a.job_title,
  a.source,
  a.status,
  a.current_step,
  a.fit_score,
  a.fit_decision,
  a.priority,
  a.location,
  a.work_mode,
  a.seniority_level,
  a.deadline,
  a.submitted_at,
  a.created_at,
  a.updated_at,
  a.last_error,
  (
    SELECT count(*)
    FROM generated_documents gd
    WHERE gd.application_id = a.id
  ) AS generated_document_count,
  (
    SELECT count(*)
    FROM browser_tasks bt
    WHERE bt.application_id = a.id
  ) AS browser_task_count,
  (
    SELECT count(*)
    FROM approval_requests ar
    WHERE ar.application_id = a.id
  ) AS approval_count
FROM applications a;

-- =========================
-- BROWSER QUEUE
-- =========================

CREATE OR REPLACE VIEW v_browser_queue AS
SELECT
  bt.id,
  bt.task_type,
  bt.requested_by,
  bt.status,
  bt.priority,
  bt.retry_count,
  bt.max_retries,
  bt.timeout_seconds,
  bt.locked_by,
  bt.lease_expires_at,
  bt.approval_request_id,
  bt.idempotency_key,
  bt.application_id,
  bt.message_thread_id,
  bt.created_at,
  bt.started_at,
  bt.finished_at,
  bt.error_message,
  bt.input_json,
  bt.result_json
FROM browser_tasks bt
ORDER BY
  CASE bt.status
    WHEN 'running' THEN 1
    WHEN 'queued' THEN 2
    WHEN 'dead_letter' THEN 3
    WHEN 'failed' THEN 4
    WHEN 'completed' THEN 5
    ELSE 6
  END,
  CASE bt.priority
    WHEN 'high' THEN 1
    WHEN 'normal' THEN 2
    WHEN 'low' THEN 3
    ELSE 4
  END,
  bt.created_at ASC;

-- =========================
-- PENDING APPROVALS
-- =========================

CREATE OR REPLACE VIEW v_pending_approvals AS
SELECT
  ar.id,
  ar.type,
  ar.status,
  ar.target_action,
  ar.approval_channel,
  ar.token_expires_at,
  ar.action_taken,
  ar.action_note,
  ar.application_id,
  ar.message_thread_id,
  ar.idempotency_key,
  ar.notified_at,
  ar.reminded_at,
  ar.responded_at,
  ar.consumed_at,
  ar.consumed_by,
  ar.created_at,
  ar.payload_json
FROM approval_requests ar
WHERE ar.status IN ('pending', 'approved')
ORDER BY ar.created_at DESC;

-- =========================
-- PROFILE FACT OVERVIEW
-- =========================

CREATE OR REPLACE VIEW v_profile_fact_overview AS
SELECT
  pf.id,
  pf.category,
  pf.subcategory,
  pf.fact_text,
  pf.approved_by_user,
  pf.is_active,
  pf.confidence,
  pf.conflict_group_id,
  pf.conflict_status,
  pf.used_in_applications,
  pf.evidence_source,
  pf.evidence_quote,
  rf.file_name AS evidence_file_name,
  pf.created_at,
  pf.updated_at
FROM profile_facts pf
LEFT JOIN raw_files rf
  ON rf.id = pf.evidence_file_id
ORDER BY
  pf.approved_by_user DESC,
  pf.is_active DESC,
  pf.category,
  pf.subcategory,
  pf.created_at DESC;

-- =========================
-- MESSAGE INBOX
-- =========================

CREATE OR REPLACE VIEW v_message_inbox AS
SELECT
  mt.id,
  mt.source,
  mt.company,
  mt.person_name,
  mt.person_role,
  mt.classification,
  mt.status,
  mt.priority,
  mt.needs_user_attention,
  mt.linked_application_id,
  a.company AS linked_application_company,
  a.job_title AS linked_application_job_title,
  mt.last_message_at,
  mt.last_checked_at,
  mt.our_last_reply_at,
  mt.reply_count,
  mt.created_at,
  mt.updated_at,
  mt.last_message_text
FROM message_threads mt
LEFT JOIN applications a
  ON a.id = mt.linked_application_id
ORDER BY
  mt.needs_user_attention DESC,
  CASE mt.priority
    WHEN 'high' THEN 1
    WHEN 'normal' THEN 2
    WHEN 'low' THEN 3
    ELSE 4
  END,
  mt.last_message_at DESC NULLS LAST,
  mt.created_at DESC;

-- =========================
-- SYSTEM HEALTH
-- =========================

CREATE OR REPLACE VIEW v_system_health AS
SELECT
  (SELECT count(*) FROM applications) AS applications_total,
  (SELECT count(*) FROM applications WHERE status = 'submitted') AS applications_submitted,
  (SELECT count(*) FROM browser_tasks WHERE status = 'queued') AS browser_tasks_queued,
  (SELECT count(*) FROM browser_tasks WHERE status = 'running') AS browser_tasks_running,
  (SELECT count(*) FROM browser_tasks WHERE status = 'dead_letter') AS browser_tasks_dead_letter,
  (SELECT count(*) FROM approval_requests WHERE status = 'pending') AS approvals_pending,
  (SELECT count(*) FROM approval_requests WHERE status = 'approved') AS approvals_approved_unconsumed,
  (SELECT count(*) FROM message_threads WHERE needs_user_attention = true) AS messages_need_attention,
  (SELECT count(*) FROM profile_facts WHERE approved_by_user = true AND is_active = true) AS active_approved_profile_facts,
  (SELECT current_cost_usd FROM daily_budgets WHERE date = current_date) AS today_cost_usd,
  (SELECT max_cost_usd FROM daily_budgets WHERE date = current_date) AS today_budget_usd;

