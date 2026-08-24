-- 059 -- LinkedIn Saved Jobs as an explicit read-only discovery channel
BEGIN;

CREATE TABLE IF NOT EXISTS linkedin_saved_syncs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  browser_task_id uuid UNIQUE REFERENCES browser_tasks(id) ON DELETE SET NULL,
  requested_limit integer NOT NULL CHECK (requested_limit BETWEEN 1 AND 20),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed')),
  jobs_seen integer NOT NULL DEFAULT 0,
  jobs_created integer NOT NULL DEFAULT 0,
  duplicates integer NOT NULL DEFAULT 0,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz
);

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS discovery_channel text,
  ADD COLUMN IF NOT EXISTS linkedin_saved_at timestamptz,
  ADD COLUMN IF NOT EXISTS linkedin_saved_sync_id uuid REFERENCES linkedin_saved_syncs(id) ON DELETE SET NULL;

UPDATE applications
SET discovery_channel = CASE
  WHEN intake_channel = 'linkedin_browser_discovery' THEN 'search'
  WHEN intake_channel IN ('linkedin_export_user_reviewed', 'linkedin_browser_user_initiated') THEN 'manual'
  ELSE discovery_channel
END
WHERE source = 'linkedin' AND discovery_channel IS NULL;

CREATE INDEX IF NOT EXISTS idx_applications_discovery_channel
  ON applications(source, discovery_channel, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_linkedin_saved_syncs_status
  ON linkedin_saved_syncs(status, created_at);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('linkedin_saved_jobs_intake', 'service', 'L0',
   'Read the user Saved Jobs list from an already authenticated LinkedIn browser and ingest canonical job evidence through the normal intake boundary.',
   false, 'active',
   'Read only: never save/unsave, apply, message, upload, change preferences, authenticate, or solve a checkpoint. Saved Jobs is an alternate intake source only.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
