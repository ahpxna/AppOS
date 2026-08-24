-- 060 -- Unified Human Review Hub
BEGIN;

CREATE TABLE IF NOT EXISTS review_bundles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  bundle_kind text NOT NULL DEFAULT 'application' CHECK (bundle_kind IN ('application','document','autofill','reconciliation')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_review','approved','rejected','closed')),
  title text,
  summary_text text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (application_id, bundle_kind)
);

CREATE TABLE IF NOT EXISTS human_review_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_bundle_id uuid NOT NULL REFERENCES review_bundles(id) ON DELETE CASCADE,
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  item_type text NOT NULL CHECK (item_type IN (
    'document_review','approval_request','autofill_review','question_required',
    'reconciliation_required','application_ready'
  )),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending','approved','rejected','needs_revision','resolved','expired'
  )),
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE CASCADE,
  approval_request_id uuid REFERENCES approval_requests(id) ON DELETE CASCADE,
  browser_task_id uuid REFERENCES browser_tasks(id) ON DELETE CASCADE,
  title text NOT NULL,
  summary_text text,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_sha256 text,
  priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),
  decided_by text,
  decision_note text,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_review_document_active
  ON human_review_items(generated_document_id)
  WHERE generated_document_id IS NOT NULL AND item_type = 'document_review' AND status IN ('pending','needs_revision');
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_approval_active
  ON human_review_items(approval_request_id)
  WHERE approval_request_id IS NOT NULL AND item_type = 'approval_request' AND status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_autofill_task
  ON human_review_items(browser_task_id)
  WHERE browser_task_id IS NOT NULL AND item_type = 'autofill_review';
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_reconciliation_task
  ON human_review_items(browser_task_id)
  WHERE browser_task_id IS NOT NULL AND item_type = 'reconciliation_required' AND status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_question_active
  ON human_review_items(application_id, source_sha256)
  WHERE source_sha256 IS NOT NULL AND item_type = 'question_required' AND status IN ('pending','needs_revision');
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_application_ready_active
  ON human_review_items(application_id)
  WHERE item_type = 'application_ready' AND status = 'pending';
CREATE INDEX IF NOT EXISTS idx_human_review_items_actionable
  ON human_review_items(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_human_review_items_application
  ON human_review_items(application_id, created_at DESC);

CREATE TABLE IF NOT EXISTS human_review_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  generated_document_artifact_id uuid REFERENCES generated_document_artifacts(id) ON DELETE SET NULL,
  artifact_kind text NOT NULL CHECK (artifact_kind IN ('resume_pdf','cover_letter_pdf','autofill_screenshot','review_json')),
  file_path text NOT NULL,
  filename text NOT NULL,
  mime_type text NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_item_id, artifact_kind, sha256)
);

INSERT INTO pipeline_steps (step, layer, description, is_terminal, requires_human, sort_order)
VALUES ('application_ready', 'L1', 'Autofill reviewed; final submission remains a human action.', false, true, 95)
ON CONFLICT (step) DO UPDATE
SET description = EXCLUDED.description, requires_human = true, sort_order = EXCLUDED.sort_order;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('form_filled', 'application_ready', false, 'Human reviewed the post-autofill screenshot.'),
  ('application_ready', 'submitted', false, 'Human performed final submission.'),
  ('application_ready', 'abandoned', false, 'Human declined final submission.')
ON CONFLICT (from_step, to_step) DO UPDATE SET automated = EXCLUDED.automated, note = EXCLUDED.note;

CREATE OR REPLACE VIEW v_human_review_inbox AS
SELECT
  hri.id AS review_item_id,
  hri.review_bundle_id,
  hri.application_id,
  hri.item_type,
  hri.status,
  hri.priority,
  hri.title,
  hri.summary_text,
  hri.generated_document_id,
  hri.approval_request_id,
  hri.browser_task_id,
  hri.payload_json,
  hri.created_at,
  a.company,
  a.job_title,
  a.job_url
FROM human_review_items hri
JOIN applications a ON a.id = hri.application_id
WHERE hri.status IN ('pending','needs_revision')
ORDER BY CASE hri.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
         hri.created_at;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('human_review_hub', 'service', 'L1',
   'Unify all human intervention into one canonical inbox while leaving each underlying safety/approval state authoritative.',
   false, 'active',
   'Document decisions update generated_documents; capability decisions delegate to approval_service; autofill review never submits.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
