-- 087 -- Durable execution identities shared by orchestration and operator commands.
BEGIN;

CREATE TABLE IF NOT EXISTS workflow_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_kind text NOT NULL,
  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  subject_type text,
  subject_id text,
  idempotency_key text,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending','claimed','running','completed','failed','uncertain','cancelled','needs_reconciliation'
  )),
  requested_by text,
  input_sha256 text,
  started_at timestamptz,
  finished_at timestamptz,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_application
  ON workflow_runs(application_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_open
  ON workflow_runs(status, created_at)
  WHERE status IN ('pending','claimed','running','uncertain','needs_reconciliation');

CREATE TABLE IF NOT EXISTS workflow_step_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  step_key text NOT NULL,
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  attempt_no integer NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending','claimed','running','completed','failed','uncertain','cancelled','needs_reconciliation'
  )),
  claimed_by text,
  lease_expires_at timestamptz,
  input_sha256 text,
  output_sha256 text,
  started_at timestamptz,
  finished_at timestamptz,
  error_message text,
  detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workflow_run_id, step_key, attempt_no),
  UNIQUE (workflow_run_id, sequence_no, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_workflow_step_runs_claimable
  ON workflow_step_runs(status, lease_expires_at, created_at)
  WHERE status IN ('pending','claimed','running');

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('durable_execution_kernel','control','L1',
   'Give orchestration and operator work a durable run/step identity independent of process memory.',
   false,'active',
   'Existing component_runs remain telemetry/learning records; workflow_runs is the correctness lineage.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
