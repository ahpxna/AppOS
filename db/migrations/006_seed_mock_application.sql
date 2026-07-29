-- DB-4: Seed a mock end-to-end application to validate relational design.
-- This is test data only. It does not contact any real website.

WITH app AS (
  INSERT INTO applications (
    source,
    company,
    job_title,
    job_url,
    jd_text,
    jd_hash,
    current_step,
    status,
    fit_score,
    fit_decision,
    priority,
    ats_type,
    deadline,
    salary_range,
    work_mode,
    location,
    seniority_level
  )
  VALUES (
    'mock',
    'Example Security Labs',
    'Entry-Level Cybersecurity Analyst',
    'https://example.com/jobs/security-analyst',
    'Mock JD: entry-level cybersecurity analyst role requiring networking, Linux, Python, incident response fundamentals, and clear documentation.',
    encode(digest('Example Security Labs Entry-Level Cybersecurity Analyst mock JD', 'sha256'), 'hex'),
    'waiting_user_review',
    'draft_package_ready',
    82,
    'approve_research',
    'high',
    'greenhouse',
    current_date + interval '14 days',
    '$65,000-$80,000',
    'hybrid',
    'New York, NY',
    'entry_level'
  )
  RETURNING id
),
research AS (
  INSERT INTO company_research_cache (
    company_name,
    company_domain,
    summary,
    mission,
    products,
    recent_news,
    risks,
    sources,
    last_refreshed_at,
    expires_at
  )
  SELECT
    'Example Security Labs',
    'example.com',
    'Mock company summary for validating database design.',
    'Mock mission: help organizations improve security readiness.',
    'Mock products: security monitoring, incident response tooling, compliance dashboards.',
    '[]'::jsonb,
    '[]'::jsonb,
    '[{"type":"mock","url":"https://example.com"}]'::jsonb,
    now(),
    now() + interval '30 days'
  RETURNING id
),
doc AS (
  INSERT INTO generated_documents (
    application_id,
    doc_type,
    version,
    content,
    format,
    fact_ids_used,
    chunk_ids_used,
    evidence_map,
    qa_status,
    approved
  )
  SELECT
    app.id,
    'resume',
    1,
    '# Mock Tailored Resume\n\nThis is a placeholder resume draft for database validation only.',
    'markdown',
    '[]'::jsonb,
    '[]'::jsonb,
    '{"note":"mock evidence map; real documents will require approved profile facts"}'::jsonb,
    'pending_review',
    false
  FROM app
  RETURNING id, application_id
),
msg AS (
  INSERT INTO message_threads (
    source,
    external_thread_id,
    company,
    person_name,
    person_role,
    linked_application_id,
    last_message_text,
    last_message_at,
    last_checked_at,
    reply_count,
    status,
    needs_user_attention,
    priority,
    classification
  )
  SELECT
    'email',
    'mock-thread-001',
    'Example Security Labs',
    'Mock Recruiter',
    'Recruiter',
    app.id,
    'Mock recruiter message: Thanks for your application. Are you available for a short phone screen next week?',
    now(),
    now(),
    0,
    'draft_needed',
    true,
    'high',
    'interview_invite'
  FROM app
  RETURNING id, linked_application_id
),
approval AS (
  INSERT INTO approval_requests (
    type,
    application_id,
    payload_json,
    status,
    approval_channel,
    approval_token_hash,
    token_expires_at,
    target_action,
    idempotency_key,
    notified_at
  )
  SELECT
    'mock_review_package',
    app.id,
    jsonb_build_object(
      'purpose', 'Mock approval for reviewing generated resume package',
      'company', 'Example Security Labs',
      'role', 'Entry-Level Cybersecurity Analyst',
      'risk_level', 'low'
    ),
    'pending',
    'manual_db_test',
    encode(digest(encode(gen_random_bytes(32), 'hex'), 'sha256'), 'hex'),
    now() + interval '30 minutes',
    'review_generated_documents',
    'mock_review_package:' || app.id::text,
    now()
  FROM app
  RETURNING id, application_id
),
interview AS (
  INSERT INTO interviews (
    application_id,
    interview_type,
    scheduled_at,
    timezone,
    interviewer_info,
    prep_notes,
    status
  )
  SELECT
    app.id,
    'phone',
    now() + interval '7 days',
    'America/New_York',
    '{"name":"Mock Recruiter","role":"Recruiter"}'::jsonb,
    'Mock prep notes placeholder. Real prep package will be generated later.',
    'prep_needed'
  FROM app
  RETURNING id, application_id
),
events AS (
  INSERT INTO application_events (
    application_id,
    event_type,
    event_source,
    event_payload
  )
  SELECT app.id, 'mock_application_seeded', 'db_migration_006',
         '{"note":"Seeded mock application to validate relational schema."}'::jsonb
  FROM app
  UNION ALL
  SELECT doc.application_id, 'mock_resume_draft_created', 'db_migration_006',
         jsonb_build_object('generated_document_id', doc.id)
  FROM doc
  UNION ALL
  SELECT msg.linked_application_id, 'mock_message_thread_created', 'db_migration_006',
         jsonb_build_object('message_thread_id', msg.id)
  FROM msg
  UNION ALL
  SELECT approval.application_id, 'mock_approval_created', 'db_migration_006',
         jsonb_build_object('approval_request_id', approval.id)
  FROM approval
  UNION ALL
  SELECT interview.application_id, 'mock_interview_created', 'db_migration_006',
         jsonb_build_object('interview_id', interview.id)
  FROM interview
)
SELECT
  app.id AS application_id,
  doc.id AS generated_document_id,
  msg.id AS message_thread_id,
  approval.id AS approval_request_id,
  interview.id AS interview_id
FROM app, doc, msg, approval, interview;
