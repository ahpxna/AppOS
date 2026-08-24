-- 063 -- Exact review artifact and source-JD binding.
BEGIN;

ALTER TABLE generated_documents
  ADD COLUMN IF NOT EXISTS source_jd_hash text;

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS approved_resume_artifact_id uuid
    REFERENCES generated_document_artifacts(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS approved_cover_letter_artifact_id uuid
    REFERENCES generated_document_artifacts(id) ON DELETE SET NULL;

ALTER TABLE human_review_items
  ADD COLUMN IF NOT EXISTS reviewed_artifact_id uuid
    REFERENCES human_review_artifacts(id) ON DELETE SET NULL;

DROP INDEX IF EXISTS uq_review_reconciliation_task;
CREATE UNIQUE INDEX uq_review_reconciliation_task
  ON human_review_items(browser_task_id)
  WHERE browser_task_id IS NOT NULL
    AND item_type = 'reconciliation_required'
    AND status IN ('pending', 'needs_revision');

CREATE UNIQUE INDEX IF NOT EXISTS uq_review_document_slot_active
  ON human_review_items(application_id, (payload_json->>'doc_type'))
  WHERE item_type = 'document_review'
    AND status IN ('pending', 'needs_revision');

COMMIT;
