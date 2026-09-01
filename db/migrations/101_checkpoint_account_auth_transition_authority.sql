-- 101 -- Align checkpoint -> account-auth recovery with runtime authority.
--
-- Migration 074 added the legal edge before transition_kind existed. Migration
-- 084 later classified non-automated edges, but omitted this one from its
-- recovery set, leaving it as `human`. The browser state watcher correctly
-- observes a common flow where a completed checkpoint redirects back to Login;
-- that observation is recovery, not a new human decision or browser action.
BEGIN;

UPDATE pipeline_transitions
   SET automated=false,
       transition_kind='recovery',
       note='Checkpoint cleared and the employer now requires account authentication.'
 WHERE from_step='needs_human_checkpoint'
   AND to_step='needs_account_auth';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pipeline_transitions
     WHERE from_step='needs_human_checkpoint'
       AND to_step='needs_account_auth'
       AND transition_kind='recovery'
  ) THEN
    RAISE EXCEPTION 'checkpoint -> account-auth recovery edge is missing or misclassified';
  END IF;
END;
$$;

COMMIT;
