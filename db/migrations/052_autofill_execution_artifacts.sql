-- 052 -- verified upload artifacts for deterministic form sessions
BEGIN;

CREATE TABLE IF NOT EXISTS generated_document_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  generated_document_id uuid NOT NULL REFERENCES generated_documents(id) ON DELETE CASCADE,
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  artifact_type text NOT NULL CHECK (artifact_type IN ('resume', 'cover_letter')),
  file_path text NOT NULL,
  filename text NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (generated_document_id, artifact_type, sha256)
);

CREATE INDEX IF NOT EXISTS idx_generated_document_artifacts_application
  ON generated_document_artifacts(application_id, artifact_type, created_at DESC);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('deterministic_autofill_session', 'safety', 'L7',
   'Execute a capability-bound form action one field at a time with origin and post-write verification.',
   false, 'active',
   'OpenClaw receives only narrow fill/select/check/upload commands; approval is consumed after the first verified write.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
