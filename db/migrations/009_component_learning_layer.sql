-- 009_component_learning_layer.sql
-- Generic learning/logging layer for all system components.
-- Trainable agents get feedback/training examples.
-- Non-trainable tools/services/runtimes get run logs only.

CREATE TABLE IF NOT EXISTS component_registry (
  name text PRIMARY KEY,

  component_type text NOT NULL,
  -- agent / service / router / tool / runtime / workflow / safety / db_worker

  layer text,
  purpose text,

  trainable boolean NOT NULL DEFAULT false,

  status text NOT NULL DEFAULT 'planned',
  -- planned / prototype / active / deprecated

  notes text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS component_prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_name text NOT NULL REFERENCES component_registry(name) ON DELETE CASCADE,
  prompt_version text NOT NULL,

  model_provider text,
  model_name text,

  system_prompt text,
  developer_prompt text,
  output_schema jsonb,

  is_active boolean DEFAULT false,
  notes text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(component_name, prompt_version)
);

CREATE TABLE IF NOT EXISTS component_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_name text NOT NULL REFERENCES component_registry(name) ON DELETE RESTRICT,
  task_type text NOT NULL,

  prompt_version_id uuid REFERENCES component_prompt_versions(id) ON DELETE SET NULL,

  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  message_thread_id uuid REFERENCES message_threads(id) ON DELETE SET NULL,
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,

  source_file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,
  source_chunk_id uuid REFERENCES profile_chunks(id) ON DELETE SET NULL,
  source_candidate_fact_id uuid REFERENCES candidate_profile_facts(id) ON DELETE SET NULL,

  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_text text,

  status text NOT NULL DEFAULT 'completed',
  error_message text,

  model_provider text,
  model_name text,
  input_tokens integer,
  output_tokens integer,
  estimated_cost_usd numeric,

  created_at timestamptz DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_component_runs_component_task
ON component_runs(component_name, task_type);

CREATE INDEX IF NOT EXISTS idx_component_runs_application
ON component_runs(application_id);

CREATE INDEX IF NOT EXISTS idx_component_runs_message_thread
ON component_runs(message_thread_id);

CREATE INDEX IF NOT EXISTS idx_component_runs_source_chunk
ON component_runs(source_chunk_id);

CREATE TABLE IF NOT EXISTS component_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  component_name text NOT NULL REFERENCES component_registry(name) ON DELETE RESTRICT,
  task_type text NOT NULL,

  feedback_source text NOT NULL DEFAULT 'human',
  reviewer text DEFAULT 'user',

  decision text NOT NULL,
  -- good / bad / needs_edit / approved / rejected / qa_pass / qa_fail

  score numeric,
  review_note text,

  corrected_output_json jsonb,
  corrected_output_text text,

  usable_for_prompt boolean DEFAULT true,
  usable_for_finetune boolean DEFAULT false,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_component_feedback_component_task
ON component_feedback(component_name, task_type);

CREATE INDEX IF NOT EXISTS idx_component_feedback_decision
ON component_feedback(decision);

CREATE INDEX IF NOT EXISTS idx_component_feedback_run
ON component_feedback(component_run_id);

CREATE TABLE IF NOT EXISTS component_training_examples (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source_feedback_id uuid REFERENCES component_feedback(id) ON DELETE SET NULL,
  source_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  component_name text NOT NULL REFERENCES component_registry(name) ON DELETE RESTRICT,
  task_type text NOT NULL,

  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  positive_output_json jsonb,
  positive_output_text text,

  negative_output_json jsonb,
  negative_output_text text,

  corrected_output_json jsonb,
  corrected_output_text text,

  label text NOT NULL,
  -- positive / negative / edit / preference

  rationale text,

  split text NOT NULL DEFAULT 'train',
  -- train / validation / test

  quality_score numeric,

  usable_for_prompt boolean DEFAULT true,
  usable_for_finetune boolean DEFAULT false,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_component_training_examples_component_task
ON component_training_examples(component_name, task_type);

CREATE INDEX IF NOT EXISTS idx_component_training_examples_label
ON component_training_examples(label);

CREATE INDEX IF NOT EXISTS idx_component_training_examples_split
ON component_training_examples(split);

CREATE INDEX IF NOT EXISTS idx_component_training_examples_source_run
ON component_training_examples(source_run_id);

CREATE OR REPLACE VIEW v_component_learning_status AS
SELECT
  cr.name AS component_name,
  cr.component_type,
  cr.layer,
  cr.trainable,
  cr.status,
  count(DISTINCT runs.id) AS run_count,
  count(DISTINCT fb.id) AS feedback_count,
  count(DISTINCT ex.id) AS training_example_count,
  count(DISTINCT ex.id) FILTER (WHERE ex.label = 'positive') AS positive_examples,
  count(DISTINCT ex.id) FILTER (WHERE ex.label = 'negative') AS negative_examples,
  count(DISTINCT ex.id) FILTER (WHERE ex.label = 'edit') AS edit_examples,
  max(runs.created_at) AS last_run_at,
  max(fb.created_at) AS last_feedback_at
FROM component_registry cr
LEFT JOIN component_runs runs
  ON runs.component_name = cr.name
LEFT JOIN component_feedback fb
  ON fb.component_name = cr.name
LEFT JOIN component_training_examples ex
  ON ex.component_name = cr.name
GROUP BY cr.name, cr.component_type, cr.layer, cr.trainable, cr.status
ORDER BY cr.layer, cr.component_type, cr.name;

CREATE OR REPLACE VIEW v_trainable_agents AS
SELECT
  name,
  layer,
  status,
  purpose,
  notes
FROM component_registry
WHERE trainable = true
ORDER BY layer, name;

CREATE OR REPLACE VIEW v_component_training_examples AS
SELECT
  ex.id,
  ex.component_name,
  cr.component_type,
  cr.layer,
  cr.trainable,
  ex.task_type,
  ex.label,
  ex.split,
  ex.quality_score,
  ex.usable_for_prompt,
  ex.usable_for_finetune,
  ex.input_json,
  ex.positive_output_json,
  ex.negative_output_json,
  ex.corrected_output_json,
  ex.rationale,
  ex.created_at
FROM component_training_examples ex
JOIN component_registry cr
  ON cr.name = ex.component_name
ORDER BY ex.created_at DESC;

-- =========================
-- Seed trainable agents
-- =========================
INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES
  (
    'candidate_fact_extractor',
    'agent',
    'profile',
    'Extract evidence-grounded candidate facts from profile chunks.',
    true,
    'prototype',
    'Local Ollama V2 is working; evidence_quote is required.'
  ),
  (
    'semantic_dedup_worker',
    'agent',
    'profile',
    'Merge or deduplicate semantically similar candidate facts before conflict resolution.',
    true,
    'planned',
    'May use embeddings, rules, or LLM judgment. Training examples should focus on merge/keep/reject decisions.'
  ),
  (
    'fit_checker_jd_analyzer',
    'agent',
    'job_analysis',
    'Parse job description, compare requirements against approved profile facts/context, compute fit score, and decide whether research is needed.',
    true,
    'planned',
    'Owns the research decision. Does not do deep company research itself.'
  ),
  (
    'profile_aware_worker',
    'agent',
    'generation',
    'Central planning worker that reads job description, research output, and profile context pack, then assigns strategy to resume, cover letter, and short-answer agents.',
    true,
    'planned',
    'This is the big-picture worker. It should not read all raw files.'
  ),
  (
    'resume_agent',
    'agent',
    'generation',
    'Generate tailored resume drafts using profile context pack and evidence map.',
    true,
    'planned',
    NULL
  ),
  (
    'cover_letter_agent',
    'agent',
    'generation',
    'Generate tailored cover letters using profile context pack, role angle, and company context.',
    true,
    'planned',
    NULL
  ),
  (
    'short_answer_agent',
    'agent',
    'generation',
    'Generate application form short answers using approved facts and role-specific strategy.',
    true,
    'planned',
    NULL
  ),
  (
    'truth_quality_checker',
    'agent',
    'qa',
    'Check generated documents and answers against evidence, role requirements, style rules, and hallucination risks.',
    true,
    'planned',
    NULL
  ),
  (
    'message_classifier',
    'agent',
    'messaging',
    'Classify incoming email, LinkedIn, and Handshake messages.',
    true,
    'planned',
    NULL
  ),
  (
    'message_reply_agent',
    'agent',
    'messaging',
    'Draft replies for email, LinkedIn, and Handshake messages.',
    true,
    'planned',
    'Sending should require approval.'
  ),
  (
    'interview_prep_agent',
    'agent',
    'interview',
    'Generate interview prep packages from job description, company context, profile facts, and message thread.',
    true,
    'planned',
    NULL
  ),
  (
    'autofill_agent',
    'agent',
    'browser',
    'Map application fields to approved user/profile/application data and prepare browser fill actions.',
    true,
    'planned',
    'Train on field mapping and safe decision logic. Do not store unnecessary raw sensitive data.'
  )
ON CONFLICT (name)
DO UPDATE SET
  component_type = EXCLUDED.component_type,
  layer = EXCLUDED.layer,
  purpose = EXCLUDED.purpose,
  trainable = EXCLUDED.trainable,
  status = EXCLUDED.status,
  notes = EXCLUDED.notes,
  updated_at = now();

-- =========================
-- Seed non-trainable components / tools / services
-- =========================
INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES
  (
    'n8n_orchestrator',
    'workflow',
    'control',
    'Deterministic state machine that moves applications through current_step/status and calls agents/tools only when needed.',
    false,
    'prototype',
    'n8n is running and manual DB tests passed.'
  ),
  (
    'approval_service',
    'safety',
    'control',
    'Create, verify, expire, and consume approval tokens.',
    false,
    'prototype',
    'Manual approval prototype works. Telegram UI is not built yet.'
  ),
  (
    'cost_controller',
    'safety',
    'control',
    'Enforce per-day and per-job budgets, throttle expensive steps, and log token/cost usage.',
    false,
    'planned',
    'Policy rows exist, enforcement is incomplete.'
  ),
  (
    'no_llm_filter',
    'service',
    'control',
    'Cheap deterministic filter for duplicate jobs, bad locations, obvious seniority mismatch, and rule-based rejects before LLM use.',
    false,
    'planned',
    NULL
  ),
  (
    'job_message_intake',
    'service',
    'intake',
    'Normalize incoming jobs and messages from email, browser extraction, LinkedIn, and Handshake into database records.',
    false,
    'planned',
    NULL
  ),
  (
    'research_tool_router',
    'router',
    'research',
    'Route research requests from Fit Checker to company cache, search/fetch tools, company site fetch, or browser task.',
    false,
    'planned',
    'Not an agent. Fit Checker decides if research is needed; this router only chooses the source/tool.'
  ),
  (
    'search_fetch_tool',
    'tool',
    'research',
    'Fetch public web/search results when research_tool_router requests it.',
    false,
    'planned',
    NULL
  ),
  (
    'company_site_fetch_tool',
    'tool',
    'research',
    'Fetch and summarize company website pages when research_tool_router requests it.',
    false,
    'planned',
    NULL
  ),
  (
    'parser_ocr',
    'service',
    'profile',
    'Parse PDF, DOCX, text, markdown, and later OCR image/scanned PDF files.',
    false,
    'prototype',
    'pypdf and python-docx ingestion work. OCR not implemented yet.'
  ),
  (
    'chunker',
    'service',
    'profile',
    'Split parsed profile text into heading/paragraph-aware chunks.',
    false,
    'prototype',
    'Improved chunker works and produced 39 chunks from real files.'
  ),
  (
    'brief_generator',
    'service',
    'profile',
    'Generate or refresh profile briefs from approved profile facts.',
    false,
    'partial',
    'Mock/profile_briefs exist, real refresh logic not complete.'
  ),
  (
    'profile_pack_builder',
    'service',
    'profile',
    'Build small job-specific profile context packs from approved facts, briefs, relevant chunks, JD requirements, and company context.',
    false,
    'partial',
    'Mock context pack exists. Real job-specific pack builder not complete.'
  ),
  (
    'browser_queue_worker',
    'service',
    'browser',
    'Claim browser_tasks, hold lease, and execute one browser task at a time.',
    false,
    'prototype',
    'fake_worker.py tested. Real browser worker not complete.'
  ),
  (
    'browser_controller',
    'runtime',
    'browser',
    'Execute browser tasks safely through OpenClaw with lock, timeout, domain policy, and evidence capture.',
    false,
    'planned',
    'Should call OpenClaw native gateway at 127.0.0.1:18789.'
  ),
  (
    'browser_watchdog',
    'db_worker',
    'browser',
    'Requeue expired browser leases and move exhausted tasks to dead_letter_tasks.',
    false,
    'prototype',
    'Watchdog retry and dead-letter behavior tested.'
  ),
  (
    'openclaw_gateway',
    'runtime',
    'browser',
    'Native OpenClaw gateway controlling browser tooling.',
    false,
    'active',
    'Runs outside Docker under user openclaw on 127.0.0.1:18789.'
  ),
  (
    'smtp_sender',
    'tool',
    'messaging',
    'Send approved email replies through Gmail or SMTP.',
    false,
    'planned',
    NULL
  ),
  (
    'browser_send_task',
    'tool',
    'messaging',
    'Create browser task for sending LinkedIn or Handshake messages after approval.',
    false,
    'planned',
    NULL
  )
ON CONFLICT (name)
DO UPDATE SET
  component_type = EXCLUDED.component_type,
  layer = EXCLUDED.layer,
  purpose = EXCLUDED.purpose,
  trainable = EXCLUDED.trainable,
  status = EXCLUDED.status,
  notes = EXCLUDED.notes,
  updated_at = now();
