-- 058 -- Approval capabilities may write only the exact reviewed action set.
BEGIN;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS bound_autofill_action_scope jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE browser_tasks
  ADD COLUMN IF NOT EXISTS autofill_action_scope jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE approval_requests
  ADD CONSTRAINT chk_autofill_action_scope
  CHECK (type <> 'autofill_form' OR bound_autofill_action_scope ? 'profile_keys') NOT VALID;

-- Discovery adapters are proven read paths, not proven browser-form write
-- paths. Explicitly promote one ATS only after a local browser pilot.
UPDATE ats_capabilities
   SET supports_static_text = false,
       supports_radio = false,
       supports_select = false,
       supports_upload = false,
       autofill_mode = 'review_only',
       notes = coalesce(notes || ' ', '') || 'Browser writes disabled by default until a local ATS pilot explicitly promotes this capability.',
       updated_at = now()
 WHERE ats_type IN ('greenhouse', 'lever', 'ashby');

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('autofill_exact_action_scope', 'safety', 'L7',
   'Binds an autofill approval to the reviewed profile keys, legal semantic classes, remembered questions, and document types.',
   false, 'active',
   'Fields revealed after approval are paused unless they were part of the approved action scope.', now(), now())
ON CONFLICT (name) DO UPDATE SET notes = EXCLUDED.notes, updated_at = now();

COMMIT;
