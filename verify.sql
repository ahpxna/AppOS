-- verify.sql - kiem tra suc khoe pipeline JobOS
--
-- Chay:
--   docker exec -i jobos-postgres psql -U jobos -d job_apply_os -P pager=off \
--     < jobos_patch/verify.sql
--
-- Chay TRUOC khi va  -> chup anh hien trang
-- Chay SAU khi va lai -> so sanh, kiem tra CHECK 1 phai RONG

\echo ''
\echo '=========================================================='
\echo 'CHECK 1  [CRITICAL]  Tool exposure-only bi gan claim manh'
\echo '         KY VONG SAU KHI VA + CHAY LAI: 0 dong'
\echo '=========================================================='
SELECT
  tool_name,
  claim_type,
  evidence_strength,
  left(coalesce(resume_safe_phrase, claim, ''), 90) AS phrase
FROM profile_evidence_units
WHERE evidence_strength IN ('direct_lab_use', 'project_use')
  AND (
    lower(tool_name) LIKE '%metasploit%' OR
    lower(tool_name) LIKE '%sqlmap%'     OR
    lower(tool_name) LIKE '%nessus%'     OR
    lower(tool_name) LIKE '%openvas%'    OR
    lower(tool_name) LIKE '%hydra%'      OR
    lower(tool_name) LIKE '%patator%'    OR
    lower(tool_name) LIKE '%hashcat%'    OR
    lower(tool_name) LIKE '%zap%'        OR
    lower(tool_name) LIKE '%burp%'       OR
    lower(tool_name) LIKE '%wireshark%'  OR
    lower(tool_name) LIKE '%splunk%'
  )
ORDER BY tool_name;

\echo ''
\echo '=========================================================='
\echo 'CHECK 2  Phan bo evidence_strength'
\echo '         Truoc patch: project_use/direct_lab_use chiem da so'
\echo '=========================================================='
SELECT evidence_strength, count(*) AS n
FROM profile_evidence_units
GROUP BY 1 ORDER BY n DESC;

\echo ''
\echo '=========================================================='
\echo 'CHECK 3  tool_name la rac (tieu de doc lot vao)'
\echo '         KY VONG SAU: 0 dong'
\echo '=========================================================='
SELECT id, tool_name
FROM profile_evidence_units
WHERE tool_name ~ '^[A-Z][A-Z &]{12,}$'      -- TOAN CHU HOA = tieu de tai lieu
   OR lower(tool_name) LIKE '%narrative%'
   OR lower(tool_name) LIKE '%mapping%'
   OR lower(tool_name) LIKE '%source mapping%'
   OR length(tool_name) > 60;

\echo ''
\echo '=========================================================='
\echo 'CHECK 4  do_not_overclaim_rules con danh tu tran / enum'
\echo '         KY VONG SAU: 0 dong'
\echo '=========================================================='
SELECT
  pa.id,
  left(pa.asset_title, 40) AS asset,
  r.rule
FROM profile_assets pa
CROSS JOIN LATERAL unnest(pa.do_not_overclaim_rules) AS r(rule)
WHERE r.rule !~* '^(do not|don''t|never|must not|avoid|keep|bounded to|limit)'
ORDER BY pa.id;

\echo ''
\echo '=========================================================='
\echo 'CHECK 5  Chat luong section title (bug chunk)'
\echo '         Title trung lap nhieu = heading detector bat nham'
\echo '=========================================================='
SELECT
  section_title,
  count(*) AS n
FROM profile_document_sections
GROUP BY 1
HAVING count(*) > 2
ORDER BY n DESC
LIMIT 15;

\echo ''
\echo '=========================================================='
\echo 'CHECK 6  Do phu: bao nhieu file that su di het pipeline'
\echo '=========================================================='
SELECT
  rf.file_name,
  count(DISTINCT pds.id) AS sections,
  count(DISTINCT peu.id) AS evidence_units
FROM raw_files rf
LEFT JOIN profile_document_sections pds ON pds.raw_file_id = rf.id
LEFT JOIN profile_evidence_units peu    ON peu.raw_file_id = rf.id
WHERE rf.is_active
GROUP BY rf.file_name
ORDER BY sections DESC, rf.file_name;

\echo ''
\echo '=========================================================='
\echo 'CHECK 7  Asset va trang thai duyet'
\echo '=========================================================='
SELECT status, count(*) FROM profile_assets GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=========================================================='
\echo 'CHECK 8  File parse ra RONG (bug H1 - import mem)'
\echo '         KY VONG: 0 dong'
\echo '=========================================================='
SELECT id, file_name, parser_used, parse_error
FROM raw_files
WHERE is_active
  AND (
    parser_used IN ('python-docx_missing', 'pypdf_missing')
    OR parse_error IS NOT NULL
  );

\echo ''
\echo '=========================================================='
\echo 'CHECK 9  Chunk chua embed'
\echo '=========================================================='
SELECT
  count(*) FILTER (WHERE pce.id IS NULL) AS missing_embedding,
  count(*) FILTER (WHERE pce.id IS NOT NULL) AS embedded
FROM profile_chunks pc
LEFT JOIN profile_chunk_embeddings pce ON pce.chunk_id = pc.id;

\echo ''
\echo '=========================================================='
\echo 'CHECK 10 Schema xung dot: profile_asset_audits'
\echo '         Neu THIEU audit_status -> migration 031 se gay'
\echo '         khi dung lai DB tu dau'
\echo '=========================================================='
SELECT
  bool_or(column_name = 'audit_type')   AS has_027_schema,
  bool_or(column_name = 'audit_status') AS has_031_schema
FROM information_schema.columns
WHERE table_name = 'profile_asset_audits';

\echo ''
\echo '=== HET ==='
