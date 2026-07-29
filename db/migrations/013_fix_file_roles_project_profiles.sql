-- 013_fix_file_roles_project_profiles.sql
-- Fix project/profile files that were too aggressively classified as course_reference_material.

UPDATE raw_files
SET
  file_role = 'project_artifact_evidence',
  evidence_weight = 0.80,
  allow_profile_fact_promotion = true,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'Project artifact/profile evidence; usable for candidate facts with human review.'
WHERE
  (
    file_name ILIKE '%Project%'
    OR file_name ILIKE '%Implementation_Details%'
    OR file_name ILIKE '%Project_Paper%'
    OR file_name ILIKE '%Pki Ocsp Mitm%'
    OR file_name ILIKE '%Attack Detection%'
    OR file_name ILIKE '%Privilege Escalation%'
    OR file_name ILIKE '%Nist Iso Enterprise%'
    OR file_name ILIKE '%Forensics Lab%'
  )
  AND file_role = 'course_reference_material';

UPDATE raw_files
SET
  file_role = 'enriched_profile_evidence',
  evidence_weight = 0.85,
  allow_profile_fact_promotion = true,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'User course/profile enrichment document; usable for candidate facts with human review.'
WHERE
  (
    file_name ILIKE '%Course Profile%'
    OR file_name ILIKE '%Strategic Profile%'
    OR file_name ILIKE '%Foundation Portfolio Profile%'
    OR file_name ILIKE '%Lecture Class Digital Forensics Profile%'
    OR file_name ILIKE '%Secure Software Web Security Profile%'
    OR file_name ILIKE '%Network Defense Countermeasures Profile%'
  )
  AND file_role = 'course_reference_material';

