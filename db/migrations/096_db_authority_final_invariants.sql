-- 096 -- Final cross-table invariants after legacy backfill/materialization.
BEGIN;

-- ---------------------------------------------------------------------------
-- Autofill approval/task -> exact immutable plan binding.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION jobos_validate_approval_autofill_plan()
RETURNS trigger AS $$
DECLARE
  v_application_id uuid;
  v_plan_key text;
  v_pipeline_version bigint;
BEGIN
  IF NEW.bound_autofill_plan_id IS NOT NULL THEN
    SELECT application_id,plan_key,pipeline_version
      INTO v_application_id,v_plan_key,v_pipeline_version
      FROM autofill_plans WHERE id=NEW.bound_autofill_plan_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'bound autofill plan does not exist';
    END IF;
    IF NEW.application_id IS DISTINCT FROM v_application_id THEN
      RAISE EXCEPTION 'approval application does not match bound autofill plan';
    END IF;
    IF NEW.bound_autofill_plan_key IS DISTINCT FROM v_plan_key THEN
      RAISE EXCEPTION 'approval plan_key does not match bound autofill plan';
    END IF;
    IF NEW.bound_pipeline_version IS DISTINCT FROM v_pipeline_version THEN
      RAISE EXCEPTION 'approval pipeline_version does not match bound autofill plan';
    END IF;
  END IF;
  IF NEW.type='autofill_form' AND NEW.status IN ('pending','approved','executing')
     AND NEW.bound_autofill_plan_id IS NULL THEN
    RAISE EXCEPTION 'active autofill approval lacks a first-class autofill plan';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_approval_autofill_plan ON approval_requests;
CREATE TRIGGER trg_jobos_validate_approval_autofill_plan
BEFORE INSERT OR UPDATE OF bound_autofill_plan_id,bound_autofill_plan_key,bound_pipeline_version,
  application_id,status,type
ON approval_requests FOR EACH ROW EXECUTE FUNCTION jobos_validate_approval_autofill_plan();

CREATE OR REPLACE FUNCTION jobos_validate_browser_task_autofill_plan()
RETURNS trigger AS $$
DECLARE
  v_plan_application_id uuid;
  v_approval_plan_id uuid;
BEGIN
  IF NEW.autofill_plan_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT application_id INTO v_plan_application_id
    FROM autofill_plans WHERE id=NEW.autofill_plan_id;
  IF NOT FOUND OR NEW.application_id IS DISTINCT FROM v_plan_application_id THEN
    RAISE EXCEPTION 'browser task application does not match autofill plan';
  END IF;
  IF NEW.approval_request_id IS NULL THEN
    RAISE EXCEPTION 'browser task with autofill plan lacks approval_request_id';
  END IF;
  SELECT bound_autofill_plan_id INTO v_approval_plan_id
    FROM approval_requests WHERE id=NEW.approval_request_id;
  IF NOT FOUND OR v_approval_plan_id IS DISTINCT FROM NEW.autofill_plan_id THEN
    RAISE EXCEPTION 'browser task autofill plan does not match approval';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_browser_task_autofill_plan ON browser_tasks;
CREATE TRIGGER trg_jobos_validate_browser_task_autofill_plan
BEFORE INSERT OR UPDATE OF autofill_plan_id,approval_request_id,application_id
ON browser_tasks FOR EACH ROW EXECUTE FUNCTION jobos_validate_browser_task_autofill_plan();

-- Plan identity/content is immutable. Lifecycle fields remain mutable.
CREATE OR REPLACE FUNCTION jobos_guard_autofill_plan_identity()
RETURNS trigger AS $$
BEGIN
  IF NEW.application_id IS DISTINCT FROM OLD.application_id
     OR NEW.plan_key IS DISTINCT FROM OLD.plan_key
     OR NEW.pipeline_version IS DISTINCT FROM OLD.pipeline_version
     OR NEW.page_snapshot_id IS DISTINCT FROM OLD.page_snapshot_id
     OR NEW.target_id IS DISTINCT FROM OLD.target_id
     OR NEW.page_url IS DISTINCT FROM OLD.page_url
     OR NEW.origin IS DISTINCT FROM OLD.origin
     OR NEW.page_fingerprint IS DISTINCT FROM OLD.page_fingerprint
     OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
     OR NEW.action_scope_sha256 IS DISTINCT FROM OLD.action_scope_sha256
     OR NEW.action_scope_json IS DISTINCT FROM OLD.action_scope_json
     OR NEW.generated_document_id IS DISTINCT FROM OLD.generated_document_id
     OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
     OR NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256 THEN
    RAISE EXCEPTION 'autofill plan identity/content is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_guard_autofill_plan_identity ON autofill_plans;
CREATE TRIGGER trg_jobos_guard_autofill_plan_identity
BEFORE UPDATE ON autofill_plans
FOR EACH ROW EXECUTE FUNCTION jobos_guard_autofill_plan_identity();

CREATE OR REPLACE FUNCTION jobos_guard_autofill_plan_action_identity()
RETURNS trigger AS $$
BEGIN
  IF NEW.autofill_plan_id IS DISTINCT FROM OLD.autofill_plan_id
     OR NEW.sequence_no IS DISTINCT FROM OLD.sequence_no
     OR NEW.action_kind IS DISTINCT FROM OLD.action_kind
     OR NEW.field_ref IS DISTINCT FROM OLD.field_ref
     OR NEW.field_registry_key IS DISTINCT FROM OLD.field_registry_key
     OR NEW.value_sha256 IS DISTINCT FROM OLD.value_sha256
     OR NEW.document_artifact_id IS DISTINCT FROM OLD.document_artifact_id
     OR NEW.action_json IS DISTINCT FROM OLD.action_json THEN
    RAISE EXCEPTION 'autofill plan action is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_guard_autofill_plan_action_identity ON autofill_plan_actions;
CREATE TRIGGER trg_jobos_guard_autofill_plan_action_identity
BEFORE UPDATE ON autofill_plan_actions
FOR EACH ROW EXECUTE FUNCTION jobos_guard_autofill_plan_action_identity();

-- Project approval/browser lifecycle into the first-class plan row. The
-- immutable content remains untouched; needs_reconciliation always wins.
CREATE OR REPLACE FUNCTION jobos_project_autofill_plan_from_approval()
RETURNS trigger AS $$
DECLARE
  v_status text;
BEGIN
  IF NEW.type <> 'autofill_form' OR NEW.bound_autofill_plan_id IS NULL THEN
    RETURN NEW;
  END IF;
  v_status := CASE NEW.status
    WHEN 'pending' THEN 'awaiting_approval'
    WHEN 'approved' THEN 'approved'
    WHEN 'executing' THEN 'executing'
    WHEN 'consumed' THEN 'completed'
    WHEN 'denied' THEN 'cancelled'
    WHEN 'expired' THEN 'cancelled'
    ELSE NULL
  END;
  IF v_status IS NOT NULL THEN
    UPDATE autofill_plans
       SET status=v_status,
           approved_at=CASE WHEN v_status='approved' THEN coalesce(approved_at,now()) ELSE approved_at END,
           finished_at=CASE WHEN v_status IN ('completed','cancelled') THEN coalesce(finished_at,now()) ELSE finished_at END
     WHERE id=NEW.bound_autofill_plan_id AND status <> 'needs_reconciliation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_project_autofill_plan_from_approval ON approval_requests;
CREATE TRIGGER trg_jobos_project_autofill_plan_from_approval
AFTER INSERT OR UPDATE OF status,bound_autofill_plan_id
ON approval_requests FOR EACH ROW EXECUTE FUNCTION jobos_project_autofill_plan_from_approval();

CREATE OR REPLACE FUNCTION jobos_project_autofill_plan_from_task()
RETURNS trigger AS $$
BEGIN
  IF NEW.autofill_plan_id IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.execution_state='needs_reconciliation' THEN
    UPDATE autofill_plans SET status='needs_reconciliation',finished_at=coalesce(finished_at,now())
     WHERE id=NEW.autofill_plan_id;
  ELSIF NEW.execution_state='completed' THEN
    UPDATE autofill_plans SET status='completed',finished_at=coalesce(finished_at,now())
     WHERE id=NEW.autofill_plan_id AND status <> 'needs_reconciliation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_project_autofill_plan_from_task ON browser_tasks;
CREATE TRIGGER trg_jobos_project_autofill_plan_from_task
AFTER INSERT OR UPDATE OF execution_state,autofill_plan_id
ON browser_tasks FOR EACH ROW EXECUTE FUNCTION jobos_project_autofill_plan_from_task();

-- ---------------------------------------------------------------------------
-- Human decision/callback must name a revision belonging to the same item.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION jobos_validate_review_revision_binding()
RETURNS trigger AS $$
DECLARE
  v_item_id uuid;
BEGIN
  SELECT review_item_id INTO v_item_id
    FROM human_review_item_revisions WHERE id=NEW.review_revision_id;
  IF NOT FOUND OR v_item_id IS DISTINCT FROM NEW.review_item_id THEN
    RAISE EXCEPTION 'review decision revision does not belong to review item';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_review_revision_binding ON human_review_decisions;
CREATE TRIGGER trg_jobos_validate_review_revision_binding
BEFORE INSERT OR UPDATE OF review_item_id,review_revision_id
ON human_review_decisions FOR EACH ROW EXECUTE FUNCTION jobos_validate_review_revision_binding();

CREATE OR REPLACE FUNCTION jobos_validate_callback_revision_binding()
RETURNS trigger AS $$
DECLARE
  v_item_id uuid;
BEGIN
  IF NEW.review_revision_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT review_item_id INTO v_item_id
    FROM human_review_item_revisions WHERE id=NEW.review_revision_id;
  IF NOT FOUND OR v_item_id IS DISTINCT FROM NEW.review_item_id THEN
    RAISE EXCEPTION 'Telegram callback revision does not belong to review item';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_callback_revision_binding ON telegram_callback_tokens;
CREATE TRIGGER trg_jobos_validate_callback_revision_binding
BEFORE INSERT OR UPDATE OF review_item_id,review_revision_id
ON telegram_callback_tokens FOR EACH ROW EXECUTE FUNCTION jobos_validate_callback_revision_binding();

-- ---------------------------------------------------------------------------
-- Render and LLM lineage ownership.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION jobos_validate_render_claim()
RETURNS trigger AS $$
BEGIN
  IF NEW.status='running' AND
     (NEW.claimed_by IS NULL OR btrim(NEW.claimed_by)='' OR NEW.claim_token IS NULL
      OR NEW.lease_expires_at IS NULL) THEN
    RAISE EXCEPTION 'running render must have owner token and lease';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_render_claim ON document_render_runs;
CREATE TRIGGER trg_jobos_validate_render_claim
BEFORE INSERT OR UPDATE OF status,claimed_by,claim_token,lease_expires_at
ON document_render_runs FOR EACH ROW EXECUTE FUNCTION jobos_validate_render_claim();

CREATE OR REPLACE FUNCTION jobos_validate_llm_workflow_lineage()
RETURNS trigger AS $$
DECLARE
  v_application_id uuid;
BEGIN
  IF NEW.workflow_step_run_id IS NULL OR NEW.application_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT wr.application_id INTO v_application_id
    FROM workflow_step_runs wsr
    JOIN workflow_runs wr ON wr.id=wsr.workflow_run_id
   WHERE wsr.id=NEW.workflow_step_run_id;
  IF NOT FOUND OR v_application_id IS DISTINCT FROM NEW.application_id THEN
    RAISE EXCEPTION 'LLM call workflow step does not belong to application';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_jobos_validate_llm_workflow_lineage ON llm_calls;
CREATE TRIGGER trg_jobos_validate_llm_workflow_lineage
BEFORE INSERT OR UPDATE OF workflow_step_run_id,application_id
ON llm_calls FOR EACH ROW EXECUTE FUNCTION jobos_validate_llm_workflow_lineage();

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('db_authority_final_invariants','safety','L1',
   'Enforce cross-table plan, review-revision, render-lease and workflow-lineage contracts after legacy backfill.',
   false,'active',
   'Autofill plan content is immutable; active approvals/tasks must reference the exact same plan/application/version.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
