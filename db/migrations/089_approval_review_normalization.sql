-- 089 -- Typed authorization bindings and immutable human-review revisions.
BEGIN;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS parent_approval_request_id uuid REFERENCES approval_requests(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS bound_pipeline_version bigint,
  ADD COLUMN IF NOT EXISTS bound_autofill_plan_key text,
  ADD COLUMN IF NOT EXISTS binding_sha256 text,
  ADD COLUMN IF NOT EXISTS expected_target_id text,
  ADD COLUMN IF NOT EXISTS application_job_url text,
  ADD COLUMN IF NOT EXISTS application_jd_hash text,
  ADD COLUMN IF NOT EXISTS bound_email_candidate_id uuid REFERENCES email_verification_candidates(id) ON DELETE SET NULL;

UPDATE approval_requests
   SET bound_autofill_plan_key = coalesce(bound_autofill_plan_key, nullif(payload_json->>'autofill_plan_key','')),
       binding_sha256 = coalesce(binding_sha256, nullif(payload_json->>'binding_sha256','')),
       expected_target_id = coalesce(expected_target_id, nullif(payload_json->>'expected_target_id',''), nullif(payload_json->>'target_id','')),
       application_job_url = coalesce(application_job_url, payload_json->>'application_job_url', payload_json->>'job_url'),
       application_jd_hash = coalesce(application_jd_hash, payload_json->>'application_jd_hash', payload_json->>'jd_hash')
 WHERE payload_json IS NOT NULL;

-- Resolve relational identities by equality to existing rows instead of casting
-- untrusted historical JSON text. Broken legacy references remain NULL/fail-closed.
UPDATE approval_requests child
   SET parent_approval_request_id=parent.id
  FROM approval_requests parent
 WHERE child.parent_approval_request_id IS NULL
   AND child.payload_json->>'parent_approval_request_id'=parent.id::text;

UPDATE approval_requests ar
   SET bound_email_candidate_id=c.id
  FROM email_verification_candidates c
 WHERE ar.bound_email_candidate_id IS NULL
   AND ar.payload_json->>'candidate_id'=c.id::text;

-- Never bless a legacy approval with the application's *current* version: that
-- would revive a capability across an ABA/re-entry. Only recover a version that
-- the original payload explicitly carried; otherwise active autofill approvals
-- are retired fail-closed and must be recreated/reviewed.
UPDATE approval_requests
   SET bound_pipeline_version=(payload_json->>'expected_pipeline_version')::bigint
 WHERE bound_pipeline_version IS NULL
   AND coalesce(payload_json->>'expected_pipeline_version','') ~ '^[0-9]+$';

UPDATE approval_requests
   SET status='expired', executing_task_id=NULL,
       action_note=coalesce(action_note,'Legacy autofill approval lacked pipeline_version after DB-authority upgrade; recreate the plan.')
 WHERE type='autofill_form'
   AND status IN ('pending','approved')
   AND bound_pipeline_version IS NULL;

-- An already-executing legacy approval may have crossed browser I/O. Do not
-- relabel it retryable; consume the capability and let its task/journal drive
-- reconciliation.
UPDATE approval_requests
   SET status='consumed', consumed_at=coalesce(consumed_at,now()),
       consumed_by=coalesce(consumed_by,'migration-089'), executing_task_id=NULL,
       action_note=coalesce(action_note,'Legacy executing autofill approval lacked pipeline_version; never replay after upgrade.')
 WHERE type='autofill_form' AND status='executing' AND bound_pipeline_version IS NULL;

UPDATE browser_tasks bt
   SET status='failed', execution_state='needs_reconciliation',
       locked_by=NULL, lease_expires_at=NULL, finished_at=coalesce(finished_at,now()),
       error_message=coalesce(error_message,'Legacy executing approval lacked pipeline_version after DB-authority upgrade; reconcile before any retry.')
  FROM approval_requests ar
 WHERE bt.approval_request_id=ar.id
   AND ar.type='autofill_form' AND ar.consumed_by='migration-089'
   AND bt.execution_state <> 'completed';

CREATE INDEX IF NOT EXISTS idx_approval_requests_parent ON approval_requests(parent_approval_request_id);
CREATE INDEX IF NOT EXISTS idx_approval_requests_autofill_plan_key
  ON approval_requests(application_id,bound_autofill_plan_key)
  WHERE bound_autofill_plan_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_approval_requests_email_candidate
  ON approval_requests(bound_email_candidate_id) WHERE bound_email_candidate_id IS NOT NULL;

CREATE OR REPLACE FUNCTION jobos_validate_approval_binding()
RETURNS trigger AS $$
DECLARE
  v_parent_app uuid;
  v_parent_plan text;
BEGIN
  IF NEW.parent_approval_request_id IS NOT NULL THEN
    SELECT application_id,bound_autofill_plan_key INTO v_parent_app,v_parent_plan
      FROM approval_requests WHERE id=NEW.parent_approval_request_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'parent approval does not exist'; END IF;
    IF NEW.application_id IS DISTINCT FROM v_parent_app THEN
      RAISE EXCEPTION 'child approval application does not match parent';
    END IF;
    IF NEW.bound_autofill_plan_key IS NOT NULL AND v_parent_plan IS NOT NULL
       AND NEW.bound_autofill_plan_key <> v_parent_plan THEN
      RAISE EXCEPTION 'child approval autofill plan does not match parent';
    END IF;
  END IF;
  IF NEW.type='autofill_form' AND NEW.status IN ('pending','approved','executing') THEN
    IF NEW.bound_pipeline_version IS NULL OR NEW.bound_autofill_plan_key IS NULL
       OR NEW.expected_target_id IS NULL OR NEW.expected_origin IS NULL
       OR NEW.expected_initial_url IS NULL OR NEW.expected_page_fingerprint IS NULL
       OR NEW.bound_autofill_input_hash IS NULL OR NEW.bound_document_id IS NULL
       OR NEW.bound_document_sha256 IS NULL THEN
      RAISE EXCEPTION 'active autofill approval lacks an exact typed DB binding';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_approval_binding ON approval_requests;
CREATE TRIGGER trg_jobos_validate_approval_binding
BEFORE INSERT OR UPDATE OF parent_approval_request_id,application_id,bound_autofill_plan_key,bound_pipeline_version,
  expected_target_id,expected_origin,expected_initial_url,expected_page_fingerprint,bound_autofill_input_hash,
  bound_document_id,bound_document_sha256,status,type
ON approval_requests FOR EACH ROW EXECUTE FUNCTION jobos_validate_approval_binding();

ALTER TABLE approval_events
  ADD COLUMN IF NOT EXISTS binding_sha256 text,
  ADD COLUMN IF NOT EXISTS decision_channel text,
  ADD COLUMN IF NOT EXISTS review_item_id uuid REFERENCES human_review_items(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS human_review_item_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  revision_no integer NOT NULL CHECK (revision_no > 0),
  source_sha256 text,
  title text NOT NULL,
  summary_text text,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  approval_request_id uuid REFERENCES approval_requests(id) ON DELETE SET NULL,
  browser_task_id uuid REFERENCES browser_tasks(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(review_item_id,revision_no)
);

ALTER TABLE human_review_items
  ADD COLUMN IF NOT EXISTS current_revision_id uuid,
  ADD COLUMN IF NOT EXISTS review_version bigint NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION jobos_capture_review_revision()
RETURNS trigger AS $$
DECLARE
  v_revision integer;
  v_revision_id uuid;
BEGIN
  IF TG_OP='UPDATE' AND NOT (
       NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256
    OR NEW.title IS DISTINCT FROM OLD.title
    OR NEW.summary_text IS DISTINCT FROM OLD.summary_text
    OR NEW.payload_json IS DISTINCT FROM OLD.payload_json
    OR NEW.generated_document_id IS DISTINCT FROM OLD.generated_document_id
    OR NEW.approval_request_id IS DISTINCT FROM OLD.approval_request_id
    OR NEW.browser_task_id IS DISTINCT FROM OLD.browser_task_id
  ) THEN
    RETURN NEW;
  END IF;
  SELECT coalesce(max(revision_no),0)+1 INTO v_revision
    FROM human_review_item_revisions WHERE review_item_id=NEW.id;
  INSERT INTO human_review_item_revisions(
    review_item_id,revision_no,source_sha256,title,summary_text,payload_json,
    generated_document_id,approval_request_id,browser_task_id
  ) VALUES (
    NEW.id,v_revision,NEW.source_sha256,NEW.title,NEW.summary_text,NEW.payload_json,
    NEW.generated_document_id,NEW.approval_request_id,NEW.browser_task_id
  ) RETURNING id INTO v_revision_id;
  UPDATE human_review_items
     SET current_revision_id=v_revision_id,review_version=v_revision
   WHERE id=NEW.id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_capture_review_revision ON human_review_items;
CREATE TRIGGER trg_jobos_capture_review_revision
AFTER INSERT OR UPDATE OF source_sha256,title,summary_text,payload_json,generated_document_id,approval_request_id,browser_task_id
ON human_review_items FOR EACH ROW EXECUTE FUNCTION jobos_capture_review_revision();

-- Backfill one immutable revision for pre-089 review items.
INSERT INTO human_review_item_revisions(
  review_item_id,revision_no,source_sha256,title,summary_text,payload_json,
  generated_document_id,approval_request_id,browser_task_id,created_at
)
SELECT id,1,source_sha256,title,summary_text,payload_json,generated_document_id,approval_request_id,browser_task_id,created_at
  FROM human_review_items
ON CONFLICT (review_item_id,revision_no) DO NOTHING;
UPDATE human_review_items h
   SET current_revision_id=r.id,review_version=greatest(h.review_version,r.revision_no)
  FROM human_review_item_revisions r
 WHERE r.review_item_id=h.id AND r.revision_no=1 AND h.current_revision_id IS NULL;
ALTER TABLE human_review_items DROP CONSTRAINT IF EXISTS human_review_items_current_revision_id_fkey;
ALTER TABLE human_review_items ADD CONSTRAINT human_review_items_current_revision_id_fkey
  FOREIGN KEY (current_revision_id) REFERENCES human_review_item_revisions(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS human_review_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  review_revision_id uuid NOT NULL REFERENCES human_review_item_revisions(id) ON DELETE RESTRICT,
  decision text NOT NULL CHECK (decision IN ('approved','rejected','needs_revision','resolved')),
  actor text,
  channel text,
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(review_revision_id)
);

CREATE OR REPLACE FUNCTION jobos_capture_review_decision()
RETURNS trigger AS $$
BEGIN
  IF NEW.status IN ('approved','rejected','needs_revision','resolved')
     AND NEW.status IS DISTINCT FROM OLD.status AND NEW.current_revision_id IS NOT NULL THEN
    INSERT INTO human_review_decisions(review_item_id,review_revision_id,decision,actor,note)
    VALUES (NEW.id,NEW.current_revision_id,NEW.status,NEW.decided_by,NEW.decision_note)
    ON CONFLICT (review_revision_id) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_capture_review_decision ON human_review_items;
CREATE TRIGGER trg_jobos_capture_review_decision
AFTER UPDATE OF status ON human_review_items
FOR EACH ROW EXECUTE FUNCTION jobos_capture_review_decision();

ALTER TABLE telegram_callback_tokens
  ADD COLUMN IF NOT EXISTS review_revision_id uuid REFERENCES human_review_item_revisions(id) ON DELETE CASCADE;
UPDATE telegram_callback_tokens t
   SET review_revision_id=h.current_revision_id
  FROM human_review_items h
 WHERE h.id=t.review_item_id AND t.review_revision_id IS NULL;

COMMIT;
