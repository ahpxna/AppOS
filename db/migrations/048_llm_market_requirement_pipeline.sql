-- =========================================================
-- 048 -- Evidence-grounded LLM market-requirement pipeline
--
-- Every captured JD is queued irrespective of its application outcome.
-- The worker records only requirements whose supplied quotation can be
-- located in the source JD; it never turns an LLM's unstated inference into
-- a market signal or a candidate claim.
-- =========================================================

BEGIN;

ALTER TABLE market_requirement_signals
  ADD COLUMN IF NOT EXISTS importance text NOT NULL DEFAULT 'mentioned',
  ADD COLUMN IF NOT EXISTS extraction_method text NOT NULL DEFAULT 'legacy_static';

COMMENT ON COLUMN market_requirement_signals.importance IS
  'How the JD frames the requirement: required, preferred, mentioned, or unknown.';
COMMENT ON COLUMN market_requirement_signals.extraction_method IS
  'Pipeline that created the signal; LLM records still require an exact JD quotation.';

CREATE TABLE IF NOT EXISTS market_requirement_extraction_runs (
  application_id       uuid PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
  source_jd_hash       text NOT NULL,
  status               text NOT NULL DEFAULT 'pending',
  -- pending / running / succeeded / failed
  attempt_count        integer NOT NULL DEFAULT 0,
  signal_count         integer,
  extractor_version    text,
  backend              text,
  model_name           text,
  last_error           text,
  queued_at            timestamptz NOT NULL DEFAULT now(),
  started_at           timestamptz,
  completed_at         timestamptz,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT market_requirement_extraction_runs_status_check
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_market_requirement_extraction_runs_pending
  ON market_requirement_extraction_runs(status, queued_at);

CREATE OR REPLACE FUNCTION queue_market_requirement_extraction()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- A short/empty JD cannot yield source-grounded requirements. It is not
  -- silently marked successful, so operators can see it was intentionally
  -- not queued.
  IF NEW.jd_text IS NULL OR length(btrim(NEW.jd_text)) < 80 THEN
    RETURN NEW;
  END IF;

  INSERT INTO market_requirement_extraction_runs
    (application_id, source_jd_hash, status, attempt_count, signal_count,
     extractor_version, backend, model_name, last_error, queued_at,
     started_at, completed_at, updated_at)
  VALUES
    (NEW.id, NEW.jd_hash, 'pending', 0, NULL, NULL, NULL, NULL, NULL,
     now(), NULL, NULL, now())
  ON CONFLICT (application_id) DO UPDATE
  SET source_jd_hash = EXCLUDED.source_jd_hash,
      status = CASE
        WHEN market_requirement_extraction_runs.source_jd_hash
             IS DISTINCT FROM EXCLUDED.source_jd_hash THEN 'pending'
        ELSE market_requirement_extraction_runs.status
      END,
      attempt_count = CASE
        WHEN market_requirement_extraction_runs.source_jd_hash
             IS DISTINCT FROM EXCLUDED.source_jd_hash THEN 0
        ELSE market_requirement_extraction_runs.attempt_count
      END,
      signal_count = CASE
        WHEN market_requirement_extraction_runs.source_jd_hash
             IS DISTINCT FROM EXCLUDED.source_jd_hash THEN NULL
        ELSE market_requirement_extraction_runs.signal_count
      END,
      last_error = CASE
        WHEN market_requirement_extraction_runs.source_jd_hash
             IS DISTINCT FROM EXCLUDED.source_jd_hash THEN NULL
        ELSE market_requirement_extraction_runs.last_error
      END,
      queued_at = CASE
        WHEN market_requirement_extraction_runs.source_jd_hash
             IS DISTINCT FROM EXCLUDED.source_jd_hash THEN now()
        ELSE market_requirement_extraction_runs.queued_at
      END,
      updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_queue_market_requirement_extraction ON applications;
CREATE TRIGGER trg_queue_market_requirement_extraction
AFTER INSERT OR UPDATE OF jd_text, jd_hash ON applications
FOR EACH ROW EXECUTE FUNCTION queue_market_requirement_extraction();

-- Existing captured JDs become pending work too. This deliberately has no
-- filter on application step/status: discarded posts are market evidence.
INSERT INTO market_requirement_extraction_runs
  (application_id, source_jd_hash, status, queued_at, updated_at)
SELECT id, jd_hash, 'pending', now(), now()
FROM applications
WHERE jd_text IS NOT NULL AND length(btrim(jd_text)) >= 80
ON CONFLICT (application_id) DO NOTHING;

CREATE OR REPLACE VIEW v_market_requirement_extraction_queue AS
SELECT
  r.application_id,
  r.status,
  r.attempt_count,
  r.signal_count,
  r.extractor_version,
  r.backend,
  r.model_name,
  r.last_error,
  r.queued_at,
  r.started_at,
  r.completed_at,
  a.company,
  a.job_title,
  a.current_step,
  a.status AS application_status
FROM market_requirement_extraction_runs r
JOIN applications a ON a.id = r.application_id;

COMMENT ON VIEW v_market_requirement_extraction_queue IS
  'LLM market-requirement extraction status for every captured JD, including filtered_out and fit_rejected applications.';

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('llm_market_requirement_pipeline', 'service', 'L1',
   'Queues every captured JD and extracts evidence-grounded skills, tools, methods, standards, and qualifications independent of application fit.',
   false, 'active',
   'No static technology allowlist. The worker stores an item only when its LLM-provided quotation is located in the captured JD.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
