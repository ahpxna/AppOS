-- Remove deterministic test fixtures that historical migrations inserted into
-- every database, including production.  Match every row by multiple exact
-- fixture markers so operator-created data is never selected by this cleanup.
BEGIN;

DELETE FROM browser_tasks
 WHERE task_type = 'test_open_url'
   AND requested_by = 'manual_seed'
   AND input_json->>'url' = 'https://example.com'
   AND input_json->>'note' = 'fake task to test browser_tasks queue';

DELETE FROM approval_requests
 WHERE type = 'mock_review_package'
   AND approval_channel = 'manual_db_test'
   AND payload_json->>'company' = 'Example Security Labs'
   AND payload_json->>'role' = 'Entry-Level Cybersecurity Analyst';

DELETE FROM message_threads
 WHERE source = 'email'
   AND external_thread_id = 'mock-thread-001'
   AND company = 'Example Security Labs'
   AND person_name = 'Mock Recruiter';

DELETE FROM company_research_cache
 WHERE company_name = 'Example Security Labs'
   AND company_domain = 'example.com'
   AND summary = 'Mock company summary for validating database design.';

DELETE FROM applications
 WHERE source = 'mock'
   AND company = 'Example Security Labs'
   AND job_title = 'Entry-Level Cybersecurity Analyst'
   AND job_url = 'https://example.com/jobs/security-analyst';

DELETE FROM profile_facts
 WHERE evidence_source = 'mock_profile_summary.md'
   AND evidence_quote IS NOT NULL
   AND category IN ('academic', 'skills', 'career_positioning');

DELETE FROM profile_briefs
 WHERE content LIKE 'Mock cybersecurity profile brief:%';

DELETE FROM raw_files
 WHERE source = 'mock_seed'
   AND file_name = 'mock_profile_summary.md'
   AND storage_url = 'local://mock_profile_summary.md'
   AND sha256 = encode(digest('mock_profile_summary_v1', 'sha256'), 'hex');

DELETE FROM allowed_domains
 WHERE domain = 'example.com'
   AND category = 'test'
   AND notes = 'Used by the worker smoke test.';

COMMIT;
