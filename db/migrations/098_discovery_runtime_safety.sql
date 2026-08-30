-- 098 -- Durable discovery rotation, periodic health, and bounded ATS runs.
BEGIN;

CREATE TABLE IF NOT EXISTS discovery_scheduler_state (
  scheduler_key text PRIMARY KEY,
  cursor integer NOT NULL DEFAULT 0,
  last_queued_at timestamptz,
  last_finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS periodic_task_health (
  task_key text PRIMARY KEY,
  consecutive_failures integer NOT NULL DEFAULT 0,
  last_success_at timestamptz,
  last_failure_at timestamptz,
  last_error text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ats_discovery_runs_open
  ON ats_discovery_runs(started_at)
  WHERE finished_at IS NULL;

COMMIT;
