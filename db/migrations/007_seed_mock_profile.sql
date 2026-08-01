-- DB-5: Seed a mock Profile Knowledge Layer
-- This validates raw_files -> chunks -> facts -> briefs -> context_packs.
-- Test data only.
--
-- FIXED 2026-08-01: the ON CONFLICT (input_hash) clause on the
-- profile_context_packs insert did not match
-- idx_profile_context_packs_input_hash, which is a PARTIAL unique index
-- (`... WHERE input_hash IS NOT NULL`, see 003_extended_schema.sql). A bare
-- `ON CONFLICT (input_hash)` cannot infer a partial index as its target,
-- so this always failed with "no unique or exclusion constraint matching
-- the ON CONFLICT specification" on every fresh install -- confirmed live
-- by a real WSL/Ubuntu install attempt. Added the matching
-- `WHERE input_hash IS NOT NULL` predicate, mirroring the fix that
-- 007_seed_mock_profile_fixed.sql already had (that sibling file's fix was
-- correct; this original file just never got the same fix applied to it).
--
-- Practical effect: on a fresh install this file now succeeds, and
-- 007_seed_mock_profile_fixed.sql running right after it re-applies the
-- same seed logic -- harmless for this table (mock/test data only, not
-- read by any application code), except it will insert one duplicate set
-- of 3 profile_chunks / 3 profile_facts / 1 profile_briefs row (those
-- three inserts have no ON CONFLICT guard). Not fixed here on purpose: it
-- is test fixture data for a single mock "Example Security Labs"
-- application, not something any real pipeline logic depends on being
-- exactly-once.

WITH target_app AS (
  SELECT id, jd_hash
  FROM applications
  WHERE company = 'Example Security Labs'
  ORDER BY created_at DESC
  LIMIT 1
),
raw AS (
  INSERT INTO raw_files (
    file_name,
    file_type,
    mime_type,
    storage_url,
    sha256,
    source,
    document_date,
    parse_status,
    parser_used,
    is_active
  )
  VALUES (
    'mock_profile_summary.md',
    'markdown',
    'text/markdown',
    'local://mock_profile_summary.md',
    encode(digest('mock_profile_summary_v1', 'sha256'), 'hex'),
    'mock_seed',
    current_date,
    'parsed',
    'manual_seed',
    true
  )
  ON CONFLICT (sha256) WHERE sha256 IS NOT NULL
  DO UPDATE SET
    parse_status = EXCLUDED.parse_status,
    parser_used = EXCLUDED.parser_used,
    is_active = true
  RETURNING id
),
chunks AS (
  INSERT INTO profile_chunks (
    file_id,
    chunk_index,
    section,
    category,
    text_content,
    page_number,
    token_count,
    metadata
  )
  SELECT
    raw.id,
    x.chunk_index,
    x.section,
    x.category,
    x.text_content,
    1,
    x.token_count,
    x.metadata::jsonb
  FROM raw,
  (
    VALUES
      (
        1,
        'Education',
        'academic',
        'User is pursuing a Bachelor of Science with majors in Computer Science and Cybersecurity. Relevant coursework includes Computer Networks, Operating Systems & Cybersecurity, Ethical Hacking & PenTesting, Cyber Forensics, Network Defenses, Data Structures, Algorithms, and Database Systems.',
        55,
        '{"source_type":"mock_profile"}'
      ),
      (
        2,
        'Skills',
        'skills',
        'User has technical foundations in Python, Java, SQL, Linux, networking, cybersecurity fundamentals, software engineering, algorithms, and database systems.',
        35,
        '{"source_type":"mock_profile"}'
      ),
      (
        3,
        'Positioning',
        'career_positioning',
        'For entry-level cybersecurity roles, user should be positioned as having a software engineering foundation plus cybersecurity specialization, with strengths in networking, security coursework, forensic thinking, and technical documentation.',
        38,
        '{"source_type":"mock_profile"}'
      )
  ) AS x(chunk_index, section, category, text_content, token_count, metadata)
  ON CONFLICT DO NOTHING
  RETURNING id, chunk_index, section, category, text_content
),
fact_seed AS (
  SELECT
    c.id AS chunk_id,
    c.section,
    c.category,
    c.text_content
  FROM chunks c
),
facts AS (
  INSERT INTO profile_facts (
    category,
    subcategory,
    fact_text,
    evidence_source,
    evidence_file_id,
    evidence_chunk_id,
    evidence_quote,
    confidence,
    approved_by_user,
    is_active,
    conflict_status
  )
  SELECT
    'academic',
    'degree_and_coursework',
    'User has an academic background combining Computer Science and Cybersecurity, with coursework in networks, operating systems, ethical hacking, cyber forensics, network defenses, algorithms, and databases.',
    'mock_profile_summary.md',
    raw.id,
    (SELECT id FROM profile_chunks WHERE file_id = raw.id AND section = 'Education' ORDER BY chunk_index LIMIT 1),
    'Relevant coursework includes Computer Networks, Operating Systems & Cybersecurity, Ethical Hacking & PenTesting, Cyber Forensics, Network Defenses, Data Structures, Algorithms, and Database Systems.',
    0.95,
    true,
    true,
    'no_conflict'
  FROM raw

  UNION ALL

  SELECT
    'skills',
    'technical_skills',
    'User has technical foundations in Python, Java, SQL, Linux, networking, cybersecurity fundamentals, software engineering, algorithms, and database systems.',
    'mock_profile_summary.md',
    raw.id,
    (SELECT id FROM profile_chunks WHERE file_id = raw.id AND section = 'Skills' ORDER BY chunk_index LIMIT 1),
    'technical foundations in Python, Java, SQL, Linux, networking, cybersecurity fundamentals, software engineering, algorithms, and database systems',
    0.92,
    true,
    true,
    'no_conflict'
  FROM raw

  UNION ALL

  SELECT
    'career_positioning',
    'cybersecurity_entry_level',
    'For entry-level cybersecurity roles, user should be positioned as software engineering foundation plus cybersecurity specialization.',
    'mock_profile_summary.md',
    raw.id,
    (SELECT id FROM profile_chunks WHERE file_id = raw.id AND section = 'Positioning' ORDER BY chunk_index LIMIT 1),
    'software engineering foundation plus cybersecurity specialization',
    0.93,
    true,
    true,
    'no_conflict'
  FROM raw
  RETURNING id, category, subcategory, fact_text
),
snapshot AS (
  SELECT encode(digest(string_agg(id::text, ',' ORDER BY id::text), 'sha256'), 'hex') AS facts_hash
  FROM profile_facts
  WHERE approved_by_user = true AND is_active = true
),
brief AS (
  INSERT INTO profile_briefs (
    brief_type,
    content,
    fact_ids_included,
    approved_facts_snapshot_hash,
    is_stale
  )
  SELECT
    'cybersecurity',
    'Mock cybersecurity profile brief: The user combines computer science fundamentals with cybersecurity specialization. Best positioning for entry-level cybersecurity roles: software engineering foundation, networking coursework, security coursework, forensic thinking, Python/SQL/Linux basics, and strong documentation potential.',
    (
      SELECT jsonb_agg(id)
      FROM profile_facts
      WHERE approved_by_user = true
        AND is_active = true
        AND category IN ('academic', 'skills', 'career_positioning')
    ),
    snapshot.facts_hash,
    false
  FROM snapshot
  RETURNING id
),
context_pack AS (
  INSERT INTO profile_context_packs (
    application_id,
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
    target_app.id,
    'resume',
    encode(digest(target_app.jd_hash || ':' || snapshot.facts_hash || ':resume', 'sha256'), 'hex'),
    target_app.jd_hash,
    snapshot.facts_hash,
    (
      SELECT jsonb_agg(id)
      FROM profile_facts
      WHERE approved_by_user = true
        AND is_active = true
    ),
    (
      SELECT jsonb_agg(id)
      FROM profile_chunks
      WHERE file_id = (SELECT id FROM raw)
    ),
    (
      SELECT jsonb_agg(id)
      FROM brief
    ),
    'Context pack for mock cybersecurity application: emphasize CS + cybersecurity coursework, networking, Linux, Python, SQL, security fundamentals, documentation, and avoid claiming professional SOC experience or certifications not evidenced.',
    95
  FROM target_app, snapshot
  ON CONFLICT (input_hash) WHERE input_hash IS NOT NULL
  DO UPDATE SET
    context_text = EXCLUDED.context_text,
    token_count = EXCLUDED.token_count
  RETURNING id
)
SELECT
  (SELECT id FROM raw) AS raw_file_id,
  (SELECT count(*) FROM profile_chunks WHERE file_id = (SELECT id FROM raw)) AS chunk_count,
  (SELECT count(*) FROM facts) AS inserted_fact_count,
  (SELECT id FROM brief) AS brief_id,
  (SELECT id FROM context_pack) AS context_pack_id;
