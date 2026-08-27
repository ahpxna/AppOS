-- 090 -- First-class immutable autofill plans and browser page snapshots.
BEGIN;

CREATE TABLE IF NOT EXISTS browser_page_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  browser_task_id uuid REFERENCES browser_tasks(id) ON DELETE SET NULL,
  target_id text NOT NULL,
  canonical_url text NOT NULL,
  origin text NOT NULL,
  page_fingerprint text NOT NULL CHECK (length(page_fingerprint)=64),
  snapshot_sha256 text NOT NULL CHECK (length(snapshot_sha256)=64),
  snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  captured_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(application_id,target_id,canonical_url,page_fingerprint,snapshot_sha256)
);

CREATE TABLE IF NOT EXISTS autofill_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  plan_key text NOT NULL UNIQUE CHECK (length(plan_key)=64),
  pipeline_version bigint NOT NULL,
  page_snapshot_id uuid REFERENCES browser_page_snapshots(id) ON DELETE SET NULL,
  target_id text NOT NULL,
  page_url text NOT NULL,
  origin text NOT NULL,
  page_fingerprint text NOT NULL CHECK (length(page_fingerprint)=64),
  input_sha256 text NOT NULL CHECK (length(input_sha256)=64),
  action_scope_sha256 text NOT NULL CHECK (length(action_scope_sha256)=64),
  action_scope_json jsonb NOT NULL,
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE RESTRICT,
  artifact_id uuid REFERENCES generated_document_artifacts(id) ON DELETE RESTRICT,
  artifact_sha256 text,
  status text NOT NULL DEFAULT 'prepared' CHECK (status IN (
    'prepared','awaiting_approval','approved','executing','completed','superseded','cancelled','needs_reconciliation'
  )),
  superseded_by uuid REFERENCES autofill_plans(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  approved_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_autofill_plans_application
  ON autofill_plans(application_id,created_at DESC);

CREATE TABLE IF NOT EXISTS autofill_plan_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  autofill_plan_id uuid NOT NULL REFERENCES autofill_plans(id) ON DELETE CASCADE,
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  action_kind text NOT NULL CHECK (action_kind IN ('fill','select','check','upload')),
  field_ref text NOT NULL,
  field_registry_key text,
  value_sha256 text NOT NULL CHECK (length(value_sha256)=64),
  document_artifact_id uuid REFERENCES generated_document_artifacts(id) ON DELETE RESTRICT,
  action_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(autofill_plan_id,sequence_no)
);

-- Recover historical plan snapshots from the exact active/terminal approval payloads.
INSERT INTO autofill_plans(
  application_id,plan_key,pipeline_version,target_id,page_url,origin,page_fingerprint,input_sha256,
  action_scope_sha256,action_scope_json,generated_document_id,artifact_id,artifact_sha256,status,created_at
)
SELECT DISTINCT ON (ar.payload_json->>'autofill_plan_key')
       ar.application_id,
       ar.payload_json->>'autofill_plan_key',
       ar.bound_pipeline_version,
       coalesce(ar.expected_target_id,ar.payload_json->>'expected_target_id'),
       coalesce(ar.expected_initial_url,ar.payload_json->>'expected_initial_url'),
       coalesce(ar.expected_origin,ar.payload_json->>'expected_origin'),
       coalesce(ar.expected_page_fingerprint,ar.payload_json->>'expected_page_fingerprint'),
       coalesce(ar.bound_autofill_input_hash,ar.payload_json->>'autofill_input_hash'),
       encode(digest(coalesce(ar.bound_autofill_action_scope,ar.payload_json->'autofill_action_scope','{}'::jsonb)::text,'sha256'),'hex'),
       coalesce(ar.bound_autofill_action_scope,ar.payload_json->'autofill_action_scope','{}'::jsonb),
       ar.bound_document_id,ar.bound_artifact_id,ar.bound_artifact_sha256,
       CASE ar.status WHEN 'approved' THEN 'approved' WHEN 'executing' THEN 'executing'
            WHEN 'consumed' THEN 'completed' WHEN 'denied' THEN 'cancelled' WHEN 'expired' THEN 'cancelled'
            ELSE 'awaiting_approval' END,
       ar.created_at
  FROM approval_requests ar
  JOIN applications a ON a.id=ar.application_id
 WHERE ar.type='autofill_form'
   AND ar.bound_pipeline_version IS NOT NULL
   AND length(coalesce(ar.payload_json->>'autofill_plan_key',''))=64
   AND length(coalesce(ar.expected_page_fingerprint,ar.payload_json->>'expected_page_fingerprint',''))=64
   AND length(coalesce(ar.bound_autofill_input_hash,ar.payload_json->>'autofill_input_hash',''))=64
   AND coalesce(ar.expected_target_id,ar.payload_json->>'expected_target_id') IS NOT NULL
 ORDER BY ar.payload_json->>'autofill_plan_key', ar.created_at DESC
ON CONFLICT (plan_key) DO NOTHING;

INSERT INTO autofill_plan_actions(autofill_plan_id,sequence_no,action_kind,field_ref,field_registry_key,value_sha256,action_json)
SELECT p.id, x.ord::integer,
       x.item->>'action', x.item->>'ref', nullif(x.item->>'profile_key',''), x.item->>'value_sha256', x.item
  FROM autofill_plans p
 CROSS JOIN LATERAL jsonb_array_elements(coalesce(p.action_scope_json->'actions','[]'::jsonb)) WITH ORDINALITY AS x(item,ord)
 WHERE x.item->>'action' IN ('fill','select','check','upload')
   AND coalesce(x.item->>'ref','')<>'' AND length(coalesce(x.item->>'value_sha256',''))=64
ON CONFLICT (autofill_plan_id,sequence_no) DO NOTHING;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS bound_autofill_plan_id uuid REFERENCES autofill_plans(id) ON DELETE RESTRICT;
ALTER TABLE browser_tasks
  ADD COLUMN IF NOT EXISTS autofill_plan_id uuid REFERENCES autofill_plans(id) ON DELETE RESTRICT;
UPDATE approval_requests ar SET bound_autofill_plan_id=p.id
  FROM autofill_plans p
 WHERE ar.bound_autofill_plan_id IS NULL AND ar.application_id=p.application_id
   AND coalesce(ar.bound_autofill_plan_key,ar.payload_json->>'autofill_plan_key')=p.plan_key;
UPDATE browser_tasks bt SET autofill_plan_id=p.id
  FROM approval_requests ar JOIN autofill_plans p ON p.id=ar.bound_autofill_plan_id
 WHERE bt.approval_request_id=ar.id AND bt.autofill_plan_id IS NULL;

-- Legacy rows whose payload cannot be reconstructed into an exact immutable
-- first-class plan are not safe to keep active. Never synthesize a binding
-- from the application's *current* state: that could revive stale authority.
UPDATE approval_requests
   SET status='expired', executing_task_id=NULL,
       action_note=coalesce(action_note,'Autofill approval could not be materialized as an exact first-class plan; recreate it after DB-authority upgrade.')
 WHERE type='autofill_form' AND status IN ('pending','approved')
   AND bound_autofill_plan_id IS NULL;

UPDATE approval_requests
   SET status='consumed', consumed_at=coalesce(consumed_at,now()),
       consumed_by=coalesce(consumed_by,'migration-090'), executing_task_id=NULL,
       action_note=coalesce(action_note,'Executing autofill approval lacked an exact first-class plan; never replay after DB-authority upgrade.')
 WHERE type='autofill_form' AND status='executing'
   AND bound_autofill_plan_id IS NULL;

UPDATE browser_tasks bt
   SET status='failed', execution_state='needs_reconciliation',
       locked_by=NULL, lease_expires_at=NULL, finished_at=coalesce(finished_at,now()),
       error_message=coalesce(error_message,'Autofill execution lacked an exact first-class plan after DB-authority upgrade; reconcile before any retry.')
  FROM approval_requests ar
 WHERE bt.approval_request_id=ar.id AND ar.consumed_by='migration-090'
   AND bt.execution_state <> 'completed';

CREATE INDEX IF NOT EXISTS idx_browser_tasks_autofill_plan ON browser_tasks(autofill_plan_id)
  WHERE autofill_plan_id IS NOT NULL;

COMMIT;
