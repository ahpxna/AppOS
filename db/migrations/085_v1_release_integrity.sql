-- 085 -- V1 release integrity: full posting provenance, generalized ATS locators,
-- candidate-domain catalog separation, and safe upgrade compatibility.
BEGIN;

-- A source revision is the whole normalized posting observation, not only the
-- JD body. Employers can change location/work mode/title without changing the
-- description; downstream snapshots must remain immutable while the change is
-- still auditable.
ALTER TABLE job_posting_source_revisions
  ADD COLUMN IF NOT EXISTS source_content_sha256 char(64);

UPDATE job_posting_source_revisions
SET source_content_sha256 = encode(digest(
      concat_ws(chr(31),
        coalesce(canonical_url,''),
        regexp_replace(trim(coalesce(company,'')), '\s+', ' ', 'g'),
        lower(coalesce(jd_hash::text,'')),
        regexp_replace(trim(coalesce(job_title,'')), '\s+', ' ', 'g'),
        regexp_replace(trim(coalesce(location,'')), '\s+', ' ', 'g'),
        trim(coalesce(source_job_id,'')),
        CASE regexp_replace(lower(trim(coalesce(work_mode,''))), '[\s_-]+', ' ', 'g')
          WHEN 'remote' THEN 'remote'
          WHEN 'fully remote' THEN 'remote'
          WHEN 'remote only' THEN 'remote'
          WHEN 'work from home' THEN 'remote'
          WHEN 'telecommute' THEN 'remote'
          WHEN 'telecommuting' THEN 'remote'
          WHEN 'hybrid' THEN 'hybrid'
          WHEN 'hybrid remote' THEN 'hybrid'
          WHEN 'flexible hybrid' THEN 'hybrid'
          WHEN 'hybrid work' THEN 'hybrid'
          WHEN 'on site' THEN 'on_site'
          WHEN 'onsite' THEN 'on_site'
          WHEN 'in office' THEN 'on_site'
          WHEN 'office' THEN 'on_site'
          WHEN 'office based' THEN 'on_site'
          WHEN 'in person' THEN 'on_site'
          ELSE 'unknown'
        END
      ),
      'sha256'
    ), 'hex')
WHERE source_content_sha256 IS NULL;

ALTER TABLE job_posting_source_revisions
  ALTER COLUMN source_content_sha256 SET NOT NULL;

ALTER TABLE job_posting_source_revisions
  DROP CONSTRAINT IF EXISTS job_posting_source_revisions_application_id_source_name_jd_hash_key;

DROP INDEX IF EXISTS uq_job_posting_source_revision_content;
CREATE UNIQUE INDEX uq_job_posting_source_revision_content
  ON job_posting_source_revisions(application_id, source_name, source_content_sha256);

-- Legacy ATS discovery modeled every vendor as a Greenhouse-like slug. Native
-- APIs still require a tenant key, while structured/browser discovery uses an
-- official source URL. Keep the old column for compatibility but stop forcing
-- fake slugs for Workday/iCIMS/Oracle/custom career sites.
ALTER TABLE ats_companies ALTER COLUMN slug DROP NOT NULL;
ALTER TABLE ats_companies DROP CONSTRAINT IF EXISTS ats_companies_ats_platform_slug_key;

ALTER TABLE ats_companies DROP CONSTRAINT IF EXISTS ats_companies_locator_check;
ALTER TABLE ats_companies ADD CONSTRAINT ats_companies_locator_check
  CHECK (nullif(trim(coalesce(slug,'')),'') IS NOT NULL
      OR nullif(trim(coalesce(source_url,'')),'') IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ats_companies_platform_slug
  ON ats_companies(ats_platform, slug)
  WHERE nullif(trim(coalesce(slug,'')),'') IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ats_companies_platform_source_url
  ON ats_companies(ats_platform, source_url)
  WHERE nullif(trim(coalesce(source_url,'')),'') IS NOT NULL;

-- Registry candidate domains are classification/discovery data, not global
-- browser authority. Preserve the catalog separately and remove the implicit
-- trust escalation introduced by older ATS registry seeds. Human-approved,
-- application-scoped trusts remain in application_scoped_domain_trusts.
CREATE TABLE IF NOT EXISTS ats_candidate_domains (
  domain text PRIMARY KEY,
  category text NOT NULL DEFAULT 'ats',
  notes text,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ats_candidate_domains(domain, category, notes, enabled)
SELECT lower(domain), coalesce(category,'ats'), notes, enabled
FROM allowed_domains
WHERE category='ats' OR category='ats_candidate_catalog'
ON CONFLICT (domain) DO UPDATE SET
  category=EXCLUDED.category,
  notes=coalesce(EXCLUDED.notes, ats_candidate_domains.notes),
  enabled=EXCLUDED.enabled,
  updated_at=now();

UPDATE allowed_domains
SET category='ats_candidate_catalog',
    notes=coalesce(notes,'ATS candidate-domain catalog; application browser authority requires a scoped human trust.')
WHERE category='ats';

COMMENT ON TABLE ats_candidate_domains IS
  'ATS hostname catalog for detection/discovery only. Presence here never grants browser navigation/write authority.';

COMMENT ON COLUMN allowed_domains.category IS
  'Global browser policy category. ats_candidate_catalog rows are classification-only and are NOT browser authority.';

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('canonical_source_observation_v2','service','L0',
   'Freeze downstream application evidence while recording full posting metadata revisions.',
   false,'active','Revision identity binds JD hash, company, title, location, work mode, URL and source job id.',now(),now()),
  ('ats_candidate_domain_catalog','control','L7',
   'Separate ATS detection domains from application-scoped browser trust.',
   false,'active','Candidate-domain recognition does not authorize browser writes or navigation.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose, status=EXCLUDED.status, notes=EXCLUDED.notes, updated_at=now();

COMMIT;
