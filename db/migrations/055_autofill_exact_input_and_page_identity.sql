-- 055 -- exact page and input binding for browser form capabilities
BEGIN;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS expected_initial_url text,
  ADD COLUMN IF NOT EXISTS expected_page_fingerprint text,
  ADD COLUMN IF NOT EXISTS bound_autofill_input_hash text;

ALTER TABLE browser_tasks
  ADD COLUMN IF NOT EXISTS expected_initial_url text,
  ADD COLUMN IF NOT EXISTS expected_page_fingerprint text,
  ADD COLUMN IF NOT EXISTS autofill_input_hash text;

ALTER TABLE approval_requests
  ADD CONSTRAINT chk_autofill_page_binding
  CHECK (type <> 'autofill_form' OR (expected_initial_url IS NOT NULL AND expected_page_fingerprint IS NOT NULL AND bound_autofill_input_hash IS NOT NULL)) NOT VALID;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('autofill_exact_page_binding', 'safety', 'L7',
   'Binds each approved autofill session to a canonical initial page URL, snapshot fingerprint, and exact input hash.',
   false, 'active',
   'Same-origin pages cannot reuse a capability issued for another application page or changed candidate profile.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
