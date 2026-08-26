BEGIN;

INSERT INTO pipeline_steps (step, layer, description, is_terminal, requires_human, sort_order)
VALUES
  ('autofill_executing', 'L3', 'Exact approved deterministic autofill is actively writing the pinned form.', false, false, 89)
ON CONFLICT (step) DO UPDATE
SET description=EXCLUDED.description, requires_human=EXCLUDED.requires_human, sort_order=EXCLUDED.sort_order;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('awaiting_approval', 'autofill_executing', true, 'Durable CAS fence acquired immediately before the first deterministic browser write.'),
  ('autofill_executing', 'form_filled', true, 'Deterministic autofill completed and all writes were verified.'),
  ('autofill_executing', 'awaiting_approval', false, 'Deterministic autofill ended partial without an uncertain side effect; a fresh plan may be reviewed.'),
  ('autofill_executing', 'application_form_ready', false, 'Human reconciliation closed an uncertain deterministic autofill; inspect and create a fresh approval before any further write.'),
  ('needs_email_verification', 'needs_account_auth', false, 'Employer session regressed to account authentication.'),
  ('needs_mfa', 'needs_account_auth', false, 'Employer session expired/regressed to account authentication.'),
  ('needs_human_checkpoint', 'needs_mfa', false, 'Checkpoint cleared and the employer now requires MFA.'),
  ('needs_human_checkpoint', 'needs_email_verification', false, 'Checkpoint cleared and the employer now requires email verification.'),
  ('needs_mfa', 'needs_email_verification', false, 'MFA flow returned to an email-verification factor.'),
  ('needs_email_verification', 'needs_human_checkpoint', false, 'Email verification flow encountered a human checkpoint.')
ON CONFLICT (from_step, to_step) DO UPDATE SET automated=EXCLUDED.automated, note=EXCLUDED.note;

COMMIT;
