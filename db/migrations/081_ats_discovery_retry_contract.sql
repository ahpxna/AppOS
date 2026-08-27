BEGIN;

ALTER TABLE ats_companies
  ADD COLUMN IF NOT EXISTS next_retry_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_error_kind text;

CREATE INDEX IF NOT EXISTS idx_ats_companies_next_retry
  ON ats_companies(enabled, next_retry_at NULLS FIRST);

COMMENT ON COLUMN ats_companies.next_retry_at IS
  'Automatic discovery cooldown. Transient failures back off; companies are never disabled by an automatic failure count.';

COMMIT;
