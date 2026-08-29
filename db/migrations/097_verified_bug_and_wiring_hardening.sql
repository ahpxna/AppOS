-- 097_verified_bug_and_wiring_hardening.sql
-- Close verified authority/identity races found by the post-096 runtime audit.

BEGIN;

-- ---------------------------------------------------------------------------
-- Multi-provider model pricing is keyed by provider + model, not model alone.
-- ---------------------------------------------------------------------------
UPDATE model_pricing SET provider=lower(btrim(provider));
ALTER TABLE model_pricing DROP CONSTRAINT IF EXISTS model_pricing_pkey;
ALTER TABLE model_pricing ADD CONSTRAINT model_pricing_pkey PRIMARY KEY (provider, model_name);
CREATE INDEX IF NOT EXISTS idx_model_pricing_model_name ON model_pricing(model_name);

-- ---------------------------------------------------------------------------
-- A vector is meaningful only inside the provider/model vector space that
-- created it.  Legacy rows are deliberately quarantined instead of being
-- silently mixed with the currently configured provider.
-- ---------------------------------------------------------------------------
ALTER TABLE profile_chunk_embeddings
  ADD COLUMN IF NOT EXISTS embedding_provider text,
  ADD COLUMN IF NOT EXISTS resolved_embedding_model text;

UPDATE profile_chunk_embeddings
   SET embedding_provider = coalesce(nullif(embedding_provider,''), 'legacy_unknown'),
       resolved_embedding_model = coalesce(nullif(resolved_embedding_model,''), embedding_model);

ALTER TABLE profile_chunk_embeddings
  ALTER COLUMN embedding_provider SET NOT NULL,
  ALTER COLUMN resolved_embedding_model SET NOT NULL;

DO $$
DECLARE
  v_constraint_name text;
BEGIN
  SELECT c.conname INTO v_constraint_name
    FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
   WHERE n.nspname='public' AND t.relname='profile_chunk_embeddings' AND c.contype='u'
     AND (
       SELECT array_agg(a.attname::text ORDER BY u.ordinality)
         FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ordinality)
         JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=u.attnum
     ) = ARRAY['chunk_id','embedding_model','content_hash']::text[]
   LIMIT 1;
  IF v_constraint_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE profile_chunk_embeddings DROP CONSTRAINT %I', v_constraint_name);
  END IF;
END;
$$;
DROP INDEX IF EXISTS uq_profile_chunk_embeddings_vector_space;
CREATE UNIQUE INDEX uq_profile_chunk_embeddings_vector_space
  ON profile_chunk_embeddings(
       chunk_id, embedding_provider, embedding_model, resolved_embedding_model, content_hash
     );
CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_space
  ON profile_chunk_embeddings(embedding_provider, embedding_model, resolved_embedding_model, status);

ALTER TABLE profile_retrieval_queries
  ADD COLUMN IF NOT EXISTS embedding_provider text,
  ADD COLUMN IF NOT EXISTS resolved_embedding_model text;

-- Keep operator/status views correct when more than one vector space exists for
-- a chunk. Preserve every historical column and append identity fields only.
CREATE OR REPLACE VIEW v_profile_vector_index_status AS
SELECT
  rf.file_name,
  rf.file_role,
  count(DISTINCT pc.id) AS total_chunks,
  count(DISTINCT pc.id) FILTER (WHERE e.status = 'completed') AS embedded_chunks,
  count(DISTINCT pc.id) - count(DISTINCT pc.id) FILTER (WHERE e.status = 'completed') AS missing_embeddings
FROM raw_files rf
JOIN profile_chunks pc ON pc.file_id = rf.id
LEFT JOIN profile_chunk_embeddings e ON e.chunk_id = pc.id AND e.status = 'completed'
WHERE rf.source = 'local_profile_ingestion'
GROUP BY rf.file_name, rf.file_role
ORDER BY missing_embeddings DESC, rf.file_name;

CREATE OR REPLACE VIEW v_profile_retrievable_chunks AS
SELECT
  pc.id AS chunk_id,
  rf.id AS file_id,
  rf.file_name,
  rf.file_role,
  rf.evidence_weight,
  rf.allow_profile_fact_promotion,
  pc.chunk_index,
  pc.section,
  pc.category,
  pc.text_content,
  e.embedding_model,
  e.embedding_dim,
  e.embedding,
  e.created_at AS embedded_at,
  e.embedding_provider,
  e.resolved_embedding_model
FROM profile_chunks pc
JOIN raw_files rf ON rf.id = pc.file_id
JOIN profile_chunk_embeddings e ON e.chunk_id = pc.id
WHERE rf.source = 'local_profile_ingestion'
  AND rf.is_active = true
  AND rf.path_status = 'verified'
  AND e.status = 'completed';

CREATE OR REPLACE VIEW v_profile_retrieval_latest_results AS
SELECT
  q.id AS retrieval_query_id,
  left(q.id::text, 8) AS retrieval_short_id,
  q.purpose,
  q.role_family,
  q.query_text,
  q.embedding_model,
  q.max_chunks,
  q.status AS query_status,
  r.rank,
  r.similarity,
  r.distance,
  r.rerank_score,
  r.retrieval_bucket,
  r.retrieval_signal_score,
  r.negative_retrieval_flags,
  left(r.chunk_id::text, 8) AS chunk_short_id,
  r.file_name,
  r.file_role,
  r.chunk_index,
  r.section,
  r.category,
  r.text_preview,
  q.created_at,
  q.embedding_provider,
  q.resolved_embedding_model
FROM profile_retrieval_queries q
JOIN profile_retrieval_results r ON r.retrieval_query_id = q.id
ORDER BY q.created_at DESC, r.rank ASC;

-- ---------------------------------------------------------------------------
-- ForceReply prompts now use the durable Telegram outbox. Extend the historical
-- delivery-kind domain before those new intents can be inserted.
-- ---------------------------------------------------------------------------
ALTER TABLE telegram_review_deliveries
  DROP CONSTRAINT IF EXISTS telegram_review_deliveries_delivery_kind_check;
ALTER TABLE telegram_review_deliveries
  ADD CONSTRAINT telegram_review_deliveries_delivery_kind_check
  CHECK (delivery_kind IN (
    'summary','artifact','decision_update','document_feedback_prompt','question_reply_prompt'
  ));

-- ---------------------------------------------------------------------------
-- Version lanes are first-class identities.  Writers also take advisory locks,
-- but the DB constraint is the final concurrent-writer safety net.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_documents_application_type_version
  ON generated_documents(application_id, doc_type, version)
  WHERE application_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_drafted_replies_thread_version
  ON drafted_replies(thread_id, version);

-- Expired fit-review capabilities must not strand a human-gated application.
INSERT INTO pipeline_transitions(from_step,to_step,automated,note,transition_kind)
VALUES ('awaiting_fit_review','screened',false,
        'Fit-review capability expired; return to screened so a fresh review can be issued.',
        'recovery')
ON CONFLICT (from_step,to_step) DO UPDATE
SET automated=EXCLUDED.automated, note=EXCLUDED.note, transition_kind=EXCLUDED.transition_kind;

-- ---------------------------------------------------------------------------
-- pipeline_events: pre-populating sequence/version/kind must not bypass the
-- state-changing-event guard.  Canonical transitions keep the authorization
-- GUC enabled through both the applications UPDATE and its matching event.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION jobos_prepare_pipeline_event()
RETURNS trigger AS $$
DECLARE
  v_current_version bigint;
  v_next_sequence bigint;
BEGIN
  IF NEW.application_id IS NULL THEN
    NEW.sequence_no := coalesce(NEW.sequence_no, 1);
    NEW.pipeline_version := coalesce(NEW.pipeline_version, 0);
    NEW.transition_kind := coalesce(NEW.transition_kind, 'recovery');
    RETURN NEW;
  END IF;

  IF NEW.from_step IS NOT NULL AND NEW.to_step IS DISTINCT FROM NEW.from_step
     AND current_setting('jobos.pipeline_transition_authorized', true) IS DISTINCT FROM 'on' THEN
    RAISE EXCEPTION 'state-changing pipeline events must use jobos_transition_application()';
  END IF;

  SELECT pipeline_version INTO v_current_version
    FROM applications WHERE id=NEW.application_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Application % not found for pipeline event', NEW.application_id;
  END IF;

  SELECT coalesce(max(sequence_no),0)+1 INTO v_next_sequence
    FROM pipeline_events WHERE application_id=NEW.application_id;

  IF NEW.sequence_no IS NOT NULL AND NEW.sequence_no <> v_next_sequence THEN
    RAISE EXCEPTION 'pipeline event sequence must be the next append-only sequence';
  END IF;
  IF NEW.pipeline_version IS NOT NULL AND NEW.pipeline_version <> v_current_version THEN
    RAISE EXCEPTION 'pipeline event version does not match current application version';
  END IF;

  NEW.sequence_no := coalesce(NEW.sequence_no, v_next_sequence);
  NEW.pipeline_version := coalesce(NEW.pipeline_version, v_current_version);
  NEW.transition_kind := coalesce(NEW.transition_kind, 'recovery');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION jobos_transition_application(
  p_application_id uuid,
  p_expected_version bigint,
  p_expected_from text,
  p_to text,
  p_actor text,
  p_reason text,
  p_detail jsonb DEFAULT '{}'::jsonb,
  p_status text DEFAULT NULL,
  p_required_kind text DEFAULT NULL,
  p_lease_run_id uuid DEFAULT NULL,
  p_expected_job_url text DEFAULT NULL,
  p_expected_jd_hash text DEFAULT NULL,
  p_idempotency_key text DEFAULT NULL,
  p_workflow_run_id uuid DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
  v_step text;
  v_version bigint;
  v_kind text;
  v_job_url text;
  v_jd_hash text;
  v_processing_run_id uuid;
  v_processing_step text;
  v_lease_expires_at timestamptz;
  v_new_version bigint;
  v_existing_version bigint;
  v_existing_from text;
  v_existing_to text;
  v_sequence_no bigint;
BEGIN
  SELECT current_step,pipeline_version,coalesce(job_url,''),coalesce(jd_hash,''),
         processing_run_id,processing_step,processing_lease_expires_at
    INTO v_step,v_version,v_job_url,v_jd_hash,v_processing_run_id,v_processing_step,v_lease_expires_at
    FROM applications WHERE id=p_application_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Application % not found', p_application_id; END IF;

  IF p_idempotency_key IS NOT NULL THEN
    SELECT pipeline_version,from_step,to_step
      INTO v_existing_version,v_existing_from,v_existing_to
      FROM pipeline_events
     WHERE application_id=p_application_id AND idempotency_key=p_idempotency_key;
    IF FOUND THEN
      IF v_existing_from IS DISTINCT FROM p_expected_from OR v_existing_to IS DISTINCT FROM p_to THEN
        RAISE EXCEPTION 'Pipeline idempotency key reused for a different transition';
      END IF;
      RETURN v_existing_version;
    END IF;
  END IF;

  IF v_step IS DISTINCT FROM p_expected_from OR v_version <> p_expected_version THEN
    RAISE EXCEPTION 'Application state/version changed during % -> % (step %, version %)',
      p_expected_from,p_to,v_step,v_version;
  END IF;

  SELECT coalesce(transition_kind, CASE WHEN automated THEN 'automated' ELSE 'human' END)
    INTO v_kind FROM pipeline_transitions WHERE from_step=p_expected_from AND to_step=p_to;
  IF NOT FOUND THEN RAISE EXCEPTION 'Illegal transition % -> %', p_expected_from,p_to; END IF;
  IF p_required_kind IS NOT NULL AND v_kind <> p_required_kind THEN
    RAISE EXCEPTION 'Transition % -> % is %, not %',p_expected_from,p_to,v_kind,p_required_kind;
  END IF;
  IF p_lease_run_id IS NOT NULL AND
     (v_processing_run_id IS DISTINCT FROM p_lease_run_id OR v_processing_step IS DISTINCT FROM p_expected_from
      OR v_lease_expires_at IS NULL OR v_lease_expires_at <= now()) THEN
    RAISE EXCEPTION 'Application processing lease changed/expired';
  END IF;
  IF p_expected_job_url IS NOT NULL AND v_job_url <> p_expected_job_url THEN
    RAISE EXCEPTION 'Application job URL changed';
  END IF;
  IF p_expected_jd_hash IS NOT NULL AND v_jd_hash <> p_expected_jd_hash THEN
    RAISE EXCEPTION 'Application JD hash changed';
  END IF;

  v_new_version := v_version + 1;
  SELECT coalesce(max(sequence_no),0)+1 INTO v_sequence_no
    FROM pipeline_events WHERE application_id=p_application_id;
  PERFORM set_config('jobos.pipeline_transition_authorized','on',true);
  UPDATE applications
     SET current_step=p_to,
         status=coalesce(p_status,status),
         pipeline_version=v_new_version,
         updated_at=now()
   WHERE id=p_application_id;

  INSERT INTO pipeline_events(
    application_id,from_step,to_step,actor,reason,detail_json,sequence_no,pipeline_version,
    transition_kind,idempotency_key,workflow_run_id
  ) VALUES (
    p_application_id,p_expected_from,p_to,p_actor,p_reason,coalesce(p_detail,'{}'::jsonb),
    v_sequence_no,v_new_version,v_kind,p_idempotency_key,p_workflow_run_id
  );
  PERFORM set_config('jobos.pipeline_transition_authorized','off',true);
  RETURN v_new_version;
EXCEPTION WHEN OTHERS THEN
  PERFORM set_config('jobos.pipeline_transition_authorized','off',true);
  RAISE;
END;
$$ LANGUAGE plpgsql;

COMMIT;
