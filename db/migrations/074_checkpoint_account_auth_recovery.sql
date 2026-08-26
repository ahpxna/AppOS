BEGIN;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES (
  'needs_human_checkpoint', 'needs_account_auth', false,
  'Checkpoint cleared and the employer now requires account authentication.'
)
ON CONFLICT (from_step, to_step) DO UPDATE
SET automated=EXCLUDED.automated, note=EXCLUDED.note;

COMMIT;
