-- 030_structured_profile_evidence_schema.sql
-- Keeps the original architecture. This only enriches CHUNK/DMAP/EUB internals.

ALTER TABLE profile_documents
  ADD COLUMN IF NOT EXISTS document_structure_type text,
  ADD COLUMN IF NOT EXISTS chunking_strategy text,
  ADD COLUMN IF NOT EXISTS do_not_chunk_mid_unit boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS structure_confidence numeric DEFAULT 0.50;

ALTER TABLE profile_document_sections
  ADD COLUMN IF NOT EXISTS structured_section_key text,
  ADD COLUMN IF NOT EXISTS structured_section_kind text,
  ADD COLUMN IF NOT EXISTS source_boundary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS chunking_strategy text;

ALTER TABLE profile_evidence_units
  ADD COLUMN IF NOT EXISTS claim text,
  ADD COLUMN IF NOT EXISTS claim_type text,
  ADD COLUMN IF NOT EXISTS tool_name text,
  ADD COLUMN IF NOT EXISTS tool_category text,
  ADD COLUMN IF NOT EXISTS workflow_group text,
  ADD COLUMN IF NOT EXISTS source_boundaries jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS evidence_strength text,
  ADD COLUMN IF NOT EXISTS resume_safe_phrase text,
  ADD COLUMN IF NOT EXISTS role_relevance text[] NOT NULL DEFAULT '{}'::text[],
  ADD COLUMN IF NOT EXISTS must_not_claim text[] NOT NULL DEFAULT '{}'::text[],
  ADD COLUMN IF NOT EXISTS structured_extraction_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS extraction_strategy text;

CREATE INDEX IF NOT EXISTS idx_profile_documents_chunking_strategy
ON profile_documents(chunking_strategy);

CREATE INDEX IF NOT EXISTS idx_profile_document_sections_structured_kind
ON profile_document_sections(structured_section_kind);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_claim_type
ON profile_evidence_units(claim_type);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_workflow_group
ON profile_evidence_units(workflow_group);

CREATE INDEX IF NOT EXISTS idx_profile_evidence_units_evidence_strength
ON profile_evidence_units(evidence_strength);

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
  'structured_profile_evidence_schema',
  'schema',
  'profile',
  'Adds structured evidence fields inside the existing CHUNK, DMAP, and EUB flow: source boundaries, resume-safe phrases, workflow groups, evidence strength, and must-not-claim boundaries.',
  false,
  'active',
  'Does not change the architecture graph. Supports structured inventory documents such as tool inventories and cross-portfolio mappings.'
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
