-- 028_profile_document_map_quality_gate.sql
-- L4 Profile Knowledge Layer only.
-- Adds deterministic quality gate between Profile Document Mapper and Evidence Unit Builder.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS profile_document_map_audits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_document_id uuid NOT NULL REFERENCES profile_documents(id) ON DELETE CASCADE,

  audit_version text NOT NULL,
  audit_method text NOT NULL DEFAULT 'deterministic_document_map_quality_gate',

  audit_status text NOT NULL,
  -- pass / warn / block

  severity text NOT NULL DEFAULT 'low',
  -- low / medium / high / critical

  finding_count integer NOT NULL DEFAULT 0,

  findings_json jsonb NOT NULL DEFAULT '[]'::jsonb,

  has_document_type_mismatch boolean NOT NULL DEFAULT false,
  has_external_metadata_hallucination boolean NOT NULL DEFAULT false,
  has_source_role_violation boolean NOT NULL DEFAULT false,
  has_guidance_truth_violation boolean NOT NULL DEFAULT false,
  has_source_paper_truth_violation boolean NOT NULL DEFAULT false,
  has_research_completion_overclaim boolean NOT NULL DEFAULT false,
  has_generic_or_low_value_summary boolean NOT NULL DEFAULT false,
  has_duplicate_risk_notes boolean NOT NULL DEFAULT false,

  recommended_action text NOT NULL DEFAULT 'allow',
  -- allow / review_before_evidence / remap / ignore_for_truth

  created_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE(profile_document_id, audit_version)
);

CREATE INDEX IF NOT EXISTS idx_profile_document_map_audits_doc
ON profile_document_map_audits(profile_document_id);

CREATE INDEX IF NOT EXISTS idx_profile_document_map_audits_status
ON profile_document_map_audits(audit_status);

CREATE INDEX IF NOT EXISTS idx_profile_document_map_audits_action
ON profile_document_map_audits(recommended_action);

CREATE OR REPLACE VIEW v_profile_document_map_quality_gate AS
SELECT
  pd.id AS profile_document_id,
  left(pd.id::text, 8) AS document_short_id,
  rf.file_name,
  pd.document_type,
  pd.source_role,
  pd.status AS document_status,
  pd.mapper_model,
  pd.mapper_version,
  left(pd.document_summary, 420) AS document_summary_preview,
  a.audit_status,
  a.severity,
  a.finding_count,
  a.recommended_action,
  a.has_document_type_mismatch,
  a.has_external_metadata_hallucination,
  a.has_source_role_violation,
  a.has_guidance_truth_violation,
  a.has_source_paper_truth_violation,
  a.has_research_completion_overclaim,
  a.has_generic_or_low_value_summary,
  a.has_duplicate_risk_notes,
  a.findings_json,
  a.created_at AS audited_at
FROM profile_documents pd
LEFT JOIN raw_files rf
  ON rf.id = pd.raw_file_id
LEFT JOIN LATERAL (
  SELECT *
  FROM profile_document_map_audits a
  WHERE a.profile_document_id = pd.id
  ORDER BY a.created_at DESC
  LIMIT 1
) a ON true;

CREATE OR REPLACE VIEW v_profile_documents_ready_for_evidence AS
SELECT *
FROM v_profile_document_map_quality_gate
WHERE document_status = 'mapped'
  AND COALESCE(audit_status, 'missing') = 'pass'
  AND recommended_action = 'allow';

CREATE OR REPLACE VIEW v_profile_documents_blocked_from_evidence AS
SELECT *
FROM v_profile_document_map_quality_gate
WHERE document_status = 'mapped'
  AND (
    COALESCE(audit_status, 'missing') <> 'pass'
    OR recommended_action <> 'allow'
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
  'profile_document_map_quality_gate',
  'service',
  'profile',
  'Deterministically audits mapped profile documents before evidence-unit extraction, blocking hallucinated metadata, document-type mismatch, source-role violations, and overclaim-prone maps.',
  false,
  'active',
  'Runs after profile_document_mapper and before profile_evidence_unit_builder. Does not call LLM.'
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
