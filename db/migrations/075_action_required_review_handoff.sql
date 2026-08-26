BEGIN;

ALTER TABLE human_review_items
  DROP CONSTRAINT IF EXISTS human_review_items_item_type_check;

ALTER TABLE human_review_items
  ADD CONSTRAINT human_review_items_item_type_check CHECK (item_type IN (
    'document_review','approval_request','autofill_review','question_required',
    'reconciliation_required','application_ready','action_required'
  ));

CREATE UNIQUE INDEX IF NOT EXISTS uq_review_action_required_active
  ON human_review_items(application_id, (payload_json->>'action_kind'))
  WHERE item_type='action_required' AND status IN ('pending','needs_revision');

COMMIT;
