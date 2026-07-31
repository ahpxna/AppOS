-- =========================================================
-- 043_interview_prep_layer.sql
-- L9 -- INTERVIEW PREP (minimal wiring)
--
-- Keeps the implementation intentionally small:
--   interviews.status = 'prep_needed' is the queue
--   interview_prep_packages stores the generated prep package
--   interviews.prep_package_id links back to the generated package
-- =========================================================

BEGIN;

CREATE TABLE IF NOT EXISTS interview_prep_packages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interview_id    uuid NOT NULL UNIQUE REFERENCES interviews(id) ON DELETE CASCADE,
  application_id  uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  model_name      text,
  prompt_version  text NOT NULL DEFAULT 'interview_prep_v1_2026_07_31',
  prep_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
  prep_notes      text NOT NULL,
  qa_status       text,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_interview_prep_packages_application
  ON interview_prep_packages(application_id);
CREATE INDEX IF NOT EXISTS idx_interview_prep_packages_qa
  ON interview_prep_packages(qa_status);

ALTER TABLE interviews
  ADD CONSTRAINT fk_interviews_prep_package
  FOREIGN KEY (prep_package_id) REFERENCES interview_prep_packages(id)
  ON DELETE SET NULL;

CREATE OR REPLACE VIEW v_interviews_pending_prep AS
SELECT
  i.id AS interview_id,
  i.application_id,
  i.interview_type,
  i.scheduled_at,
  i.timezone,
  i.status,
  a.company,
  a.job_title,
  a.fit_score,
  a.fit_decision
FROM interviews i
JOIN applications a ON a.id = i.application_id
WHERE i.status = 'prep_needed'
  AND i.prep_package_id IS NULL
ORDER BY i.created_at;

COMMENT ON VIEW v_interviews_pending_prep IS
  'Interview prep queue for L9. Rows enter when a recruiter interview invite '
  'is classified and the interview prep script has not yet generated a package.';

COMMIT;
