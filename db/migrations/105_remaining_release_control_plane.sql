BEGIN;

ALTER TABLE company_research_cache ADD COLUMN IF NOT EXISTS identity_key text;
UPDATE company_research_cache
   SET identity_key = CASE
       WHEN nullif(trim(company_domain),'') IS NOT NULL
         THEN 'domain:' || lower(regexp_replace(trim(company_domain), '^www\.', ''))
       ELSE 'name:' || lower(regexp_replace(trim(company_name), '\s+', ' ', 'g'))
   END
 WHERE identity_key IS NULL OR trim(identity_key) = '';

WITH ranked AS (
  SELECT id,row_number() OVER (
    PARTITION BY identity_key
    ORDER BY last_refreshed_at DESC NULLS LAST,created_at DESC NULLS LAST,id DESC
  ) AS ordinal
  FROM company_research_cache
)
DELETE FROM company_research_cache c USING ranked r
 WHERE c.id=r.id AND r.ordinal>1;

ALTER TABLE company_research_cache ALTER COLUMN identity_key SET NOT NULL;
ALTER TABLE company_research_cache DROP CONSTRAINT IF EXISTS company_research_cache_identity_key_nonblank;
ALTER TABLE company_research_cache ADD CONSTRAINT company_research_cache_identity_key_nonblank
  CHECK (identity_key ~ '^(domain|name):[^[:space:]].*$');
CREATE UNIQUE INDEX IF NOT EXISTS company_research_cache_identity_key_uq
  ON company_research_cache(identity_key);

ALTER TABLE applications ADD COLUMN IF NOT EXISTS orchestrator_failure_streak integer NOT NULL DEFAULT 0;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS orchestrator_next_attempt_at timestamptz;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS orchestrator_blocker_kind text;

INSERT INTO pipeline_transitions(from_step,to_step,automated,note,transition_kind)
VALUES ('docs_failed_qa','docs_verified',true,'Candidate-requested revision passed truth QA.','automated')
ON CONFLICT (from_step,to_step) DO UPDATE
SET automated=EXCLUDED.automated, note=EXCLUDED.note,
    transition_kind=EXCLUDED.transition_kind;

INSERT INTO component_registry(name,component_type,layer,purpose,trainable,status,notes)
VALUES ('remaining_release_control_plane','workflow','L1',
        'Exact company identity, durable orchestrator backoff, and document QA recovery.',
        false,'active','release-control-plane-v1')
ON CONFLICT (name) DO UPDATE SET status='active',notes=EXCLUDED.notes,updated_at=now();

COMMIT;
