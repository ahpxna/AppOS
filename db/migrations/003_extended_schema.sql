-- Extended schema for Job Apply OS
-- Phase DB-1: profile knowledge, generated documents, messages, research, interviews, idempotency

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================
-- HARDEN EXISTING TABLES
-- =========================

ALTER TABLE browser_tasks
ADD COLUMN IF NOT EXISTS approval_request_id uuid REFERENCES approval_requests(id) ON DELETE SET NULL;

ALTER TABLE browser_tasks
ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_tasks_idempotency_key
ON browser_tasks(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_tasks_one_per_approval
ON browser_tasks(approval_request_id)
WHERE approval_request_id IS NOT NULL;

ALTER TABLE approval_requests
ADD COLUMN IF NOT EXISTS consumed_at timestamptz;

ALTER TABLE approval_requests
ADD COLUMN IF NOT EXISTS consumed_by text;

ALTER TABLE approval_requests
ADD COLUMN IF NOT EXISTS target_action text;

ALTER TABLE approval_requests
ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_idempotency_key
ON approval_requests(idempotency_key)
WHERE idempotency_key IS NOT NULL;

-- =========================
-- APPLICATION EVENTS / AUDIT LOG
-- =========================

CREATE TABLE IF NOT EXISTS application_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,

  event_type text NOT NULL,
  event_source text,
  event_payload jsonb DEFAULT '{}'::jsonb,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_application_events_application_id
ON application_events(application_id);

CREATE INDEX IF NOT EXISTS idx_application_events_type
ON application_events(event_type);

-- =========================
-- MESSAGE THREADS
-- =========================

CREATE TABLE IF NOT EXISTS message_threads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source text, -- email/linkedin/handshake
  external_thread_id text,

  company text,
  person_name text,
  person_role text,

  linked_application_id uuid REFERENCES applications(id) ON DELETE SET NULL,

  last_message_text text,
  last_message_at timestamptz,
  last_checked_at timestamptz,

  our_last_reply_id uuid,
  our_last_reply_at timestamptz,
  reply_count int DEFAULT 0,

  status text,
  needs_user_attention boolean DEFAULT false,
  priority text,
  classification text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_message_threads_source
ON message_threads(source);

CREATE INDEX IF NOT EXISTS idx_message_threads_linked_application_id
ON message_threads(linked_application_id);

CREATE INDEX IF NOT EXISTS idx_message_threads_needs_attention
ON message_threads(needs_user_attention);

DROP TRIGGER IF EXISTS trg_message_threads_updated_at ON message_threads;
CREATE TRIGGER trg_message_threads_updated_at
BEFORE UPDATE ON message_threads
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- =========================
-- RAW FILES
-- =========================

CREATE TABLE IF NOT EXISTS raw_files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  file_name text NOT NULL,
  file_type text,
  mime_type text,
  storage_url text,

  sha256 text,
  source text, -- upload/manual/import
  document_date date,

  parse_status text DEFAULT 'pending',
  parser_used text,
  parse_error text,

  is_active boolean DEFAULT true,

  uploaded_at timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_files_sha256
ON raw_files(sha256)
WHERE sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_files_parse_status
ON raw_files(parse_status);

-- =========================
-- PROFILE CHUNKS
-- =========================

CREATE TABLE IF NOT EXISTS profile_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  file_id uuid REFERENCES raw_files(id) ON DELETE CASCADE,

  chunk_index int,
  section text,
  category text,

  text_content text NOT NULL,
  page_number int,

  token_count int,
  metadata jsonb DEFAULT '{}'::jsonb,

  embedding vector,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_chunks_file_id
ON profile_chunks(file_id);

CREATE INDEX IF NOT EXISTS idx_profile_chunks_category
ON profile_chunks(category);

CREATE INDEX IF NOT EXISTS idx_profile_chunks_metadata
ON profile_chunks USING gin(metadata);

-- =========================
-- PROFILE FACTS
-- =========================

CREATE TABLE IF NOT EXISTS profile_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  category text,
  subcategory text,
  fact_text text NOT NULL,

  evidence_source text,
  evidence_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  evidence_chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,
  evidence_quote text,

  confidence numeric,
  approved_by_user boolean DEFAULT false,

  is_active boolean DEFAULT true,
  superseded_by_id uuid,

  conflict_group_id uuid,
  conflict_status text, -- winner/loser/pending_resolution/no_conflict

  used_in_applications int DEFAULT 0,
  expires_at timestamptz,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_facts_category
ON profile_facts(category, subcategory);

CREATE INDEX IF NOT EXISTS idx_profile_facts_approved_active
ON profile_facts(approved_by_user, is_active);

CREATE INDEX IF NOT EXISTS idx_profile_facts_conflict_group
ON profile_facts(conflict_group_id);

DROP TRIGGER IF EXISTS trg_profile_facts_updated_at ON profile_facts;
CREATE TRIGGER trg_profile_facts_updated_at
BEFORE UPDATE ON profile_facts
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- =========================
-- PROFILE BRIEFS
-- =========================

CREATE TABLE IF NOT EXISTS profile_briefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  brief_type text NOT NULL, -- cybersecurity/software/data/marketing/academic/master
  content text NOT NULL,

  fact_ids_included jsonb DEFAULT '[]'::jsonb,
  approved_facts_snapshot_hash text,

  generated_at timestamptz DEFAULT now(),
  is_stale boolean DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_profile_briefs_type
ON profile_briefs(brief_type);

CREATE INDEX IF NOT EXISTS idx_profile_briefs_stale
ON profile_briefs(is_stale);

-- =========================
-- PROFILE CONTEXT PACKS
-- =========================

CREATE TABLE IF NOT EXISTS profile_context_packs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  message_thread_id uuid REFERENCES message_threads(id) ON DELETE CASCADE,

  purpose text NOT NULL, -- resume/cover_letter/message_reply/interview_prep
  input_hash text,

  jd_hash text,
  approved_facts_snapshot_hash text,

  selected_fact_ids jsonb DEFAULT '[]'::jsonb,
  selected_chunk_ids jsonb DEFAULT '[]'::jsonb,
  selected_brief_ids jsonb DEFAULT '[]'::jsonb,

  context_text text,
  token_count int,

  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_context_packs_input_hash
ON profile_context_packs(input_hash)
WHERE input_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_profile_context_packs_application_id
ON profile_context_packs(application_id);

CREATE INDEX IF NOT EXISTS idx_profile_context_packs_purpose
ON profile_context_packs(purpose);

-- =========================
-- GENERATED DOCUMENTS
-- =========================

CREATE TABLE IF NOT EXISTS generated_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  message_thread_id uuid REFERENCES message_threads(id) ON DELETE CASCADE,

  doc_type text NOT NULL, -- resume/cover_letter/short_answers/email_reply/linkedin_reply
  version int DEFAULT 1,

  content text NOT NULL,
  format text DEFAULT 'markdown',

  fact_ids_used jsonb DEFAULT '[]'::jsonb,
  chunk_ids_used jsonb DEFAULT '[]'::jsonb,
  evidence_map jsonb DEFAULT '{}'::jsonb,

  qa_status text,
  approved boolean DEFAULT false,
  approved_at timestamptz,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generated_documents_application_id
ON generated_documents(application_id);

CREATE INDEX IF NOT EXISTS idx_generated_documents_message_thread_id
ON generated_documents(message_thread_id);

CREATE INDEX IF NOT EXISTS idx_generated_documents_type
ON generated_documents(doc_type);

-- =========================
-- COMPANY RESEARCH CACHE
-- =========================

CREATE TABLE IF NOT EXISTS company_research_cache (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  company_name text NOT NULL,
  company_domain text,

  summary text,
  mission text,
  products text,
  recent_news jsonb DEFAULT '[]'::jsonb,
  risks jsonb DEFAULT '[]'::jsonb,
  sources jsonb DEFAULT '[]'::jsonb,

  last_refreshed_at timestamptz,
  expires_at timestamptz,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_company_research_cache_name
ON company_research_cache(company_name);

CREATE INDEX IF NOT EXISTS idx_company_research_cache_domain
ON company_research_cache(company_domain);

CREATE INDEX IF NOT EXISTS idx_company_research_cache_expires
ON company_research_cache(expires_at);

-- =========================
-- INTERVIEWS
-- =========================

CREATE TABLE IF NOT EXISTS interviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,

  interview_type text, -- phone/technical/behavioral/onsite
  scheduled_at timestamptz,
  timezone text,

  interviewer_info jsonb DEFAULT '{}'::jsonb,
  prep_notes text,
  prep_package_id uuid,

  status text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_interviews_application_id
ON interviews(application_id);

CREATE INDEX IF NOT EXISTS idx_interviews_scheduled_at
ON interviews(scheduled_at);

DROP TRIGGER IF EXISTS trg_interviews_updated_at ON interviews;
CREATE TRIGGER trg_interviews_updated_at
BEFORE UPDATE ON interviews
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- =========================
-- SENSITIVE ANSWERS
-- =========================

CREATE TABLE IF NOT EXISTS sensitive_answers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  field_name text NOT NULL, -- race/gender/disability/veteran/visa/etc
  answer text NOT NULL,

  requires_review boolean DEFAULT true,
  approved_by_user boolean DEFAULT false,

  notes text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sensitive_answers_field_name
ON sensitive_answers(field_name);

DROP TRIGGER IF EXISTS trg_sensitive_answers_updated_at ON sensitive_answers;
CREATE TRIGGER trg_sensitive_answers_updated_at
BEFORE UPDATE ON sensitive_answers
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- =========================
-- SYSTEM SETTINGS
-- =========================

CREATE TABLE IF NOT EXISTS system_settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz DEFAULT now()
);

INSERT INTO system_settings (key, value)
VALUES
  ('architecture_version', '{"version":"db-1"}'::jsonb),
  ('browser_mode', '{"mode":"mock_until_openclaw_reconnected"}'::jsonb),
  ('approval_policy', '{"one_time_use":true,"final_submit_requires_separate_approval":true}'::jsonb)
ON CONFLICT (key)
DO UPDATE SET
  value = EXCLUDED.value,
  updated_at = now();

