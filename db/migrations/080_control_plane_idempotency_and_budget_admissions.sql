-- 080 -- Durable control-plane identities for retry-safe admissions and doc generation.
BEGIN;

CREATE TABLE IF NOT EXISTS budget_admissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  budget_date date NOT NULL,
  task_kind text NOT NULL CHECK (task_kind IN ('full_pipeline','browser_task','single_call')),
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  admitted_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (budget_date, task_kind, subject_type, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_budget_admissions_subject
  ON budget_admissions(subject_type, subject_id, admitted_at DESC);

CREATE TABLE IF NOT EXISTS document_generation_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  doc_type text NOT NULL CHECK (doc_type IN ('resume','cover_letter','short_answers')),
  idempotency_key text NOT NULL CHECK (length(idempotency_key)=64),
  request_kind text NOT NULL DEFAULT 'generation',
  input_manifest jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','failed','uncertain')),
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE(application_id, doc_type, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_document_generation_attempts_active
  ON document_generation_attempts(application_id, doc_type, status, updated_at DESC);

COMMIT;
