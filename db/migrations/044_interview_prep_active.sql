-- =========================================================
-- 044_interview_prep_active.sql
-- L9 -- mark interview_prep_agent as actually wired
--
-- component_registry.interview_prep_agent was seeded by migration 009
-- with status='planned' (see 009_component_learning_layer.sql). Migration
-- 043 then built the real table (interview_prep_packages) and
-- services/interview-prep/interview_prep_v1.py implements the agent, but
-- no migration ever flipped the registry row -- so the tracking table
-- still said "planned" for a component that has been runnable since 043.
--
-- Mirrors the ON CONFLICT convention introduced in
-- 041_wiring_fixes_and_gates.sql section 7.
-- =========================================================

BEGIN;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('interview_prep_agent', 'agent', 'L9',
   'Generate a grounded interview prep package (talking points, questions to ask, '
   'stories to practice) from the approved profile context pack and cached company '
   'research, once an interview invite is classified.',
   false, 'active',
   'Implemented 2026-07-31 in services/interview-prep/interview_prep_v1.py '
   '(migration 043 added the storage). Not wired into orchestrator_v1.py''s '
   'pipeline_steps state machine -- interviews live outside the applications '
   'pipeline_step column, so this stays a standalone command the user runs '
   '(--list-only to inspect the queue, --apply to generate + write), same '
   'invocation pattern as message_reply_v1.py cmd_draft.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET component_type = EXCLUDED.component_type, layer = EXCLUDED.layer,
    purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
