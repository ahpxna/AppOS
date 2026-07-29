-- 017_profile_fact_verifier_rewriter.sql
-- Real L4 profile truth-layer component:
-- Candidate fact -> evidence/context verification -> rewrite/approve/reject suggestion -> human accept -> profile_facts.

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
  'profile_fact_verifier_rewriter',
  'agent',
  'profile',
  'Verify candidate profile facts against evidence quote and source chunk context, rewrite overclaims into evidence-grounded facts, and reject unsupported claims before profile_facts promotion.',
  true,
  'prototype',
  'This is the real L4 evidence-grounding gate between semantic dedup/conflict resolution and approved profile_facts.'
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

CREATE TABLE IF NOT EXISTS candidate_fact_verification_suggestions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  candidate_fact_id uuid NOT NULL REFERENCES candidate_profile_facts(id) ON DELETE CASCADE,
  source_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  source_chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,

  verifier_name text NOT NULL DEFAULT 'profile_fact_verifier_rewriter',
  verifier_version text NOT NULL,

  status text NOT NULL DEFAULT 'pending',
  -- pending / accepted / rejected / superseded

  decision text NOT NULL,
  -- approve_as_is / rewrite / reject / ask_user

  original_category text,
  original_subcategory text,
  original_fact_text text,
  original_evidence_quote text,

  suggested_category text,
  suggested_subcategory text,
  suggested_fact_text text,
  suggested_evidence_quote text,

  evidence_assessment text,
  context_assessment text,
  reasoning text,
  risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,

  confidence numeric,

  accepted_profile_fact_id uuid REFERENCES profile_facts(id) ON DELETE SET NULL,

  created_at timestamptz DEFAULT now(),
  reviewed_at timestamptz,
  review_note text,

  UNIQUE(candidate_fact_id, verifier_version)
);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_verification_suggestions_candidate
ON candidate_fact_verification_suggestions(candidate_fact_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_verification_suggestions_status
ON candidate_fact_verification_suggestions(status);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_verification_suggestions_decision
ON candidate_fact_verification_suggestions(decision);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_verification_suggestions_source_chunk
ON candidate_fact_verification_suggestions(source_chunk_id);

CREATE OR REPLACE VIEW v_candidate_fact_verification_review AS
SELECT
  s.id,
  left(s.id::text, 8) AS suggestion_short_id,

  s.status AS suggestion_status,
  s.decision,
  s.confidence,

  cpf.status AS candidate_status,
  left(cpf.id::text, 8) AS candidate_short_id,

  rf.file_name AS source_file,
  rf.file_role,
  pc.chunk_index AS source_chunk_index,
  pc.section AS source_section,

  s.original_category,
  s.original_subcategory,
  s.original_fact_text,
  s.original_evidence_quote,

  s.suggested_category,
  s.suggested_subcategory,
  s.suggested_fact_text,
  s.suggested_evidence_quote,

  s.evidence_assessment,
  s.context_assessment,
  s.reasoning,
  s.risk_flags,

  s.accepted_profile_fact_id,
  s.created_at,
  s.reviewed_at
FROM candidate_fact_verification_suggestions s
JOIN candidate_profile_facts cpf
  ON cpf.id = s.candidate_fact_id
LEFT JOIN raw_files rf
  ON rf.id = s.source_file_id
LEFT JOIN profile_chunks pc
  ON pc.id = s.source_chunk_id
ORDER BY
  CASE s.status
    WHEN 'pending' THEN 1
    WHEN 'accepted' THEN 2
    WHEN 'rejected' THEN 3
    ELSE 4
  END,
  CASE s.decision
    WHEN 'approve_as_is' THEN 1
    WHEN 'rewrite' THEN 2
    WHEN 'ask_user' THEN 3
    WHEN 'reject' THEN 4
    ELSE 5
  END,
  s.confidence DESC NULLS LAST,
  s.created_at DESC;
