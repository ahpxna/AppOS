-- =========================================================
-- 034_document_generation_layer.sql
-- L6 -- DOCUMENT GENERATION + QA
--
-- Registers the L6 components and extends generated_documents
-- with the fields the truth checker needs.
--
-- Design rule enforced here:
--   A generated document may ONLY cite approved profile_assets.
--   Enforced by v_document_generation_source_assets, which the
--   generator reads from. Draft assets are invisible to L6.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. Component registry entries
-- ---------------------------------------------------------

INSERT INTO component_registry (
  name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at
)
VALUES
  ('resume_agent', 'agent', 'L6',
   'Generate role-targeted resume bullets from approved profile assets.',
   true, 'prototype',
   'Every bullet must carry a source profile_asset_id in evidence_map.',
   now(), now()),

  ('cover_letter_agent', 'agent', 'L6',
   'Generate a cover letter grounded in approved profile assets and the fit analysis.',
   true, 'prototype',
   'Positioning text comes from profile_assets.cover_letter_positioning.',
   now(), now()),

  ('short_answer_agent', 'agent', 'L6',
   'Answer application form free-text questions from approved profile assets.',
   true, 'prototype',
   'Refuses to answer when no approved asset supports the question.',
   now(), now()),

  ('truth_quality_checker', 'safety', 'L6',
   'Verify every claim in a generated document against its cited asset.',
   true, 'prototype',
   'Emits qa_status pass/revise/fail. Blocks approval on fail.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET component_type = EXCLUDED.component_type,
    layer          = EXCLUDED.layer,
    purpose        = EXCLUDED.purpose,
    status         = EXCLUDED.status,
    notes          = EXCLUDED.notes,
    updated_at     = now();

-- ---------------------------------------------------------
-- 2. Extend generated_documents for the QA loop
-- ---------------------------------------------------------

ALTER TABLE generated_documents
  ADD COLUMN IF NOT EXISTS asset_ids_used      jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS generator_version   text,
  ADD COLUMN IF NOT EXISTS generator_model     text,
  ADD COLUMN IF NOT EXISTS target_role_family  text,
  ADD COLUMN IF NOT EXISTS qa_report           jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS qa_checked_at       timestamptz,
  ADD COLUMN IF NOT EXISTS revision_of         uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS revision_round      int NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_generated_documents_qa_status
ON generated_documents(qa_status);

CREATE INDEX IF NOT EXISTS idx_generated_documents_revision_of
ON generated_documents(revision_of);

-- Approval is only meaningful after QA passed.
ALTER TABLE generated_documents
  DROP CONSTRAINT IF EXISTS chk_generated_documents_approval_requires_qa;

ALTER TABLE generated_documents
  ADD CONSTRAINT chk_generated_documents_approval_requires_qa
  CHECK (approved = false OR qa_status = 'pass');

-- ---------------------------------------------------------
-- 3. The only asset source L6 is allowed to read
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_document_generation_source_assets AS
SELECT
  pa.id                        AS profile_asset_id,
  pa.asset_title,
  pa.asset_type,
  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.job_oriented_summary,
  pa.resume_bullet_bank,
  pa.cover_letter_positioning,
  pa.interview_story,
  pa.do_not_overclaim_rules,
  pa.confidence
FROM profile_assets pa
WHERE pa.status = 'approved';

COMMENT ON VIEW v_document_generation_source_assets IS
  'L6 generators read ONLY from here. Unapproved assets are structurally '
  'unreachable by document generation.';

-- ---------------------------------------------------------
-- 4. Applications that are ready for document generation
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_applications_ready_for_documents AS
SELECT
  a.id            AS application_id,
  a.company,
  a.job_title,
  a.status,
  a.current_step,
  jfa.fit_score,
  jfa.fit_decision,
  jfa.role_family,
  jfa.seniority_level,
  jfa.matched_requirements,
  jfa.missing_or_weak_requirements,
  jfa.risk_flags,
  jfa.created_at  AS analyzed_at
FROM applications a
JOIN job_fit_analyses jfa ON jfa.application_id = a.id
WHERE jfa.fit_decision IN ('ask_user', 'approve_research')
ORDER BY jfa.fit_score DESC, jfa.created_at DESC;

COMMENT ON VIEW v_applications_ready_for_documents IS
  'Rejected applications never reach document generation.';

-- ---------------------------------------------------------
-- 5. QA queue
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_documents_pending_qa AS
SELECT
  gd.id AS generated_document_id,
  gd.application_id,
  gd.doc_type,
  gd.version,
  gd.revision_round,
  gd.generator_version,
  gd.created_at
FROM generated_documents gd
WHERE gd.qa_status IS NULL OR gd.qa_status = 'revise'
ORDER BY gd.created_at;

COMMIT;
