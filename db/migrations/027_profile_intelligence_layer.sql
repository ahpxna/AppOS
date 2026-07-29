-- 027_profile_intelligence_layer.sql
-- Extend only L4 Profile Knowledge Layer.
-- Keep original multi-layer JobOS architecture intact.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- =========================================================
-- L4.1 Profile documents
-- One record per raw file or major subdocument.
-- =========================================================

CREATE TABLE IF NOT EXISTS profile_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,

  document_title text NOT NULL,
  document_type text NOT NULL DEFAULT 'unknown',
  -- resume / transcript / course_profile / project_profile / tool_mapping /
  -- strategic_profile / research_profile / career_guidance / source_bundle / unknown

  document_purpose text,
  source_role text,
  -- primary_profile_evidence / enriched_profile_evidence / project_artifact_evidence /
  -- course_reference_material / career_strategy_guidance / unclassified

  source_quality numeric DEFAULT 0.80,

  contains_profile_evidence boolean NOT NULL DEFAULT true,
  contains_guidance_only boolean NOT NULL DEFAULT false,

  language text DEFAULT 'en',
  parser_used text,
  parsed_text_path text,
  original_local_path text,

  document_summary text,
  structure_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  risk_notes text[] NOT NULL DEFAULT '{}'::text[],

  mapper_version text,
  mapper_model text,

  status text NOT NULL DEFAULT 'mapped',
  -- mapped / needs_review / approved / rejected / superseded

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(raw_file_id, document_title)
);

CREATE INDEX IF NOT EXISTS idx_profile_documents_raw_file
ON profile_documents(raw_file_id);

CREATE INDEX IF NOT EXISTS idx_profile_documents_type
ON profile_documents(document_type);

CREATE INDEX IF NOT EXISTS idx_profile_documents_status
ON profile_documents(status);

-- =========================================================
-- L4.2 Document sections
-- Section/page-aware structure preserving the source narrative.
-- =========================================================

CREATE TABLE IF NOT EXISTS profile_document_sections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_document_id uuid NOT NULL REFERENCES profile_documents(id) ON DELETE CASCADE,
  raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,

  section_index integer NOT NULL,
  section_title text,
  section_type text NOT NULL DEFAULT 'source_section',
  -- opening / purpose / scope / methodology / result / tool_workflow /
  -- career_positioning / resume_phrase / limitation / source_section

  page_start integer,
  page_end integer,
  char_start integer,
  char_end integer,

  section_text text NOT NULL,
  section_summary text,

  importance_score numeric DEFAULT 0.50,
  model_notes text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(profile_document_id, section_index)
);

CREATE INDEX IF NOT EXISTS idx_profile_document_sections_document
ON profile_document_sections(profile_document_id);

CREATE INDEX IF NOT EXISTS idx_profile_document_sections_raw_file
ON profile_document_sections(raw_file_id);

CREATE INDEX IF NOT EXISTS idx_profile_document_sections_type
ON profile_document_sections(section_type);

-- =========================================================
-- L4.3 Evidence units
-- Evidence units are richer than tiny facts.
-- They preserve quote, summary, support boundary, and overclaim boundary.
-- =========================================================

CREATE TABLE IF NOT EXISTS profile_evidence_units (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_document_id uuid REFERENCES profile_documents(id) ON DELETE SET NULL,
  profile_document_section_id uuid REFERENCES profile_document_sections(id) ON DELETE SET NULL,
  raw_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,

  evidence_type text NOT NULL,
  -- identity / education / coursework / project_scope / methodology / result /
  -- tool_workflow / technical_skill / strategic_analysis / communication /
  -- leadership / resume_phrase / career_positioning / limitation / warning

  evidence_title text NOT NULL,
  direct_quote text,
  evidence_summary text NOT NULL,

  supports_claims text[] NOT NULL DEFAULT '{}'::text[],
  does_not_support_claims text[] NOT NULL DEFAULT '{}'::text[],

  role_families text[] NOT NULL DEFAULT '{}'::text[],
  competency_tags text[] NOT NULL DEFAULT '{}'::text[],
  tool_tags text[] NOT NULL DEFAULT '{}'::text[],
  project_tags text[] NOT NULL DEFAULT '{}'::text[],

  abstraction_level text NOT NULL DEFAULT 'evidence_unit',
  -- source_quote / evidence_unit / synthesized_evidence

  source_confidence numeric DEFAULT 0.80,
  grounding_confidence numeric DEFAULT 0.80,

  status text NOT NULL DEFAULT 'draft',
  -- draft / needs_review / approved / rejected / superseded

  builder_version text,
  builder_model text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_document
ON profile_evidence_units(profile_document_id);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_section
ON profile_evidence_units(profile_document_section_id);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_raw_file
ON profile_evidence_units(raw_file_id);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_status
ON profile_evidence_units(status);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_type
ON profile_evidence_units(evidence_type);

-- =========================================================
-- L4.4 Asset audits
-- DeepSeek / auditor checks grounding, overclaim, and information loss.
-- =========================================================

CREATE TABLE IF NOT EXISTS profile_asset_audits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_asset_id uuid NOT NULL REFERENCES profile_assets(id) ON DELETE CASCADE,

  audit_type text NOT NULL DEFAULT 'grounding_overclaim_audit',
  audit_model text,
  audit_version text,

  grounding_status text NOT NULL DEFAULT 'pending',
  -- pending / pass / needs_edit / reject

  overclaim_risk text NOT NULL DEFAULT 'unknown',
  -- low / medium / high / unknown

  information_loss_risk text NOT NULL DEFAULT 'unknown',
  -- low / medium / high / unknown

  evidence_coverage_score numeric,
  specificity_score numeric,
  job_relevance_score numeric,

  supported_claims text[] NOT NULL DEFAULT '{}'::text[],
  unsupported_claims text[] NOT NULL DEFAULT '{}'::text[],
  required_edits text[] NOT NULL DEFAULT '{}'::text[],
  audit_notes text,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_asset
ON profile_asset_audits(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_status
ON profile_asset_audits(grounding_status);

-- =========================================================
-- L4.5 Capabilities
-- Aggregated professional capabilities backed by assets/evidence.
-- =========================================================

CREATE TABLE IF NOT EXISTS profile_capabilities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  capability_name text NOT NULL,
  capability_type text NOT NULL,
  -- technical / analytical / security / software / data / communication /
  -- leadership / research / strategic / operational

  capability_summary text NOT NULL,
  strength_level text NOT NULL DEFAULT 'developing',
  -- emerging / developing / strong / distinctive

  role_families text[] NOT NULL DEFAULT '{}'::text[],
  competency_tags text[] NOT NULL DEFAULT '{}'::text[],
  tool_tags text[] NOT NULL DEFAULT '{}'::text[],
  course_tags text[] NOT NULL DEFAULT '{}'::text[],
  project_tags text[] NOT NULL DEFAULT '{}'::text[],

  safe_resume_claim text,
  interview_positioning text,
  do_not_overclaim_rules text[] NOT NULL DEFAULT '{}'::text[],

  status text NOT NULL DEFAULT 'draft',
  -- draft / needs_review / approved / rejected / superseded

  builder_version text,
  builder_model text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(capability_name, capability_type)
);

CREATE INDEX IF NOT EXISTS idx_profile_capabilities_type
ON profile_capabilities(capability_type);

CREATE INDEX IF NOT EXISTS idx_profile_capabilities_status
ON profile_capabilities(status);

CREATE TABLE IF NOT EXISTS profile_capability_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_capability_id uuid NOT NULL REFERENCES profile_capabilities(id) ON DELETE CASCADE,
  profile_asset_id uuid REFERENCES profile_assets(id) ON DELETE SET NULL,
  profile_evidence_unit_id uuid REFERENCES profile_evidence_units(id) ON DELETE SET NULL,

  evidence_rank integer NOT NULL DEFAULT 1,
  evidence_role text NOT NULL DEFAULT 'supporting',
  -- primary / supporting / limitation / warning

  note text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(profile_capability_id, profile_asset_id, profile_evidence_unit_id)
);

CREATE INDEX IF NOT EXISTS idx_profile_capability_evidence_capability
ON profile_capability_evidence(profile_capability_id);

CREATE INDEX IF NOT EXISTS idx_profile_capability_evidence_asset
ON profile_capability_evidence(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_capability_evidence_unit
ON profile_capability_evidence(profile_evidence_unit_id);

-- =========================================================
-- L4.6 Model routing policy
-- Declarative routing, so scripts do not hard-code model choices.
-- =========================================================

CREATE TABLE IF NOT EXISTS model_routing_policies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  task_name text UNIQUE NOT NULL,
  primary_model text NOT NULL,
  fallback_model text,
  auditor_model text,

  local_only boolean NOT NULL DEFAULT true,
  max_input_tokens integer,
  temperature numeric DEFAULT 0.10,

  notes text,
  status text NOT NULL DEFAULT 'active',

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

INSERT INTO model_routing_policies (
  task_name,
  primary_model,
  fallback_model,
  auditor_model,
  local_only,
  max_input_tokens,
  temperature,
  notes,
  status
)
VALUES
  (
    'profile_document_mapper',
    'qwen3:8b',
    'gemma3:12b',
    NULL,
    true,
    12000,
    0.10,
    'Map document purpose, structure, source role, and section plan. Do not create final facts.',
    'active'
  ),
  (
    'profile_evidence_unit_builder',
    'qwen3:8b',
    'gemma3:12b',
    NULL,
    true,
    12000,
    0.10,
    'Build evidence units with support boundaries and do-not-support boundaries.',
    'active'
  ),
  (
    'profile_asset_synthesizer',
    'qwen3:8b',
    'gemma3:12b',
    'deepseek-r1:14b',
    true,
    12000,
    0.10,
    'Synthesize rich job-oriented profile assets from multiple evidence units.',
    'active'
  ),
  (
    'profile_grounding_overclaim_auditor',
    'deepseek-r1:14b',
    'phi4-reasoning',
    NULL,
    true,
    12000,
    0.05,
    'Audit grounding, overclaim risk, and information loss before approval.',
    'active'
  ),
  (
    'profile_capability_builder',
    'qwen3:8b',
    'gemma3:12b',
    'deepseek-r1:14b',
    true,
    12000,
    0.10,
    'Aggregate approved assets into professional capabilities.',
    'active'
  )
ON CONFLICT (task_name)
DO UPDATE SET
  primary_model = EXCLUDED.primary_model,
  fallback_model = EXCLUDED.fallback_model,
  auditor_model = EXCLUDED.auditor_model,
  local_only = EXCLUDED.local_only,
  max_input_tokens = EXCLUDED.max_input_tokens,
  temperature = EXCLUDED.temperature,
  notes = EXCLUDED.notes,
  status = EXCLUDED.status,
  updated_at = now();

-- =========================================================
-- Component registry additions
-- Keep the rest of architecture unchanged.
-- =========================================================

INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES
  (
    'profile_document_mapper',
    'agent',
    'profile',
    'Map each source document into document type, purpose, structure, important sections, and source-risk notes before extraction.',
    true,
    'planned',
    'Uses qwen3:8b. Replaces naive file/title heuristics in L4 only.'
  ),
  (
    'profile_evidence_unit_builder',
    'agent',
    'profile',
    'Build source-grounded evidence units that preserve quote, summary, support boundaries, role relevance, and overclaim boundaries.',
    true,
    'planned',
    'Uses qwen3:8b. Evidence units are richer than tiny candidate facts.'
  ),
  (
    'profile_asset_synthesizer',
    'agent',
    'profile',
    'Synthesize job-oriented profile assets from multiple evidence units while preserving narrative, methodology, results, and career positioning.',
    true,
    'planned',
    'Uses qwen3:8b with fallback model if needed.'
  ),
  (
    'profile_grounding_overclaim_auditor',
    'agent',
    'profile',
    'Audit profile assets for evidence grounding, overclaim risk, and information loss before user approval.',
    true,
    'planned',
    'Uses deepseek-r1:14b as local reasoning auditor.'
  ),
  (
    'profile_capability_builder',
    'agent',
    'profile',
    'Aggregate approved profile assets and evidence units into professional capabilities for role briefs and context packs.',
    true,
    'planned',
    'Preserves the original Brief Generator and Profile Pack Builder interfaces.'
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

-- =========================================================
-- Views
-- =========================================================

CREATE OR REPLACE VIEW v_profile_intelligence_status AS
SELECT 'profile_documents' AS layer_object, count(*) AS row_count FROM profile_documents
UNION ALL SELECT 'profile_document_sections', count(*) FROM profile_document_sections
UNION ALL SELECT 'profile_evidence_units', count(*) FROM profile_evidence_units
UNION ALL SELECT 'profile_assets', count(*) FROM profile_assets
UNION ALL SELECT 'profile_asset_evidence_items', count(*) FROM profile_asset_evidence_items
UNION ALL SELECT 'profile_asset_audits', count(*) FROM profile_asset_audits
UNION ALL SELECT 'profile_capabilities', count(*) FROM profile_capabilities
UNION ALL SELECT 'profile_capability_evidence', count(*) FROM profile_capability_evidence
UNION ALL SELECT 'profile_briefs', count(*) FROM profile_briefs
UNION ALL SELECT 'profile_context_packs', count(*) FROM profile_context_packs;

CREATE OR REPLACE VIEW v_profile_document_review AS
SELECT
  pd.id AS profile_document_id,
  left(pd.id::text, 8) AS document_short_id,
  rf.file_name,
  pd.document_title,
  pd.document_type,
  pd.document_purpose,
  pd.source_role,
  pd.contains_profile_evidence,
  pd.contains_guidance_only,
  pd.status,
  pd.mapper_model,
  pd.source_quality,
  count(pds.id) AS section_count,
  count(peu.id) AS evidence_unit_count,
  pd.created_at,
  pd.updated_at
FROM profile_documents pd
LEFT JOIN raw_files rf
  ON rf.id = pd.raw_file_id
LEFT JOIN profile_document_sections pds
  ON pds.profile_document_id = pd.id
LEFT JOIN profile_evidence_units peu
  ON peu.profile_document_id = pd.id
GROUP BY pd.id, rf.file_name
ORDER BY pd.updated_at DESC, pd.document_title;

CREATE OR REPLACE VIEW v_profile_evidence_unit_review AS
SELECT
  peu.id AS evidence_unit_id,
  left(peu.id::text, 8) AS evidence_short_id,
  rf.file_name,
  pd.document_title,
  pds.section_title,
  peu.evidence_type,
  peu.evidence_title,
  peu.status,
  peu.role_families,
  peu.competency_tags,
  peu.tool_tags,
  left(peu.evidence_summary, 280) AS evidence_summary_preview,
  left(peu.direct_quote, 220) AS direct_quote_preview,
  peu.source_confidence,
  peu.grounding_confidence,
  peu.created_at,
  peu.updated_at
FROM profile_evidence_units peu
LEFT JOIN profile_documents pd
  ON pd.id = peu.profile_document_id
LEFT JOIN profile_document_sections pds
  ON pds.id = peu.profile_document_section_id
LEFT JOIN raw_files rf
  ON rf.id = peu.raw_file_id
ORDER BY peu.updated_at DESC, peu.evidence_title;

CREATE OR REPLACE VIEW v_profile_capability_review AS
SELECT
  pc.id AS profile_capability_id,
  left(pc.id::text, 8) AS capability_short_id,
  pc.capability_name,
  pc.capability_type,
  pc.strength_level,
  pc.status,
  pc.role_families,
  pc.competency_tags,
  pc.tool_tags,
  left(pc.capability_summary, 360) AS capability_summary_preview,
  count(pce.id) AS evidence_link_count,
  pc.created_at,
  pc.updated_at
FROM profile_capabilities pc
LEFT JOIN profile_capability_evidence pce
  ON pce.profile_capability_id = pc.id
GROUP BY pc.id
ORDER BY pc.updated_at DESC, pc.capability_name;

CREATE OR REPLACE VIEW v_model_routing_policy AS
SELECT
  task_name,
  primary_model,
  fallback_model,
  auditor_model,
  local_only,
  max_input_tokens,
  temperature,
  status,
  notes
FROM model_routing_policies
ORDER BY task_name;
