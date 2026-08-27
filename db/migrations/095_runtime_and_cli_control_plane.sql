-- 095 -- Durable runtime heartbeats and operator command audit; OS PID files remain the liveness authority.
BEGIN;

CREATE TABLE IF NOT EXISTS runtime_instances (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hostname text NOT NULL,
  pid integer NOT NULL,
  release_version text,
  git_commit text,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('starting','running','stopping','stopped','degraded','lost')),
  started_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  stopped_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_runtime_instances_heartbeat ON runtime_instances(status,heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS runtime_services (
  runtime_instance_id uuid NOT NULL REFERENCES runtime_instances(id) ON DELETE CASCADE,
  service_key text NOT NULL,
  pid integer,
  required boolean NOT NULL DEFAULT true,
  status text NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','running','stopped','degraded')),
  restart_count integer NOT NULL DEFAULT 0,
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  PRIMARY KEY(runtime_instance_id,service_key)
);

CREATE TABLE IF NOT EXISTS control_commands (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  command_kind text NOT NULL,
  arguments_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  requested_by text NOT NULL,
  idempotency_key text,
  workflow_run_id uuid REFERENCES workflow_runs(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','uncertain')),
  exit_code integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_control_commands_recent ON control_commands(created_at DESC);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('runtime_db_heartbeat','control','L1',
   'Persist runtime/service heartbeat and command audit while retaining OS PID/process checks as the immediate liveness source.',
   false,'active','runtime.json is a local cache; PostgreSQL supplies durable operational history, not millisecond PID truth.',now(),now())
ON CONFLICT (name) DO UPDATE SET purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
