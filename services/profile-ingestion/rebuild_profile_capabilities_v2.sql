BEGIN;

-- =========================================================
-- Capability Builder V2
-- Fix:
--   V1 over-merged assets into DFIR because it classified using a broad haystack
--   and checked DFIR terms before more specific asset/tool identity.
--
-- Principle:
--   Classify approved profile_assets by title + tool/project tags first.
--   Use job relevance text only as weak fallback, not primary classifier.
-- =========================================================

CREATE TEMP TABLE tmp_profile_capability_asset_source_v2 AS
WITH evidence_counts AS (
  SELECT
    profile_asset_id,
    count(*) AS evidence_item_count
  FROM profile_asset_evidence_items
  GROUP BY profile_asset_id
),
asset_source AS (
  SELECT
    pa.id AS profile_asset_id,
    pa.asset_title,
    pa.asset_type,
    pa.canonical_narrative,
    pa.job_oriented_summary,
    pa.resume_bullet_bank,
    pa.interview_story,
    pa.role_families,
    pa.competency_tags,
    pa.tool_tags,
    pa.project_tags,
    pa.do_not_overclaim_rules,
    COALESCE(ec.evidence_item_count, 0) AS evidence_item_count,

    lower(pa.asset_title) AS title_l,
    lower(array_to_string(pa.tool_tags, ' ')) AS tools_l,
    lower(array_to_string(pa.project_tags, ' ')) AS projects_l,
    lower(array_to_string(pa.competency_tags, ' ')) AS competencies_l,
    lower(COALESCE(pa.job_oriented_summary, '')) AS job_summary_l,
    lower(COALESCE(pa.canonical_narrative, '')) AS narrative_l

  FROM profile_assets pa
  LEFT JOIN evidence_counts ec
    ON ec.profile_asset_id = pa.id
  WHERE pa.status = 'approved'
),
classified AS (
  SELECT
    *,

    CASE
      -- Title-specific categories first.
      WHEN title_l LIKE '%enterprise network controls%'
        OR title_l LIKE '%network discovery%'
        OR tools_l ~ 'gns3|cisco|arista|freeradius|syslog|tcpdump|traceroute|ping|vrrp|ospf|bgp|firewall|radius'
        THEN 'network_security_controls_protocol_analysis'

      WHEN title_l LIKE '%artifact analysis%'
        OR title_l LIKE '%live response%'
        OR tools_l ~ 'autopsy|caine|regripper|redline|magnet|hxd|bulk extractor|browser history view|deft|dart|winaudit|treesizefree|drive manager'
        THEN 'digital_forensics_incident_response'

      WHEN title_l LIKE '%password recovery%'
        OR tools_l ~ 'john the ripper|pdfcrack'
        THEN 'credential_password_recovery'

      WHEN title_l LIKE '%web application security%'
        OR title_l LIKE '%owasp%'
        OR tools_l ~ 'owasp|juice shop|burp'
        THEN 'web_application_security_testing'

      WHEN title_l LIKE '%data security analysis%'
        OR tools_l ~ 'veracrypt|pandas|numpy|scipy|scikit|matplotlib'
        THEN 'data_security_analytics'

      WHEN title_l LIKE '%pki%'
        OR title_l LIKE '%tls%'
        OR tools_l ~ 'openssl|ocsp|mod_ssl|mitmproxy|curl'
        THEN 'pki_tls_validation'

      -- Weak fallback only after direct identity checks.
      WHEN competencies_l ~ 'network|protocol|firewall|aaa|logging'
        THEN 'network_security_controls_protocol_analysis'

      WHEN competencies_l ~ 'forensics|incident response|artifact|memory'
        THEN 'digital_forensics_incident_response'

      WHEN competencies_l ~ 'application security|web security|owasp'
        THEN 'web_application_security_testing'

      ELSE 'general_security_tool_workflows'
    END AS cluster_key

  FROM asset_source
)
SELECT
  profile_asset_id,
  asset_title,
  asset_type,
  canonical_narrative,
  job_oriented_summary,
  resume_bullet_bank,
  interview_story,
  role_families,
  competency_tags,
  tool_tags,
  project_tags,
  do_not_overclaim_rules,
  evidence_item_count,
  cluster_key,

  CASE cluster_key
    WHEN 'network_security_controls_protocol_analysis'
      THEN 'Network Security Controls and Protocol Analysis'
    WHEN 'digital_forensics_incident_response'
      THEN 'Digital Forensics and Incident Response Tooling'
    WHEN 'credential_password_recovery'
      THEN 'Credential and Password Recovery Workflows'
    WHEN 'web_application_security_testing'
      THEN 'Web Application Security Testing'
    WHEN 'data_security_analytics'
      THEN 'Data Security and Analytics Workflows'
    WHEN 'pki_tls_validation'
      THEN 'PKI/TLS Validation Workflows'
    ELSE 'General Security Tool Workflows'
  END AS capability_name

FROM classified;

DO $$
DECLARE
  approved_count integer;
BEGIN
  SELECT count(*) INTO approved_count
  FROM tmp_profile_capability_asset_source_v2;

  IF approved_count = 0 THEN
    RAISE EXCEPTION 'No approved profile_assets found. Approve profile assets before building capabilities.';
  END IF;
END $$;

-- Remove V1 and previous V2 output only. Keep other capability versions intact.
DELETE FROM profile_capability_asset_links
WHERE profile_capability_id IN (
  SELECT id
  FROM profile_capabilities
  WHERE builder_version IN (
    'capability_builder_v1_approved_assets_2026_04_27',
    'capability_builder_v2_title_tool_priority_2026_04_28'
  )
);

DELETE FROM profile_capabilities
WHERE builder_version IN (
  'capability_builder_v1_approved_assets_2026_04_27',
  'capability_builder_v2_title_tool_priority_2026_04_28'
);

WITH grouped AS (
  SELECT
    cluster_key,
    capability_name,
    count(*) AS asset_count,
    sum(evidence_item_count) AS total_evidence_items,

    string_agg(
      '- ' || asset_title || ': ' ||
      left(COALESCE(job_oriented_summary, canonical_narrative), 550),
      E'\n'
      ORDER BY asset_title
    ) AS evidence_summary,

    string_agg(asset_title, '; ' ORDER BY asset_title) AS asset_title_list

  FROM tmp_profile_capability_asset_source_v2
  GROUP BY cluster_key, capability_name
),
prepared AS (
  SELECT
    g.cluster_key,
    g.capability_name,

    CASE
      WHEN g.asset_count >= 2 AND g.total_evidence_items >= 6
        THEN 'strong_academic_project_evidence'
      WHEN g.total_evidence_items >= 4
        THEN 'moderate_academic_project_evidence'
      ELSE 'emerging_academic_project_evidence'
    END AS strength_level,

    'Aggregates ' || g.asset_count || ' approved profile asset(s) into a bounded capability cluster. ' ||
    'This capability is derived only from approved profile_assets and uses title/tool-priority classification to avoid broad role-keyword over-merging.' ||
    E'\n\nEvidence-backed source assets:\n' || g.evidence_summary AS capability_summary,

    'Academic/project-based experience with ' || lower(g.capability_name) ||
    ', supported by approved profile assets covering: ' || g.asset_title_list || '.' AS safe_resume_claim,

    'Can discuss controlled academic, lab, and project work involving ' ||
    lower(g.capability_name) ||
    '. Keep the story bounded to coursework/project evidence and avoid presenting it as professional employment.' AS interview_positioning,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.role_families) AS elem
        FROM tmp_profile_capability_asset_source_v2 t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS role_families,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.competency_tags) AS elem
        FROM tmp_profile_capability_asset_source_v2 t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS competency_tags,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.tool_tags) AS elem
        FROM tmp_profile_capability_asset_source_v2 t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS tool_tags,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.project_tags) AS elem
        FROM tmp_profile_capability_asset_source_v2 t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS project_tags,

    COALESCE((
      SELECT array_agg(DISTINCT rule ORDER BY rule)
      FROM (
        SELECT unnest(t.do_not_overclaim_rules) AS rule
        FROM tmp_profile_capability_asset_source_v2 t
        WHERE t.cluster_key = g.cluster_key

        UNION ALL
        SELECT 'Do not present academic lab, coursework, or project exposure as professional employment.'

        UNION ALL
        SELECT 'Do not claim production, enterprise, certification, or expert-level experience unless separately supported.'

        UNION ALL
        SELECT 'Keep claims bounded to the approved profile assets linked to this capability.'
      ) x
      WHERE rule IS NOT NULL AND btrim(rule) <> ''
    ), ARRAY[]::text[]) AS do_not_overclaim_rules

  FROM grouped g
)
INSERT INTO profile_capabilities (
  id,
  capability_name,
  capability_type,
  capability_summary,
  strength_level,
  role_families,
  competency_tags,
  tool_tags,
  course_tags,
  project_tags,
  safe_resume_claim,
  interview_positioning,
  do_not_overclaim_rules,
  status,
  builder_version,
  builder_model,
  created_at,
  updated_at
)
SELECT
  gen_random_uuid(),
  capability_name,
  'approved_asset_cluster',
  capability_summary,
  strength_level,
  role_families,
  competency_tags,
  tool_tags,
  ARRAY[]::text[] AS course_tags,
  project_tags,
  safe_resume_claim,
  interview_positioning,
  do_not_overclaim_rules,
  'approved',
  'capability_builder_v2_title_tool_priority_2026_04_28',
  'deterministic_sql_title_tool_priority_from_approved_profile_assets',
  now(),
  now()
FROM prepared
ORDER BY capability_name;

INSERT INTO profile_capability_asset_links (
  profile_capability_id,
  profile_asset_id,
  link_reason,
  evidence_weight
)
SELECT
  pc.id,
  t.profile_asset_id,
  'Asset grouped into capability cluster "' || t.cluster_key || '" by capability_builder_v2 using title/tool-priority classification from approved profile_assets only.',
  LEAST(1.0, GREATEST(0.2, t.evidence_item_count::numeric / 8.0))
FROM tmp_profile_capability_asset_source_v2 t
JOIN profile_capabilities pc
  ON pc.capability_name = t.capability_name
 AND pc.builder_version = 'capability_builder_v2_title_tool_priority_2026_04_28'
ON CONFLICT (profile_capability_id, profile_asset_id)
DO UPDATE SET
  link_reason = EXCLUDED.link_reason,
  evidence_weight = EXCLUDED.evidence_weight;

COMMIT;
