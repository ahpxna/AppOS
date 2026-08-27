-- 093 -- LLM request provenance and Gmail discovery provenance without storing secrets.
BEGIN;

CREATE TABLE IF NOT EXISTS llm_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_step_run_id uuid REFERENCES workflow_step_runs(id) ON DELETE SET NULL,
  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,
  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  role text NOT NULL,
  provider text NOT NULL,
  configured_model text NOT NULL,
  resolved_model text,
  request_kind text NOT NULL CHECK (request_kind IN ('generate','chat','embed')),
  request_sha256 text NOT NULL CHECK (length(request_sha256)=64),
  request_schema_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','running','completed','failed','uncertain')),
  reservation_id uuid REFERENCES llm_cost_reservations(id) ON DELETE SET NULL,
  provider_request_id text,
  input_tokens integer,
  output_tokens integer,
  response_sha256 text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_message text
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_application ON llm_calls(application_id,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_open ON llm_calls(status,started_at)
  WHERE status IN ('prepared','running','uncertain');

CREATE TABLE IF NOT EXISTS llm_call_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  llm_call_id uuid NOT NULL REFERENCES llm_calls(id) ON DELETE CASCADE,
  attempt_no integer NOT NULL,
  status text NOT NULL CHECK (status IN ('started','completed','failed','uncertain')),
  provider_request_id text,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE(llm_call_id,attempt_no)
);
ALTER TABLE llm_cost_reservations ADD COLUMN IF NOT EXISTS llm_call_id uuid REFERENCES llm_calls(id) ON DELETE SET NULL;
ALTER TABLE cost_ledger ADD COLUMN IF NOT EXISTS llm_call_id uuid REFERENCES llm_calls(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS gmail_discovery_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  gmail_account text NOT NULL,
  recipient text NOT NULL,
  requested_at timestamptz NOT NULL,
  employer_origin text,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
  scanned_count integer NOT NULL DEFAULT 0,
  candidate_id uuid REFERENCES email_verification_candidates(id) ON DELETE SET NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_message text
);

CREATE TABLE IF NOT EXISTS gmail_message_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  discovery_run_id uuid NOT NULL REFERENCES gmail_discovery_runs(id) ON DELETE CASCADE,
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  gmail_account text NOT NULL,
  gmail_message_id text NOT NULL,
  received_at timestamptz,
  sender text,
  subject text,
  headers_sha256 text,
  body_sha256 text,
  relevance_score integer,
  relevance_tier text,
  selected boolean NOT NULL DEFAULT false,
  observed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(discovery_run_id,gmail_message_id)
);

CREATE TABLE IF NOT EXISTS gmail_verification_extractions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_observation_id uuid NOT NULL REFERENCES gmail_message_observations(id) ON DELETE CASCADE,
  verification_kind text NOT NULL CHECK (verification_kind IN ('numeric_code','magic_link')),
  secret_sha256 text NOT NULL CHECK (length(secret_sha256)=64),
  secret_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  candidate_id uuid REFERENCES email_verification_candidates(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(message_observation_id,verification_kind,secret_sha256)
);

CREATE TABLE IF NOT EXISTS gmail_sync_cursors (
  gmail_account text NOT NULL,
  scope_key text NOT NULL,
  last_message_id text,
  last_received_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(gmail_account,scope_key)
);

COMMIT;
