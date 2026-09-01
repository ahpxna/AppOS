-- 102 -- Reuse an exact, already-paid LLM response after caller crash.
--
-- The provider charge is durable before a generated document/domain row can
-- be committed.  Persist the validated provider response beside its request
-- identity so recovery can finish the original business transaction without
-- issuing the same paid request again.
BEGIN;

ALTER TABLE llm_calls
  ADD COLUMN IF NOT EXISTS response_json jsonb,
  ADD COLUMN IF NOT EXISTS request_scope text;

UPDATE llm_calls
   SET request_scope='application:' || application_id::text
 WHERE request_scope IS NULL AND application_id IS NOT NULL;

ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_request_scope_check;
ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_request_scope_check
  CHECK (request_scope IS NULL OR (length(btrim(request_scope)) BETWEEN 3 AND 300));

CREATE INDEX IF NOT EXISTS idx_llm_calls_exact_application_request
  ON llm_calls(application_id,role,provider,configured_model,request_kind,request_sha256,started_at DESC)
  WHERE application_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_calls_exact_request_scope
  ON llm_calls(request_scope,role,provider,configured_model,request_kind,request_sha256,started_at DESC)
  WHERE request_scope IS NOT NULL;

COMMENT ON COLUMN llm_calls.response_json IS
  'Validated exact model response used only for application-bound idempotent replay after a caller crash; never provider credentials or prompts.';
COMMENT ON COLUMN llm_calls.request_scope IS
  'Durable business subject for exact response replay, such as application UUID, recruiter thread/message, or interview UUID.';

COMMIT;
