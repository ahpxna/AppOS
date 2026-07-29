-- 026_deprecate_atom_fact_pipeline_and_create_profile_assets.sql
-- New truth direction: profile_assets, not tiny atom facts.
-- Old atom-fact extraction remains as supporting evidence/search data only.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS profile_pipeline_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_key text UNIQUE NOT NULL,
  decision_text text NOT NULL,
  affected_components text[] DEFAULT '{}'::text[],
  created_at timestamptz DEFAULT now()
);

INSERT INTO profile_pipeline_decisions (
  decision_key,
  decision_text,
  affected_components
)
VALUES (
  'deprecate_atom_fact_truth_pipeline_2026_04_27',
  'Do not use tiny candidate_profile_facts as the primary profile truth layer. Rich synthesized source documents must compile into profile_assets with multi-section evidence, job-oriented narratives, resume/interview variants, and overclaim controls.',
  ARRAY[
    'candidate_fact_extractor',
    'candidate_fact_embedder',
    'semantic_dedup_worker',
    'profile_fact_verifier_rewriter',
    'promote_clean_facts'
  ]
)
ON CONFLICT (decision_key)
DO UPDATE SET
  decision_text = EXCLUDED.decision_text,
  affected_components = EXCLUDED.affected_components;

UPDATE candidate_fact_dedup_groups
SET
  group_status = 'superseded',
  updated_at = now(),
  reasoning = concat_ws(
    E'\n',
    reasoning,
    'Superseded by architecture decision: atom-fact dedup is no longer the primary truth path. Use profile_assets compiled from source-level synthesized documents.'
  )
WHERE group_status = 'pending_review';

UPDATE component_registry
SET
  status = CASE
    WHEN name IN ('candidate_fact_extractor', 'candidate_fact_embedder', 'semantic_dedup_worker', 'profile_fact_verifier_rewriter')
      THEN 'deprecated'
    ELSE status
  END,
  notes = concat_ws(
    E'\n',
    notes,
    CASE
      WHEN name IN ('candidate_fact_extractor', 'candidate_fact_embedder', 'semantic_dedup_worker', 'profile_fact_verifier_rewriter')
      THEN 'Deprecated as primary truth path. May only be used as supporting atom/evidence search, not as source of profile truth.'
      ELSE NULL
    END
  ),
  updated_at = now()
WHERE name IN (
  'candidate_fact_extractor',
  'candidate_fact_embedder',
  'semantic_dedup_worker',
  'profile_fact_verifier_rewriter'
);

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
  'portfolio_asset_compiler',
  'service',
  'profile',
  'Compile rich synthesized profile documents into job-oriented profile assets with multi-section evidence, role relevance, resume/interview variants, and overclaim controls.',
  true,
  'active',
  'Primary Profile Knowledge Layer path. Replaces tiny atom facts as the main truth representation.'
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

CREATE TABLE IF NOT EXISTS profile_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  asset_title text NOT NULL,
  asset_type text NOT NULL,
  -- tool_workflow_asset / project_asset / strategic_course_asset / course_competency_asset / research_asset / source_document_asset

  abstraction_level text NOT NULL DEFAULT 'source_preserving_asset',
  -- source_preserving_asset / synthesized_profile_asset / job_oriented_asset

  status text NOT NULL DEFAULT 'draft',
  -- draft / needs_review / approved / rejected / superseded

  canonical_narrative text NOT NULL,
  job_oriented_summary text,
  resume_bullet_bank text,
  interview_story text,
  cover_letter_positioning text,

  role_families text[] NOT NULL DEFAULT '{}'::text[],
  competency_tags text[] NOT NULL DEFAULT '{}'::text[],
  tool_tags text[] NOT NULL DEFAULT '{}'::text[],
  project_tags text[] NOT NULL DEFAULT '{}'::text[],

  do_not_overclaim_rules text[] NOT NULL DEFAULT '{}'::text[],

  created_from_raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  compiler_version text NOT NULL,
  source_strategy text NOT NULL DEFAULT 'source_preserving_compilation',

  confidence numeric DEFAULT 0.80,

  review_note text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(compiler_version, created_from_raw_file_id, asset_title)
);

CREATE INDEX IF NOT EXISTS idx_profile_assets_status
ON profile_assets(status);

CREATE INDEX IF NOT EXISTS idx_profile_assets_type
ON profile_assets(asset_type);

CREATE INDEX IF NOT EXISTS idx_profile_assets_source_file
ON profile_assets(created_from_raw_file_id);

CREATE TABLE IF NOT EXISTS profile_asset_evidence_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_asset_id uuid NOT NULL REFERENCES profile_assets(id) ON DELETE CASCADE,

  raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,

  evidence_rank integer NOT NULL,
  evidence_type text NOT NULL,
  -- purpose / narrative / category_map / methodology / result / positioning / resume_phrase / limitation / source_excerpt

  section_title text,
  evidence_text text NOT NULL,

  source_file_name text,
  source_path text,
  page_hint text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(profile_asset_id, evidence_rank)
);

CREATE INDEX IF NOT EXISTS idx_profile_asset_evidence_asset
ON profile_asset_evidence_items(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_asset_evidence_raw_file
ON profile_asset_evidence_items(raw_file_id);

CREATE TABLE IF NOT EXISTS profile_asset_outputs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_asset_id uuid NOT NULL REFERENCES profile_assets(id) ON DELETE CASCADE,

  output_type text NOT NULL,
  -- resume_bullet / cover_letter_paragraph / interview_story / linkedin_summary / job_fit_evidence

  role_family text,
  target_job_title text,

  output_text text NOT NULL,

  status text NOT NULL DEFAULT 'draft',
  generator_version text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_asset_outputs_asset
ON profile_asset_outputs(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_asset_outputs_type
ON profile_asset_outputs(output_type);

CREATE OR REPLACE VIEW v_profile_asset_review AS
SELECT
  pa.id AS profile_asset_id,
  left(pa.id::text, 8) AS asset_short_id,
  pa.asset_title,
  pa.asset_type,
  pa.abstraction_level,
  pa.status,
  pa.confidence,
  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.project_tags,
  rf.file_name AS source_file,
  rf.file_role,
  pa.canonical_narrative,
  pa.job_oriented_summary,
  pa.resume_bullet_bank,
  pa.interview_story,
  pa.do_not_overclaim_rules,
  count(e.id) AS evidence_item_count,
  pa.created_at,
  pa.updated_at
FROM profile_assets pa
LEFT JOIN raw_files rf
  ON rf.id = pa.created_from_raw_file_id
LEFT JOIN profile_asset_evidence_items e
  ON e.profile_asset_id = pa.id
GROUP BY pa.id, rf.file_name, rf.file_role
ORDER BY
  CASE pa.status
    WHEN 'needs_review' THEN 1
    WHEN 'draft' THEN 2
    WHEN 'approved' THEN 3
    ELSE 4
  END,
  pa.updated_at DESC;

CREATE OR REPLACE VIEW v_profile_asset_status AS
SELECT
  asset_type,
  abstraction_level,
  status,
  count(*) AS asset_count,
  sum(CASE WHEN job_oriented_summary IS NOT NULL AND length(btrim(job_oriented_summary)) > 0 THEN 1 ELSE 0 END) AS assets_with_job_summary,
  sum(CASE WHEN resume_bullet_bank IS NOT NULL AND length(btrim(resume_bullet_bank)) > 0 THEN 1 ELSE 0 END) AS assets_with_resume_bullets
FROM profile_assets
GROUP BY asset_type, abstraction_level, status
ORDER BY asset_type, abstraction_level, status;
