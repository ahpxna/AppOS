-- 091 -- Per-effect durable journal below each privileged one-shot execution.
BEGIN;

CREATE TABLE IF NOT EXISTS privileged_action_journal (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id uuid NOT NULL REFERENCES privileged_action_executions(id) ON DELETE CASCADE,
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  effect_kind text NOT NULL CHECK (effect_kind IN ('open','click','fill','check','upload','navigate','other')),
  target_id text,
  target_ref text,
  request_sha256 text NOT NULL CHECK (length(request_sha256)=64),
  precondition_sha256 text,
  status text NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','executed','verified','failed','uncertain')),
  observed_url text,
  observed_page_fingerprint text,
  evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  prepared_at timestamptz NOT NULL DEFAULT now(),
  executed_at timestamptz,
  verified_at timestamptz,
  UNIQUE(execution_id,sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_privileged_action_journal_open
  ON privileged_action_journal(status,prepared_at)
  WHERE status IN ('prepared','executed','uncertain');

COMMIT;
