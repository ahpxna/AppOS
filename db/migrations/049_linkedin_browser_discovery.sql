-- 049 -- Bounded LinkedIn browser discovery
--
-- Discovery is activated only by a user-created browser task. The browser
-- worker enforces the result cap and does not have any submit/action path.

BEGIN;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('linkedin_browser_discovery', 'service', 'L0',
   'Use a manually linked, isolated LinkedIn browser profile to run bounded user-requested searches and intake evidence-bearing job descriptions.',
   false, 'active',
   'No password/cookie import, authentication, CAPTCHA solving, alerts, saved jobs, messaging, or applications. A task cap limits detail pages; valid JDs enter applications and downstream review gates.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
