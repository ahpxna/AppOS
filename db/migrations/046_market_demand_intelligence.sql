-- =========================================================
-- 046 -- Market-demand intelligence from captured job descriptions
--
-- Each signal retains an exact excerpt from its source JD. Aggregates are
-- observations, not an assessment of candidate quality or a claim that a
-- keyword is universally required.
-- =========================================================

BEGIN;

CREATE TABLE IF NOT EXISTS market_requirement_signals (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id      uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  role_family         text NOT NULL DEFAULT 'other',
  normalized_keyword  text NOT NULL,
  display_keyword     text NOT NULL,
  requirement_type    text NOT NULL,
  -- technical_tool / technical_standard / technical_concept / model_requirement
  evidence_excerpt    text NOT NULL,
  extractor_version   text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE(application_id, normalized_keyword, requirement_type)
);

CREATE INDEX IF NOT EXISTS idx_market_requirement_signals_keyword
  ON market_requirement_signals(normalized_keyword);
CREATE INDEX IF NOT EXISTS idx_market_requirement_signals_role
  ON market_requirement_signals(role_family, normalized_keyword);
CREATE INDEX IF NOT EXISTS idx_market_requirement_signals_application
  ON market_requirement_signals(application_id);

CREATE TABLE IF NOT EXISTS market_project_ideas (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_family         text NOT NULL DEFAULT 'other',
  normalized_keyword  text NOT NULL,
  title               text NOT NULL,
  learning_scope      text NOT NULL,
  evidence_goal       text NOT NULL,
  status              text NOT NULL DEFAULT 'proposed',
  -- proposed / selected / building / completed / archived
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE(role_family, normalized_keyword)
);

CREATE OR REPLACE VIEW v_market_keyword_demands AS
SELECT
  m.role_family,
  m.normalized_keyword,
  min(m.display_keyword) AS display_keyword,
  m.requirement_type,
  count(*) AS posting_count,
  count(DISTINCT a.company) AS company_count,
  array_agg(DISTINCT a.company ORDER BY a.company) FILTER (WHERE a.company IS NOT NULL) AS companies,
  max(m.created_at) AS last_seen_at
FROM market_requirement_signals m
JOIN applications a ON a.id = m.application_id
GROUP BY m.role_family, m.normalized_keyword, m.requirement_type;

COMMENT ON VIEW v_market_keyword_demands IS
  'Observed technical-requirement keyword counts from JobOS-captured JDs, grouped by role. Every observation has an evidence excerpt in market_requirement_signals.';

CREATE OR REPLACE VIEW v_market_skill_gaps AS
SELECT d.*
FROM v_market_keyword_demands d
WHERE NOT EXISTS (
  SELECT 1 FROM v_profile_search_terms p
  WHERE p.term = d.normalized_keyword
);

COMMENT ON VIEW v_market_skill_gaps IS
  'Observed market keywords not present as approved profile search terms. This is a project-learning backlog input, not evidence that the candidate has a deficit.';

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('market_demand_intelligence', 'service', 'L1',
   'Extract evidence-bearing technical requirements from captured JDs and aggregate demand by role/company.',
   false, 'active',
   'Deterministic extraction plus optional existing fit-analysis phrases; preserves JD excerpts and never creates candidate claims.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
