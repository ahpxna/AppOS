-- 094 -- Canonical artifact/template/render provenance while bytes remain on disk/object storage.
BEGIN;

CREATE TABLE IF NOT EXISTS artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  artifact_kind text NOT NULL,
  storage_backend text NOT NULL DEFAULT 'filesystem' CHECK (storage_backend IN ('filesystem','object_store')),
  storage_key text NOT NULL,
  filename text NOT NULL,
  mime_type text,
  size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
  sha256 text NOT NULL CHECK (length(sha256)=64),
  provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'available' CHECK (status IN ('available','missing','superseded','deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(storage_backend,storage_key,sha256)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_application ON artifacts(application_id,artifact_kind,created_at DESC);

CREATE TABLE IF NOT EXISTS document_template_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  template_key text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  file_path text NOT NULL,
  sha256 text NOT NULL CHECK (length(sha256)=64),
  contract_version text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired','missing')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(template_key,version),
  UNIQUE(template_key,sha256)
);

CREATE TABLE IF NOT EXISTS document_render_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  generated_document_id uuid NOT NULL REFERENCES generated_documents(id) ON DELETE CASCADE,
  template_revision_id uuid REFERENCES document_template_revisions(id) ON DELETE RESTRICT,
  input_sha256 text NOT NULL CHECK (length(input_sha256)=64),
  idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key)=64),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','uncertain')),
  claimed_by text,
  claim_token uuid,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_expires_at timestamptz,
  docx_artifact_id uuid REFERENCES artifacts(id) ON DELETE SET NULL,
  pdf_artifact_id uuid REFERENCES artifacts(id) ON DELETE SET NULL,
  started_at timestamptz,
  finished_at timestamptz,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_render_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  render_run_id uuid NOT NULL REFERENCES document_render_runs(id) ON DELETE CASCADE,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  claim_token uuid NOT NULL UNIQUE,
  claimed_by text NOT NULL,
  status text NOT NULL CHECK (status IN ('running','completed','failed','uncertain','superseded')),
  lease_expires_at timestamptz,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_message text,
  UNIQUE(render_run_id,attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_document_render_attempts_open
  ON document_render_attempts(render_run_id,status,lease_expires_at) WHERE status='running';

ALTER TABLE generated_document_artifacts
  ADD COLUMN IF NOT EXISTS artifact_id uuid REFERENCES artifacts(id) ON DELETE SET NULL;
ALTER TABLE human_review_artifacts
  ADD COLUMN IF NOT EXISTS artifact_id uuid REFERENCES artifacts(id) ON DELETE SET NULL;

-- Backfill registry identities without moving any existing bytes.
INSERT INTO artifacts(application_id,artifact_kind,storage_key,filename,mime_type,size_bytes,sha256,provenance_json,created_at)
SELECT gda.application_id,gda.artifact_type,gda.file_path,gda.filename,NULL,NULL,gda.sha256,
       jsonb_build_object('generated_document_artifact_id',gda.id,'generated_document_id',gda.generated_document_id),gda.created_at
  FROM generated_document_artifacts gda
ON CONFLICT (storage_backend,storage_key,sha256) DO NOTHING;
UPDATE generated_document_artifacts gda SET artifact_id=a.id
  FROM artifacts a
 WHERE gda.artifact_id IS NULL AND a.storage_backend='filesystem'
   AND a.storage_key=gda.file_path AND a.sha256=gda.sha256;

INSERT INTO artifacts(application_id,artifact_kind,storage_key,filename,mime_type,size_bytes,sha256,provenance_json,created_at)
SELECT hri.application_id,hra.artifact_kind,hra.file_path,hra.filename,hra.mime_type,NULL,hra.sha256,
       jsonb_build_object('human_review_artifact_id',hra.id,'review_item_id',hra.review_item_id),hra.created_at
  FROM human_review_artifacts hra JOIN human_review_items hri ON hri.id=hra.review_item_id
ON CONFLICT (storage_backend,storage_key,sha256) DO NOTHING;
UPDATE human_review_artifacts hra SET artifact_id=a.id
  FROM artifacts a
 WHERE hra.artifact_id IS NULL AND a.storage_backend='filesystem'
   AND a.storage_key=hra.file_path AND a.sha256=hra.sha256;

COMMIT;
