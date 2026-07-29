-- 022_ocr_pipeline.sql
-- Real OCR layer for Profile Knowledge Layer.
-- OCR is part of File Type Classifier -> Parser/OCR -> Chunker.

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
  'profile_ocr_engine',
  'service',
  'profile',
  'Audit files for OCR need and extract OCR text from scanned PDFs/images into traceable page-level OCR records.',
  false,
  'active',
  'OCR results do not overwrite chunks automatically; they are stored as evidence and can be used by parser/chunker regeneration.'
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
ADD COLUMN IF NOT EXISTS ocr_status text DEFAULT 'not_evaluated';

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_required boolean DEFAULT false;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_engine text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_text_path text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_page_count integer;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_char_count integer;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS ocr_error text;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS last_ocr_at timestamptz;

CREATE TABLE IF NOT EXISTS raw_file_ocr_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  raw_file_id uuid NOT NULL REFERENCES raw_files(id) ON DELETE CASCADE,

  run_mode text NOT NULL,
  -- audit / ocr

  engine text NOT NULL,
  status text NOT NULL,
  -- completed / failed / skipped

  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  error_message text,

  started_at timestamptz DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_raw_file_ocr_runs_raw_file
ON raw_file_ocr_runs(raw_file_id);

CREATE INDEX IF NOT EXISTS idx_raw_file_ocr_runs_status
ON raw_file_ocr_runs(status);

CREATE TABLE IF NOT EXISTS raw_file_ocr_pages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  raw_file_id uuid NOT NULL REFERENCES raw_files(id) ON DELETE CASCADE,
  ocr_run_id uuid NOT NULL REFERENCES raw_file_ocr_runs(id) ON DELETE CASCADE,

  page_number integer NOT NULL,

  text_content text,
  text_char_count integer,
  confidence numeric,

  image_path text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(raw_file_id, ocr_run_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_raw_file_ocr_pages_raw_file
ON raw_file_ocr_pages(raw_file_id);

CREATE INDEX IF NOT EXISTS idx_raw_file_ocr_pages_run
ON raw_file_ocr_pages(ocr_run_id);

CREATE OR REPLACE VIEW v_profile_ocr_status AS
SELECT
  rf.id,
  left(rf.id::text, 8) AS short_id,
  rf.file_name,
  rf.file_type,
  rf.file_role,
  rf.parse_status,
  rf.path_status,
  rf.original_local_path,
  rf.parsed_text_path,
  rf.ocr_status,
  rf.ocr_required,
  rf.ocr_engine,
  rf.ocr_text_path,
  rf.ocr_page_count,
  rf.ocr_char_count,
  rf.ocr_error,
  rf.last_ocr_at,
  COALESCE(chunk_stats.chunk_count, 0) AS chunk_count,
  COALESCE(chunk_stats.chunk_char_count, 0) AS chunk_char_count
FROM raw_files rf
LEFT JOIN (
  SELECT
    file_id,
    count(*) AS chunk_count,
    sum(length(COALESCE(text_content, ''))) AS chunk_char_count
  FROM profile_chunks
  GROUP BY file_id
) chunk_stats
  ON chunk_stats.file_id = rf.id
WHERE rf.source = 'local_profile_ingestion'
ORDER BY
  CASE rf.ocr_status
    WHEN 'required' THEN 1
    WHEN 'failed' THEN 2
    WHEN 'completed' THEN 3
    WHEN 'not_required' THEN 4
    ELSE 5
  END,
  rf.file_name;
