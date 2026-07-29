BEGIN;

CREATE TEMP TABLE tmp_profile_capability_asset_source AS
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

    lower(
      pa.asset_title || ' ' ||
      COALESCE(pa.job_oriented_summary, '') || ' ' ||
      array_to_string(pa.role_families, ' ') || ' ' ||
      array_to_string(pa.competency_tags, ' ') || ' ' ||
      array_to_string(pa.tool_tags, ' ') || ' ' ||
      array_to_string(pa.project_tags, ' ')
    ) AS haystack
  FROM profile_assets pa
  LEFT JOIN evidence_counts ec
    ON ec.profile_asset_id = pa.id
  WHERE pa.status = 'approved'
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

  CASE
    WHEN haystack ~ 'forensic|autopsy|caine|regripper|redline|magnet|memory|artifact|incident response|dfir|live response'
      THEN 'digital_forensics_incident_response'

    WHEN haystack ~ 'owasp|juice shop|web application|burp|xss|sql injection|application security'
      THEN 'web_application_security_testing'

    WHEN haystack ~ 'john the ripper|pdfcrack|password recovery|password cracking|credential'
      THEN 'credential_password_recovery'

    WHEN haystack ~ 'veracrypt|pandas|numpy|scipy|scikit|matplotlib|data security|analytics'
      THEN 'data_security_analytics'

    WHEN haystack ~ 'pki|tls|openssl|certificate|ocsp|https|mod_ssl'
      THEN 'pki_tls_validation'

    WHEN haystack ~ 'gns3|cisco|arista|freeradius|syslog|tcpdump|traceroute|ping|network|vrrp|ospf|bgp|firewall|radius|aaa'
      THEN 'network_security_controls_protocol_analysis'

    ELSE 'general_security_tool_workflows'
  END AS cluster_key,

  CASE
    WHEN haystack ~ 'forensic|autopsy|caine|regripper|redline|magnet|memory|artifact|incident response|dfir|live response'
      THEN 'Digital Forensics and Incident Response Tooling'

    WHEN haystack ~ 'owasp|juice shop|web application|burp|xss|sql injection|application security'
      THEN 'Web Application Security Testing'

    WHEN haystack ~ 'john the ripper|pdfcrack|password recovery|password cracking|credential'
      THEN 'Credential and Password Recovery Workflows'

    WHEN haystack ~ 'veracrypt|pandas|numpy|scipy|scikit|matplotlib|data security|analytics'
      THEN 'Data Security and Analytics Workflows'

    WHEN haystack ~ 'pki|tls|openssl|certificate|ocsp|https|mod_ssl'
      THEN 'PKI/TLS Validation Workflows'

    WHEN haystack ~ 'gns3|cisco|arista|freeradius|syslog|tcpdump|traceroute|ping|network|vrrp|ospf|bgp|firewall|radius|aaa'
      THEN 'Network Security Controls and Protocol Analysis'

    ELSE 'General Security Tool Workflows'
  END AS capability_name

FROM asset_source;

DO $$
DECLARE
  approved_count integer;
BEGIN
  SELECT count(*) INTO approved_count
  FROM tmp_profile_capability_asset_source;

  IF approved_count = 0 THEN
    RAISE EXCEPTION 'No approved profile_assets found. Approve profile assets before building capabilities.';
  END IF;
END $$;

DELETE FROM profile_capability_asset_links
WHERE profile_capability_id IN (
  SELECT id
  FROM profile_capabilities
  WHERE builder_version = 'capability_builder_v1_approved_assets_2026_04_27'
);

DELETE FROM profile_capabilities
WHERE builder_version = 'capability_builder_v1_approved_assets_2026_04_27';

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

  FROM tmp_profile_capability_asset_source
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
    'This capability is derived only from approved profile_assets, not from unapproved tiny facts.' ||
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
        FROM tmp_profile_capability_asset_source t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS role_families,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.competency_tags) AS elem
        FROM tmp_profile_capability_asset_source t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS competency_tags,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.tool_tags) AS elem
        FROM tmp_profile_capability_asset_source t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS tool_tags,

    COALESCE((
      SELECT array_agg(DISTINCT elem ORDER BY elem)
      FROM (
        SELECT unnest(t.project_tags) AS elem
        FROM tmp_profile_capability_asset_source t
        WHERE t.cluster_key = g.cluster_key
      ) x
      WHERE elem IS NOT NULL AND btrim(elem) <> ''
    ), ARRAY[]::text[]) AS project_tags,

    COALESCE((
      SELECT array_agg(DISTINCT rule ORDER BY rule)
      FROM (
        SELECT unnest(t.do_not_overclaim_rules) AS rule
        FROM tmp_profile_capability_asset_source t
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
  'capability_builder_v1_approved_assets_2026_04_27',
  'deterministic_sql_from_approved_profile_assets',
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
  'Asset grouped into capability cluster "' || t.cluster_key || '" by deterministic capability_builder_v1 using approved profile_assets only.',
  LEAST(1.0, GREATEST(0.2, t.evidence_item_count::numeric / 8.0))
FROM tmp_profile_capability_asset_source t
JOIN profile_capabilities pc
  ON pc.capability_name = t.capability_name
 AND pc.builder_version = 'capability_builder_v1_approved_assets_2026_04_27'
ON CONFLICT (profile_capability_id, profile_asset_id)
DO UPDATE SET
  link_reason = EXCLUDED.link_reason,
  evidence_weight = EXCLUDED.evidence_weight;

COMMIT;
