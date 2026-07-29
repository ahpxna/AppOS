-- =========================================================
-- 031 — Profile Capability Builder Support Tables + Views
-- Purpose:
--   Link derived profile_capabilities back to approved profile_assets.
--   This keeps the capability layer auditable and evidence-grounded.
-- =========================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS profile_capability_asset_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  profile_capability_id uuid NOT NULL
    REFERENCES profile_capabilities(id)
    ON DELETE CASCADE,

  profile_asset_id uuid NOT NULL
    REFERENCES profile_assets(id)
    ON DELETE CASCADE,

  link_reason text NOT NULL,
  evidence_weight numeric,
  created_at timestamptz DEFAULT now(),

  UNIQUE (profile_capability_id, profile_asset_id)
);

CREATE INDEX IF NOT EXISTS idx_profile_capability_asset_links_capability
  ON profile_capability_asset_links(profile_capability_id);

CREATE INDEX IF NOT EXISTS idx_profile_capability_asset_links_asset
  ON profile_capability_asset_links(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_capabilities_status_builder
  ON profile_capabilities(status, builder_version);

CREATE OR REPLACE VIEW v_profile_capability_review AS
SELECT
  pc.id AS profile_capability_id,
  left(pc.id::text, 8) AS capability_short_id,

  pc.capability_name,
  pc.capability_type,
  pc.strength_level,
  pc.status,
  pc.builder_version,
  pc.builder_model,

  pc.role_families,
  pc.competency_tags,
  pc.tool_tags,
  pc.course_tags,
  pc.project_tags,

  pc.capability_summary,
  pc.safe_resume_claim,
  pc.interview_positioning,
  pc.do_not_overclaim_rules,

  COALESCE(l.linked_asset_count, 0) AS linked_asset_count,
  COALESCE(l.linked_asset_short_ids, ARRAY[]::text[]) AS linked_asset_short_ids,
  COALESCE(l.linked_asset_titles, ARRAY[]::text[]) AS linked_asset_titles,

  pc.created_at,
  pc.updated_at

FROM profile_capabilities pc
LEFT JOIN (
  SELECT
    pcal.profile_capability_id,
    count(*) AS linked_asset_count,
    array_agg(left(pa.id::text, 8) ORDER BY pa.asset_title) AS linked_asset_short_ids,
    array_agg(pa.asset_title ORDER BY pa.asset_title) AS linked_asset_titles
  FROM profile_capability_asset_links pcal
  JOIN profile_assets pa
    ON pa.id = pcal.profile_asset_id
  GROUP BY pcal.profile_capability_id
) l
  ON l.profile_capability_id = pc.id;

CREATE OR REPLACE VIEW v_profile_capability_asset_link_detail AS
SELECT
  left(pc.id::text, 8) AS capability_short_id,
  pc.capability_name,
  pc.status AS capability_status,
  pc.strength_level,

  left(pa.id::text, 8) AS asset_short_id,
  pa.asset_title,
  pa.asset_type,
  pa.status AS asset_status,

  pcal.link_reason,
  pcal.evidence_weight,
  pcal.created_at AS linked_at
FROM profile_capability_asset_links pcal
JOIN profile_capabilities pc
  ON pc.id = pcal.profile_capability_id
JOIN profile_assets pa
  ON pa.id = pcal.profile_asset_id;
