BEGIN;

-- =========================================================
-- Profile Brief Generator V1
-- Source of truth:
--   approved profile_assets
--   approved profile_capabilities from capability_builder_v2
--
-- Does NOT use:
--   candidate_profile_facts
--   tiny facts
--   unapproved assets
-- =========================================================

CREATE TEMP TABLE tmp_brief_capabilities AS
SELECT *
FROM v_profile_capability_review
WHERE status = 'approved'
  AND builder_version = 'capability_builder_v2_title_tool_priority_2026_04_28';

CREATE TEMP TABLE tmp_brief_assets AS
SELECT
  pa.id AS profile_asset_id,
  left(pa.id::text, 8) AS asset_short_id,
  pa.asset_title,
  pa.asset_type,
  pa.status,
  pa.canonical_narrative,
  pa.job_oriented_summary,
  pa.resume_bullet_bank,
  pa.interview_story,
  pa.cover_letter_positioning,
  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.project_tags,
  pa.do_not_overclaim_rules,
  pa.confidence,
  pa.updated_at
FROM profile_assets pa
WHERE pa.status = 'approved';

CREATE TEMP TABLE tmp_brief_links AS
SELECT
  c.profile_capability_id,
  c.capability_name,
  l.profile_asset_id
FROM tmp_brief_capabilities c
JOIN profile_capability_asset_links l
  ON l.profile_capability_id = c.profile_capability_id;

CREATE TEMP TABLE tmp_brief_snapshot AS
SELECT
  md5(
    COALESCE((
      SELECT string_agg(
        profile_asset_id::text || ':' || COALESCE(updated_at::text, ''),
        '|'
        ORDER BY profile_asset_id::text
      )
      FROM tmp_brief_assets
    ), '')
    || '::'
    || COALESCE((
      SELECT string_agg(
        profile_capability_id::text || ':' || COALESCE(updated_at::text, ''),
        '|'
        ORDER BY profile_capability_id::text
      )
      FROM tmp_brief_capabilities
    ), '')
  ) AS snapshot_hash;

DO $$
DECLARE
  cap_count integer;
  asset_count integer;
BEGIN
  SELECT count(*) INTO cap_count FROM tmp_brief_capabilities;
  SELECT count(*) INTO asset_count FROM tmp_brief_assets;

  IF cap_count = 0 THEN
    RAISE EXCEPTION 'No approved profile_capabilities found for builder V2.';
  END IF;

  IF asset_count = 0 THEN
    RAISE EXCEPTION 'No approved profile_assets found.';
  END IF;
END $$;

DELETE FROM profile_briefs
WHERE brief_type IN (
  'cybersecurity_general',
  'soc_dfir',
  'network_security',
  'appsec_entry_level',
  'grc_security_analytics'
);

-- =========================================================
-- 1. General cybersecurity brief
-- =========================================================
INSERT INTO profile_briefs (
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  is_stale
)
SELECT
  'cybersecurity_general',
  concat_ws(
    E'\n',
    '# Cybersecurity General Profile Brief',
    '',
    'Positioning:',
    'Senior undergraduate profile combining computer science fundamentals with cybersecurity coursework and controlled academic/project evidence. Strongest supported areas are network security controls/protocol analysis and digital forensics/incident response tooling, with additional bounded evidence in web application security testing, credential/password recovery workflows, and data security analytics.',
    '',
    'Approved capability claims:',
    COALESCE((
      SELECT string_agg(
        '- ' || capability_name || ' [' || strength_level || ']: ' || safe_resume_claim,
        E'\n'
        ORDER BY capability_name
      )
      FROM tmp_brief_capabilities
    ), '- No approved capabilities available.'),
    '',
    'Approved source assets:',
    COALESCE((
      SELECT string_agg(
        '- ' || asset_title || ': ' || left(COALESCE(job_oriented_summary, canonical_narrative), 520),
        E'\n'
        ORDER BY asset_title
      )
      FROM tmp_brief_assets
    ), '- No approved assets available.'),
    '',
    'Safe resume positioning:',
    'Use language such as “academic/project-based experience”, “controlled lab environments”, “coursework-supported exposure”, and “security tooling workflows”. Do not present this as professional production security operations experience.',
    '',
    'Do-not-overclaim rules:',
    '- Do not claim production, enterprise, certification, or expert-level experience unless separately supported.',
    '- Do not present academic labs or coursework as employment.',
    '- Do not claim tools or frameworks outside the linked approved assets.',
    '- Keep claims bounded to the approved assets and capabilities in this brief.',
    '',
    'Generator:',
    'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28'
  ),
  jsonb_build_object(
    'generator_version', 'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28',
    'source', 'approved_profile_assets_and_capabilities_only',
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(profile_capability_id::text ORDER BY capability_name), '[]'::jsonb)
      FROM tmp_brief_capabilities
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(profile_asset_id::text ORDER BY asset_title), '[]'::jsonb)
      FROM tmp_brief_assets
    )
  ),
  s.snapshot_hash,
  false
FROM tmp_brief_snapshot s;

-- =========================================================
-- 2. SOC / DFIR brief
-- =========================================================
INSERT INTO profile_briefs (
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  is_stale
)
SELECT
  'soc_dfir',
  concat_ws(
    E'\n',
    '# SOC / DFIR Profile Brief',
    '',
    'Positioning:',
    'Entry-level SOC, digital forensics, and incident response positioning based on controlled academic labs and approved project evidence. Strongest support comes from artifact analysis and live-response tooling; credential/password recovery and data security analytics can be used as supporting context, not as primary professional claims.',
    '',
    'Relevant approved capability claims:',
    COALESCE((
      SELECT string_agg(
        '- ' || capability_name || ' [' || strength_level || ']: ' || safe_resume_claim,
        E'\n'
        ORDER BY capability_name
      )
      FROM tmp_brief_capabilities
      WHERE capability_name IN (
        'Digital Forensics and Incident Response Tooling',
        'Credential and Password Recovery Workflows',
        'Data Security and Analytics Workflows'
      )
    ), '- No SOC/DFIR capabilities available.'),
    '',
    'Evidence-backed source assets:',
    COALESCE((
      SELECT string_agg(
        '- ' || a.asset_title || ': ' || left(COALESCE(a.job_oriented_summary, a.canonical_narrative), 600),
        E'\n'
        ORDER BY a.asset_title
      )
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name IN (
            'Digital Forensics and Incident Response Tooling',
            'Credential and Password Recovery Workflows',
            'Data Security and Analytics Workflows'
          )
      )
    ), '- No linked SOC/DFIR assets available.'),
    '',
    'Safe resume positioning:',
    '- Academic/project-based DFIR tooling experience with Autopsy, CAINE, RegRipper, HxD, Bulk Extractor Viewer, Redline, Magnet tools, and related live-response workflows where supported by assets.',
    '- Controlled lab exposure to password recovery workflows using John the Ripper and PDFCrack where relevant.',
    '- Data security analytics and encrypted data handling should be framed as supporting evidence, not as production SOC analytics experience.',
    '',
    'Do-not-overclaim rules:',
    '- Do not claim professional SOC analyst experience.',
    '- Do not claim production incident response ownership.',
    '- Do not claim expert forensic examiner capability.',
    '- Keep every claim tied to academic labs, coursework, or projects.',
    '',
    'Generator:',
    'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28'
  ),
  jsonb_build_object(
    'generator_version', 'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28',
    'source', 'approved_profile_assets_and_capabilities_only',
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(profile_capability_id::text ORDER BY capability_name), '[]'::jsonb)
      FROM tmp_brief_capabilities
      WHERE capability_name IN (
        'Digital Forensics and Incident Response Tooling',
        'Credential and Password Recovery Workflows',
        'Data Security and Analytics Workflows'
      )
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(a.profile_asset_id::text ORDER BY a.asset_title), '[]'::jsonb)
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name IN (
            'Digital Forensics and Incident Response Tooling',
            'Credential and Password Recovery Workflows',
            'Data Security and Analytics Workflows'
          )
      )
    )
  ),
  s.snapshot_hash,
  false
FROM tmp_brief_snapshot s
WHERE EXISTS (
  SELECT 1
  FROM tmp_brief_capabilities
  WHERE capability_name = 'Digital Forensics and Incident Response Tooling'
);

-- =========================================================
-- 3. Network security brief
-- =========================================================
INSERT INTO profile_briefs (
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  is_stale
)
SELECT
  'network_security',
  concat_ws(
    E'\n',
    '# Network Security Profile Brief',
    '',
    'Positioning:',
    'Entry-level network security positioning based on approved academic/project evidence in enterprise network controls simulation, protocol analysis, packet capture, routing/security validation, AAA/RADIUS, centralized logging, and security framework application.',
    '',
    'Relevant approved capability claims:',
    COALESCE((
      SELECT string_agg(
        '- ' || capability_name || ' [' || strength_level || ']: ' || safe_resume_claim,
        E'\n'
        ORDER BY capability_name
      )
      FROM tmp_brief_capabilities
      WHERE capability_name = 'Network Security Controls and Protocol Analysis'
    ), '- No network security capability available.'),
    '',
    'Evidence-backed source assets:',
    COALESCE((
      SELECT string_agg(
        '- ' || a.asset_title || ': ' || left(COALESCE(a.job_oriented_summary, a.canonical_narrative), 650),
        E'\n'
        ORDER BY a.asset_title
      )
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name = 'Network Security Controls and Protocol Analysis'
      )
    ), '- No linked network security assets available.'),
    '',
    'Safe resume positioning:',
    '- Academic/project-based experience with GNS3, Cisco/Arista/OpenSwitch simulation, FreeRADIUS, syslog-ng, NTP, tcpdump, ping, traceroute, and protocol/path validation where supported.',
    '- Stronger claim area: network controls and protocol analysis in controlled project environments.',
    '',
    'Do-not-overclaim rules:',
    '- Do not claim production network engineering or enterprise firewall administration.',
    '- Do not claim professional network security operations experience.',
    '- Do not imply ownership of real enterprise infrastructure.',
    '',
    'Generator:',
    'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28'
  ),
  jsonb_build_object(
    'generator_version', 'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28',
    'source', 'approved_profile_assets_and_capabilities_only',
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(profile_capability_id::text ORDER BY capability_name), '[]'::jsonb)
      FROM tmp_brief_capabilities
      WHERE capability_name = 'Network Security Controls and Protocol Analysis'
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(a.profile_asset_id::text ORDER BY a.asset_title), '[]'::jsonb)
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name = 'Network Security Controls and Protocol Analysis'
      )
    )
  ),
  s.snapshot_hash,
  false
FROM tmp_brief_snapshot s
WHERE EXISTS (
  SELECT 1
  FROM tmp_brief_capabilities
  WHERE capability_name = 'Network Security Controls and Protocol Analysis'
);

-- =========================================================
-- 4. AppSec entry-level brief
-- =========================================================
INSERT INTO profile_briefs (
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  is_stale
)
SELECT
  'appsec_entry_level',
  concat_ws(
    E'\n',
    '# Entry-Level Application Security Profile Brief',
    '',
    'Positioning:',
    'Entry-level application security positioning based on approved academic evidence in web application security testing using OWASP Juice Shop and related controlled training workflows.',
    '',
    'Relevant approved capability claims:',
    COALESCE((
      SELECT string_agg(
        '- ' || capability_name || ' [' || strength_level || ']: ' || safe_resume_claim,
        E'\n'
        ORDER BY capability_name
      )
      FROM tmp_brief_capabilities
      WHERE capability_name = 'Web Application Security Testing'
    ), '- No application security capability available.'),
    '',
    'Evidence-backed source assets:',
    COALESCE((
      SELECT string_agg(
        '- ' || a.asset_title || ': ' || left(COALESCE(a.job_oriented_summary, a.canonical_narrative), 650),
        E'\n'
        ORDER BY a.asset_title
      )
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name = 'Web Application Security Testing'
      )
    ), '- No linked AppSec assets available.'),
    '',
    'Safe resume positioning:',
    '- Academic/project-based exposure to OWASP Top 10 style web application security testing.',
    '- Use this as supporting AppSec evidence unless a job is explicitly entry-level or training-oriented.',
    '',
    'Do-not-overclaim rules:',
    '- Do not claim professional penetration testing experience.',
    '- Do not claim production application security ownership.',
    '- Do not claim bug bounty, client testing, or real-world exploitation unless separately supported.',
    '',
    'Generator:',
    'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28'
  ),
  jsonb_build_object(
    'generator_version', 'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28',
    'source', 'approved_profile_assets_and_capabilities_only',
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(profile_capability_id::text ORDER BY capability_name), '[]'::jsonb)
      FROM tmp_brief_capabilities
      WHERE capability_name = 'Web Application Security Testing'
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(a.profile_asset_id::text ORDER BY a.asset_title), '[]'::jsonb)
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name = 'Web Application Security Testing'
      )
    )
  ),
  s.snapshot_hash,
  false
FROM tmp_brief_snapshot s
WHERE EXISTS (
  SELECT 1
  FROM tmp_brief_capabilities
  WHERE capability_name = 'Web Application Security Testing'
);

-- =========================================================
-- 5. GRC / security analytics brief
-- =========================================================
INSERT INTO profile_briefs (
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  is_stale
)
SELECT
  'grc_security_analytics',
  concat_ws(
    E'\n',
    '# GRC / Security Analytics Profile Brief',
    '',
    'Positioning:',
    'Security governance, controls, and analytics positioning based on approved academic/project evidence. The strongest supported material comes from network controls/framework work and data security analytics; this should be framed as coursework/project evidence, not professional audit or compliance employment.',
    '',
    'Relevant approved capability claims:',
    COALESCE((
      SELECT string_agg(
        '- ' || capability_name || ' [' || strength_level || ']: ' || safe_resume_claim,
        E'\n'
        ORDER BY capability_name
      )
      FROM tmp_brief_capabilities
      WHERE capability_name IN (
        'Network Security Controls and Protocol Analysis',
        'Data Security and Analytics Workflows'
      )
    ), '- No GRC/security analytics capabilities available.'),
    '',
    'Evidence-backed source assets:',
    COALESCE((
      SELECT string_agg(
        '- ' || a.asset_title || ': ' || left(COALESCE(a.job_oriented_summary, a.canonical_narrative), 650),
        E'\n'
        ORDER BY a.asset_title
      )
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name IN (
            'Network Security Controls and Protocol Analysis',
            'Data Security and Analytics Workflows'
          )
      )
    ), '- No linked GRC/security analytics assets available.'),
    '',
    'Safe resume positioning:',
    '- Academic/project-based exposure to security controls, framework application, centralized logging concepts, encrypted data handling, and data analysis tooling.',
    '- Useful for entry-level GRC, security analyst, and risk/security operations support roles when phrased conservatively.',
    '',
    'Do-not-overclaim rules:',
    '- Do not claim professional audit, compliance, or risk ownership.',
    '- Do not claim SIEM or production analytics experience unless separately supported.',
    '- Do not claim certification-level GRC expertise.',
    '',
    'Generator:',
    'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28'
  ),
  jsonb_build_object(
    'generator_version', 'profile_brief_generator_v1_approved_assets_capabilities_2026_04_28',
    'source', 'approved_profile_assets_and_capabilities_only',
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(profile_capability_id::text ORDER BY capability_name), '[]'::jsonb)
      FROM tmp_brief_capabilities
      WHERE capability_name IN (
        'Network Security Controls and Protocol Analysis',
        'Data Security and Analytics Workflows'
      )
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(a.profile_asset_id::text ORDER BY a.asset_title), '[]'::jsonb)
      FROM tmp_brief_assets a
      WHERE EXISTS (
        SELECT 1
        FROM tmp_brief_links l
        WHERE l.profile_asset_id = a.profile_asset_id
          AND l.capability_name IN (
            'Network Security Controls and Protocol Analysis',
            'Data Security and Analytics Workflows'
          )
      )
    )
  ),
  s.snapshot_hash,
  false
FROM tmp_brief_snapshot s
WHERE EXISTS (
  SELECT 1
  FROM tmp_brief_capabilities
  WHERE capability_name IN (
    'Network Security Controls and Protocol Analysis',
    'Data Security and Analytics Workflows'
  )
);

COMMIT;
