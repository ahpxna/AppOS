-- 088 -- Versioned, DB-enforced application state transitions.
BEGIN;

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS pipeline_version bigint NOT NULL DEFAULT 0;

ALTER TABLE pipeline_events
  ADD COLUMN IF NOT EXISTS sequence_no bigint,
  ADD COLUMN IF NOT EXISTS pipeline_version bigint,
  ADD COLUMN IF NOT EXISTS transition_kind text,
  ADD COLUMN IF NOT EXISTS idempotency_key text,
  ADD COLUMN IF NOT EXISTS workflow_run_id uuid REFERENCES workflow_runs(id) ON DELETE SET NULL;

WITH numbered AS (
  SELECT id,
         row_number() OVER (PARTITION BY application_id ORDER BY created_at, id) AS seq,
         sum(CASE
               WHEN from_step IS NOT NULL AND to_step IS DISTINCT FROM from_step THEN 1
               ELSE 0
             END) OVER (
               PARTITION BY application_id ORDER BY created_at, id
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS state_version
    FROM pipeline_events
)
UPDATE pipeline_events pe
   SET sequence_no = numbered.seq,
       pipeline_version = numbered.state_version
  FROM numbered
 WHERE pe.id = numbered.id
   AND (pe.sequence_no IS NULL OR pe.pipeline_version IS NULL);

UPDATE pipeline_events pe
   SET transition_kind = coalesce(pt.transition_kind,
                                  CASE WHEN pt.automated THEN 'automated' ELSE 'human' END,
                                  'recovery')
  FROM pipeline_transitions pt
 WHERE pe.from_step=pt.from_step AND pe.to_step=pt.to_step
   AND pe.transition_kind IS NULL;
UPDATE pipeline_events SET transition_kind='recovery' WHERE transition_kind IS NULL;

ALTER TABLE pipeline_events
  ALTER COLUMN sequence_no SET NOT NULL,
  ALTER COLUMN pipeline_version SET NOT NULL,
  ALTER COLUMN transition_kind SET NOT NULL;
ALTER TABLE pipeline_events DROP CONSTRAINT IF EXISTS pipeline_events_transition_kind_check;
ALTER TABLE pipeline_events ADD CONSTRAINT pipeline_events_transition_kind_check
  CHECK (transition_kind IN ('automated','human','privileged','recovery'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_events_application_sequence
  ON pipeline_events(application_id, sequence_no)
  WHERE application_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_events_application_idempotency
  ON pipeline_events(application_id, idempotency_key)
  WHERE application_id IS NOT NULL AND idempotency_key IS NOT NULL;

-- ``pipeline_events`` historically also carries audit-only observations such
-- as NULL->intake provenance and same-step recovery notes. Keep those writers
-- compatible without allowing them to forge a state transition. The trigger
-- allocates an append-only sequence under the application row lock and binds
-- the event to the *current* pipeline version. Any state-changing edge must
-- still go through jobos_transition_application().
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

  IF NEW.sequence_no IS NOT NULL AND NEW.pipeline_version IS NOT NULL
     AND NEW.transition_kind IS NOT NULL THEN
    RETURN NEW;
  END IF;

  IF NEW.from_step IS NOT NULL AND NEW.to_step IS DISTINCT FROM NEW.from_step THEN
    RAISE EXCEPTION 'state-changing pipeline events must use jobos_transition_application()';
  END IF;

  SELECT pipeline_version INTO v_current_version
    FROM applications WHERE id=NEW.application_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Application % not found for pipeline event', NEW.application_id;
  END IF;
  SELECT coalesce(max(sequence_no),0)+1 INTO v_next_sequence
    FROM pipeline_events WHERE application_id=NEW.application_id;

  NEW.sequence_no := coalesce(NEW.sequence_no, v_next_sequence);
  NEW.pipeline_version := coalesce(NEW.pipeline_version, v_current_version);
  NEW.transition_kind := coalesce(NEW.transition_kind, 'recovery');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobos_prepare_pipeline_event ON pipeline_events;
CREATE TRIGGER trg_jobos_prepare_pipeline_event
BEFORE INSERT ON pipeline_events
FOR EACH ROW EXECUTE FUNCTION jobos_prepare_pipeline_event();

UPDATE applications a
   SET pipeline_version = greatest(a.pipeline_version, coalesce(src.max_version,0))
  FROM (
    SELECT application_id, max(pipeline_version) AS max_version
      FROM pipeline_events WHERE application_id IS NOT NULL GROUP BY application_id
  ) src
 WHERE a.id=src.application_id;

CREATE OR REPLACE FUNCTION jobos_guard_application_step_update()
RETURNS trigger AS $$
BEGIN
  IF NEW.current_step IS DISTINCT FROM OLD.current_step
     AND coalesce(current_setting('jobos.pipeline_transition_authorized', true),'') <> 'on' THEN
    RAISE EXCEPTION 'applications.current_step is DB-authoritative; use jobos_transition_application()';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobos_guard_application_step_update ON applications;
CREATE TRIGGER trg_jobos_guard_application_step_update
BEFORE UPDATE OF current_step ON applications
FOR EACH ROW EXECUTE FUNCTION jobos_guard_application_step_update();

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

  -- A committed transition whose client lost the response must be replay-safe.
  -- Resolve idempotency before rejecting the old expected_from/version; require
  -- the existing event to represent the exact same edge.
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
  PERFORM set_config('jobos.pipeline_transition_authorized','off',true);

  INSERT INTO pipeline_events(
    application_id,from_step,to_step,actor,reason,detail_json,sequence_no,pipeline_version,
    transition_kind,idempotency_key,workflow_run_id
  ) VALUES (
    p_application_id,p_expected_from,p_to,p_actor,p_reason,coalesce(p_detail,'{}'::jsonb),
    v_sequence_no,v_new_version,v_kind,p_idempotency_key,p_workflow_run_id
  );
  RETURN v_new_version;
EXCEPTION WHEN OTHERS THEN
  PERFORM set_config('jobos.pipeline_transition_authorized','off',true);
  RAISE;
END;
$$ LANGUAGE plpgsql;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('pipeline_db_authority','safety','L1',
   'Enforce versioned state mutation and its immutable audit event inside PostgreSQL.',
   false,'active','Direct UPDATE of applications.current_step is rejected after migration 088.',now(),now())
ON CONFLICT (name) DO UPDATE SET purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
