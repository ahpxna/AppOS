-- =========================================================
-- 033 — Register Post-Fit Router
-- Purpose:
--   Deterministic router after JD fit analysis.
-- =========================================================

INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes,
  created_at,
  updated_at
)
VALUES (
  'post_fit_router',
  'service',
  'job_analysis',
  'Route applications after fit analysis: reject/save-only, ask user for review, or advance to company research.',
  false,
  'prototype',
  'Deterministic router. Does not call LLM. Creates application_events and approval_requests where needed.',
  now(),
  now()
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
