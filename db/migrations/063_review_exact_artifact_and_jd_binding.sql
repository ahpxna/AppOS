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

-- 060 allowed one active review per generated document.  Before enforcing the
-- stronger slot invariant below, retire historical duplicate resume/cover
-- letter reviews deterministically so upgrades of an already-used database do
-- not fail while creating the index.
WITH ranked_active_reviews AS (
  SELECT h.id,
         row_number() OVER (
           PARTITION BY h.application_id, h.payload_json->>'doc_type'
           ORDER BY gd.version DESC NULLS LAST, h.created_at DESC, h.id DESC
         ) AS slot_rank
    FROM human_review_items h
    LEFT JOIN generated_documents gd ON gd.id = h.generated_document_id
   WHERE h.item_type = 'document_review'
     AND h.status IN ('pending', 'needs_revision')
)
UPDATE human_review_items h
   SET status = 'expired',
       decision_note = coalesce(h.decision_note || ' ', '') ||
         'Superseded during migration 063 by the newest active document review.',
       decided_at = now(),
       updated_at = now()
  FROM ranked_active_reviews r
 WHERE h.id = r.id
   AND r.slot_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_review_document_slot_active
  ON human_review_items(application_id, (payload_json->>'doc_type'))
  WHERE item_type = 'document_review'
    AND status IN ('pending', 'needs_revision');

COMMIT;
