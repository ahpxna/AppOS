-- 018_raw_file_source_paths.sql
-- Real source file path layer for profile evidence.
-- Allows agents/verifiers to trace facts back to original local files and parsed text files.

INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES (
  'source_file_path_resolver',
  'service',
  'profile',
  'Resolve raw_files records to original local source paths, parsed text paths, file size, and path verification status.',
  false,
  'prototype',
  'Required before evidence-grounded verification can reliably trace profile facts back to original PDF/DOCX/TXT sources.'
)
ON CONFLICT (name)
DO UPDATE SET
  component_type = EXCLUDED.component_type,
  layer = EXCLUDED.layer,
  purpose = EXCLUDED.purpose,
  trainable = EXCLUDED.trainable,
  status = EXCLUDED.status,
  notes = EXCLUDED.notes,
  updated_at = now();

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS original_local_path text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS parsed_text_path text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS file_size_bytes bigint;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS path_status text DEFAULT 'unknown';

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS path_error text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS last_path_verified_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_raw_files_original_local_path
ON raw_files(original_local_path);

CREATE INDEX IF NOT EXISTS idx_raw_files_parsed_text_path
ON raw_files(parsed_text_path);

CREATE INDEX IF NOT EXISTS idx_raw_files_path_status
ON raw_files(path_status);

CREATE OR REPLACE VIEW v_raw_file_source_paths AS
SELECT
  id,
  left(id::text, 8) AS short_id,
  file_name,
  file_type,
  source,
  parse_status,
  file_role,
  evidence_weight,
  allow_profile_fact_promotion,
  storage_url,
  original_local_path,
  parsed_text_path,
  file_size_bytes,
  sha256,
  path_status,
  path_error,
  last_path_verified_at,
  uploaded_at
FROM raw_files
ORDER BY
  CASE path_status
    WHEN 'missing_original' THEN 1
    WHEN 'sha_mismatch' THEN 2
    WHEN 'missing_parsed_text' THEN 3
    WHEN 'verified' THEN 4
    ELSE 5
  END,
  file_name;
