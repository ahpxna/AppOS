-- =========================================================
-- 045 -- Profile-driven discovery terms and user-owned LinkedIn intake
--
-- LinkedIn intake stays user-initiated and read-only.  All captured jobs
-- use the existing applications table so L1/L5/L6 gates remain unchanged.
-- =========================================================

BEGIN;

CREATE OR REPLACE VIEW v_profile_search_terms AS
WITH terms AS (
  SELECT pc.id AS capability_id, 'capability_name'::text AS term_kind,
         pc.capability_name AS term
  FROM profile_capabilities pc
  WHERE pc.status = 'approved'

  UNION ALL
  SELECT pc.id, 'tool_tag', unnest(pc.tool_tags)
  FROM profile_capabilities pc
  WHERE pc.status = 'approved'

  UNION ALL
  SELECT pc.id, 'competency_tag', unnest(pc.competency_tags)
  FROM profile_capabilities pc
  WHERE pc.status = 'approved'

  UNION ALL
  SELECT pc.id, 'role_family', unnest(pc.role_families)
  FROM profile_capabilities pc
  WHERE pc.status = 'approved'
)
SELECT DISTINCT lower(btrim(term)) AS term
FROM terms
WHERE length(btrim(term)) >= 2
  AND length(btrim(term)) <= 100;

COMMENT ON VIEW v_profile_search_terms IS
  'Discovery terms derived only from approved profile capabilities. They are '
  'for transparent lead ranking/search planning, not automated fit decisions.';

CREATE INDEX IF NOT EXISTS idx_applications_searchable_text
ON applications USING gin (
  to_tsvector('simple', coalesce(job_title, '') || ' ' || coalesce(jd_text, ''))
);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('profile_job_search', 'service', 'L0',
   'Generate human-operated search terms from approved profile capabilities and rank intaked jobs transparently.',
   false, 'active',
   'No LinkedIn scraping, credential use, cursor simulation, or automatic applications.', now(), now()),
  ('linkedin_user_intake', 'service', 'L0',
   'Ingest user-reviewed LinkedIn export or one user-pasted URL through the normal applications intake gate.',
   false, 'active',
   'Read-only, user-initiated intake only; downstream truth and approval gates apply unchanged.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
