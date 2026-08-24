-- =========================================================
-- 064 -- Versioned profile source documents and immutable revisions
--
-- A filename/Office Created timestamp is not an identity boundary.  This
-- migration records a logical document separately from each content-SHA
-- revision while preserving embedded and filesystem timestamps as provenance.
-- =========================================================
BEGIN;

CREATE TABLE IF NOT EXISTS profile_source_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  logical_source_key text NOT NULL UNIQUE,
  display_name text NOT NULL,
  source_kind text NOT NULL DEFAULT 'other'
    CHECK (source_kind IN ('docx','pdf','txt','md','other')),
  authority_class text NOT NULL DEFAULT 'unknown'
    CHECK (authority_class IN (
      'official_document','profile_document','project_document','reference_document',
      'guidance_document','unknown'
    )),
  current_revision_id uuid,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','missing','superseded','excluded')),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profile_source_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_document_id uuid NOT NULL
    REFERENCES profile_source_documents(id) ON DELETE CASCADE,
  raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  content_sha256 text NOT NULL,
  embedded_created_at timestamptz,
  embedded_modified_at timestamptz,
  filesystem_birth_at timestamptz,
  filesystem_modified_at timestamptz,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  ingested_at timestamptz NOT NULL DEFAULT now(),
  parser_fingerprint text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'current'
    CHECK (status IN ('current','superseded','missing','excluded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_document_id, content_sha256)
);

ALTER TABLE profile_source_documents
  ADD CONSTRAINT fk_profile_source_documents_current_revision
  FOREIGN KEY (current_revision_id)
  REFERENCES profile_source_revisions(id) ON DELETE SET NULL;

ALTER TABLE raw_files
  ADD COLUMN IF NOT EXISTS source_revision_id uuid
    REFERENCES profile_source_revisions(id) ON DELETE SET NULL;

ALTER TABLE profile_documents
  ADD COLUMN IF NOT EXISTS source_revision_id uuid
    REFERENCES profile_source_revisions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_profile_source_revisions_document
  ON profile_source_revisions(source_document_id, status, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_profile_source_revisions_sha
  ON profile_source_revisions(content_sha256);
CREATE INDEX IF NOT EXISTS idx_raw_files_source_revision
  ON raw_files(source_revision_id);
CREATE INDEX IF NOT EXISTS idx_profile_documents_source_revision
  ON profile_documents(source_revision_id);

CREATE OR REPLACE VIEW v_profile_source_freshness AS
SELECT
  d.id AS source_document_id,
  d.logical_source_key,
  d.display_name,
  d.source_kind,
  d.authority_class,
  d.status,
  d.current_revision_id,
  r.content_sha256,
  r.embedded_created_at,
  r.embedded_modified_at,
  r.filesystem_birth_at,
  r.filesystem_modified_at,
  r.first_seen_at AS revision_first_seen_at,
  r.ingested_at,
  r.status AS revision_status,
  d.last_seen_at
FROM profile_source_documents d
LEFT JOIN profile_source_revisions r ON r.id = d.current_revision_id;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('profile_source_revision_tracker', 'service', 'profile',
   'Version logical DOCX/PDF/text profile sources by content SHA while retaining embedded/filesystem timestamps as provenance.',
   false, 'active',
   'Content SHA, not Office/PDF Created metadata, is the revision identity boundary.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
