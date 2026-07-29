BEGIN;

-- =========================================================
-- Profile Context Pack Builder V1
-- Purpose:
--   Build reusable base context packs from approved profile_briefs,
--   approved profile_capabilities, and approved profile_assets.
--
-- Important:
--   This is not JD-specific yet.
--   application_id = NULL
--   message_thread_id = NULL
--   jd_hash = NULL
--
-- Does NOT use:
--   candidate_profile_facts
--   unapproved assets
--   old tiny-fact truth path
-- =========================================================

CREATE TEMP TABLE tmp_pack_briefs AS
SELECT
  id AS brief_id,
  brief_type,
  content,
  fact_ids_included,
  approved_facts_snapshot_hash,
  generated_at
FROM profile_briefs
WHERE is_stale = false
  AND brief_type IN (
    'cybersecurity_general',
    'soc_dfir',
    'network_security',
    'appsec_entry_level',
    'grc_security_analytics'
  );

CREATE TEMP TABLE tmp_pack_snapshot AS
SELECT
  COALESCE(
    (SELECT approved_facts_snapshot_hash FROM tmp_pack_briefs LIMIT 1),
    md5(now()::text)
  ) AS snapshot_hash;

DO $$
DECLARE
  brief_count integer;
BEGIN
  SELECT count(*) INTO brief_count FROM tmp_pack_briefs;

  IF brief_count = 0 THEN
    RAISE EXCEPTION 'No fresh profile_briefs found. Generate profile_briefs before building context packs.';
  END IF;
END $$;

-- Keep this deterministic and idempotent.
DELETE FROM profile_context_packs
WHERE application_id IS NULL
  AND message_thread_id IS NULL
  AND purpose IN (
    'base_resume_generation',
    'base_cover_letter_generation',
    'base_short_answer_generation',
    'base_interview_prep',
    'base_message_reply',
    'base_fit_check_support'
  );

-- =========================================================
-- 1. Resume generation pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_resume_generation',
  md5('base_resume_generation::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'soc_dfir',
      'network_security',
      'appsec_entry_level',
      'grc_security_analytics'
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(DISTINCT asset_id), '[]'::jsonb)
      FROM (
        SELECT jsonb_array_elements_text(fact_ids_included->'asset_ids') AS asset_id
        FROM tmp_pack_briefs
      ) x
    ),
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(DISTINCT capability_id), '[]'::jsonb)
      FROM (
        SELECT jsonb_array_elements_text(fact_ids_included->'capability_ids') AS capability_id
        FROM tmp_pack_briefs
      ) x
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
    WHERE brief_type IN (
      'cybersecurity_general',
      'soc_dfir',
      'network_security',
      'appsec_entry_level',
      'grc_security_analytics'
    )
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE RESUME GENERATION',
    'Use this pack to draft or tailor resumes. Claims must remain bounded to approved academic/project evidence. Do not convert coursework, labs, or projects into professional employment.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'appsec_entry_level'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'grc_security_analytics'),
    'GLOBAL DO-NOT-OVERCLAIM RULES:
- Do not claim professional SOC, DFIR, network engineering, penetration testing, audit, compliance, or production security operations experience unless separately supported.
- Prefer wording such as academic/project-based, controlled lab, coursework-supported, simulated environment, and evidence-backed exposure.
- Every resume bullet must map back to an approved profile asset or capability.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE RESUME GENERATION',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs)
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

-- =========================================================
-- 2. Cover letter generation pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_cover_letter_generation',
  md5('base_cover_letter_generation::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'network_security',
      'soc_dfir',
      'grc_security_analytics'
    ),
    'asset_ids', (
      SELECT COALESCE(jsonb_agg(DISTINCT asset_id), '[]'::jsonb)
      FROM (
        SELECT jsonb_array_elements_text(fact_ids_included->'asset_ids') AS asset_id
        FROM tmp_pack_briefs
        WHERE brief_type IN (
          'cybersecurity_general',
          'network_security',
          'soc_dfir',
          'grc_security_analytics'
        )
      ) x
    ),
    'capability_ids', (
      SELECT COALESCE(jsonb_agg(DISTINCT capability_id), '[]'::jsonb)
      FROM (
        SELECT jsonb_array_elements_text(fact_ids_included->'capability_ids') AS capability_id
        FROM tmp_pack_briefs
        WHERE brief_type IN (
          'cybersecurity_general',
          'network_security',
          'soc_dfir',
          'grc_security_analytics'
        )
      ) x
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
    WHERE brief_type IN (
      'cybersecurity_general',
      'network_security',
      'soc_dfir',
      'grc_security_analytics'
    )
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE COVER LETTER GENERATION',
    'Use this pack to write cover letters. It should support a credible story, not dump every tool. Choose only the capabilities relevant to the JD/company.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'grc_security_analytics'),
    'COVER LETTER POSITIONING RULES:
- Open with role/company fit, then connect to one or two approved capability clusters.
- Use conservative phrasing: academic projects, controlled labs, simulated enterprise environments.
- Do not over-list tools unless the JD explicitly asks for them.
- Do not invent passion, employment history, certifications, or production experience.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE COVER LETTER GENERATION',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs WHERE brief_type IN ('cybersecurity_general','network_security','soc_dfir','grc_security_analytics'))
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

-- =========================================================
-- 3. Short answer generation pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_short_answer_generation',
  md5('base_short_answer_generation::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'network_security',
      'soc_dfir',
      'appsec_entry_level',
      'grc_security_analytics'
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE SHORT ANSWER GENERATION',
    'Use this pack for application form answers such as “Why this role?”, “Tell us about your experience”, or “Describe a relevant project”. Keep answers concise and evidence-bound.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'appsec_entry_level'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'grc_security_analytics'),
    'SHORT ANSWER RULES:
- Use one specific project/tool workflow per answer where possible.
- Do not claim employment, production ownership, or certification.
- If asked about unavailable experience, say the closest supported academic/project evidence instead of fabricating.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE SHORT ANSWER GENERATION',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs)
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

-- =========================================================
-- 4. Interview prep pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_interview_prep',
  md5('base_interview_prep::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'soc_dfir',
      'network_security',
      'appsec_entry_level',
      'grc_security_analytics'
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE INTERVIEW PREP',
    'Use this pack to generate interview prep, STAR stories, technical refreshers, and claim-safe talking points.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'appsec_entry_level'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'grc_security_analytics'),
    'INTERVIEW RULES:
- Convert each approved capability into one or more bounded interview stories.
- Clearly separate “I used in coursework/lab” from “I used professionally”.
- Prepare honest gap answers for SIEM, production SOC, cloud security, and certifications if unsupported.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE INTERVIEW PREP',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs)
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

-- =========================================================
-- 5. Message reply pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_message_reply',
  md5('base_message_reply::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'soc_dfir',
      'network_security'
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
    WHERE brief_type IN (
      'cybersecurity_general',
      'soc_dfir',
      'network_security'
    )
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE MESSAGE REPLY',
    'Use this pack for recruiter replies, follow-ups, interview scheduling context, and concise capability summaries. Do not send without approval.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    'MESSAGE REPLY RULES:
- Be concise.
- Do not disclose unnecessary personal or sensitive information.
- Do not claim unsupported work authorization, certifications, professional experience, or availability.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE MESSAGE REPLY',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs WHERE brief_type IN ('cybersecurity_general','soc_dfir','network_security'))
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

-- =========================================================
-- 6. Fit check support pack
-- =========================================================
INSERT INTO profile_context_packs (
  application_id,
  message_thread_id,
  purpose,
  input_hash,
  jd_hash,
  approved_facts_snapshot_hash,
  selected_fact_ids,
  selected_chunk_ids,
  selected_brief_ids,
  context_text,
  token_count
)
SELECT
  NULL,
  NULL,
  'base_fit_check_support',
  md5('base_fit_check_support::' || s.snapshot_hash || '::profile_context_pack_builder_v1_2026_04_28'),
  NULL,
  s.snapshot_hash,

  jsonb_build_object(
    'source', 'approved_profile_briefs_assets_capabilities_only',
    'generator_version', 'profile_context_pack_builder_v1_2026_04_28',
    'brief_types', jsonb_build_array(
      'cybersecurity_general',
      'soc_dfir',
      'network_security',
      'appsec_entry_level',
      'grc_security_analytics'
    )
  ),

  '[]'::jsonb,

  (
    SELECT COALESCE(jsonb_agg(brief_id::text ORDER BY brief_type), '[]'::jsonb)
    FROM tmp_pack_briefs
  ),

  concat_ws(
    E'\n\n---\n\n',
    'PROFILE CONTEXT PACK: BASE FIT CHECK SUPPORT',
    'Use this pack to compare a JD against approved capabilities. This pack is for fit scoring support, not document generation.',
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'cybersecurity_general'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'network_security'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'soc_dfir'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'appsec_entry_level'),
    (SELECT content FROM tmp_pack_briefs WHERE brief_type = 'grc_security_analytics'),
    'FIT CHECK RULES:
- Strong fit: entry-level cybersecurity, SOC, network security, DFIR, GRC/security analyst, or AppSec training roles that accept academic/project evidence.
- Medium fit: roles requiring some unsupported tools but learnable quickly.
- Low fit: roles requiring years of production experience, active certifications, clearance/citizenship requirements, or unsupported professional SOC/cloud/SIEM ownership.
- Flag every unsupported required skill clearly.'
  ),

  GREATEST(
    1,
    length(
      concat_ws(
        E'\n\n---\n\n',
        'PROFILE CONTEXT PACK: BASE FIT CHECK SUPPORT',
        (SELECT string_agg(content, E'\n\n---\n\n' ORDER BY brief_type) FROM tmp_pack_briefs)
      )
    ) / 4
  )::integer
FROM tmp_pack_snapshot s;

COMMIT;
