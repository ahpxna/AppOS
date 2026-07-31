-- =========================================================
-- 041_wiring_fixes_and_gates.sql
-- Verification-pass fixes + wiring for previously "[~] has code, not
-- auto-wired" gaps. Each section says which report finding it closes.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. v_profile_asset_deepseek_review was hardcoded to one literal
--    audit_version string. Any audit re-run under a new version string
--    made this view (and v_profile_asset_approval_candidates,
--    v_profile_asset_deepseek_audit_summary) silently return 0 rows.
--    Fixed: always take the latest audit row per asset per audit_type,
--    regardless of version string.
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_profile_asset_deepseek_review AS
WITH evidence_counts AS (
  SELECT
    profile_asset_id,
    count(*) AS evidence_item_count
  FROM profile_asset_evidence_items
  GROUP BY profile_asset_id
),
latest_audit AS (
  -- One row per (profile_asset_id, audit_type): the most recent audit,
  -- whatever version string produced it. Replaces the old hardcoded
  -- `audit_version = 'deepseek_structured_asset_audit_v1_2026_04_27'`
  -- filter, which went blind the moment the auditor's version bumped.
  SELECT DISTINCT ON (profile_asset_id, audit_type) *
  FROM profile_asset_audits
  WHERE audit_type = 'deepseek_structured_asset_grounding_overclaim_audit'
  ORDER BY profile_asset_id, audit_type, created_at DESC
)
SELECT
  pa.id AS profile_asset_id,
  left(pa.id::text, 8) AS asset_short_id,

  pa.asset_title,
  pa.asset_type,
  pa.abstraction_level,
  pa.status AS asset_status,
  pa.confidence,

  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.project_tags,

  coalesce(ec.evidence_item_count, 0) AS evidence_item_count,

  paa.grounding_status,
  paa.overclaim_risk,
  paa.information_loss_risk,
  paa.evidence_coverage_score,
  paa.specificity_score,
  paa.job_relevance_score,
  paa.supported_claims,
  paa.unsupported_claims,
  paa.required_edits,
  paa.audit_notes,

  left(pa.canonical_narrative, 900) AS canonical_narrative_preview,
  left(pa.job_oriented_summary, 700) AS job_oriented_summary_preview,
  left(pa.resume_bullet_bank, 700) AS resume_bullet_bank_preview,
  left(pa.interview_story, 700) AS interview_story_preview,
  pa.do_not_overclaim_rules,

  pa.compiler_version,
  pa.created_at AS asset_created_at,
  pa.updated_at AS asset_updated_at,
  paa.created_at AS audited_at,

  CASE
    WHEN paa.grounding_status = 'grounded'
      AND paa.overclaim_risk = 'low'
      AND paa.information_loss_risk = 'low'
      AND coalesce(array_length(paa.required_edits, 1), 0) = 0
      AND coalesce(ec.evidence_item_count, 0) >= 2
    THEN 'ready_for_user_approval'

    WHEN paa.grounding_status = 'grounded'
      AND paa.overclaim_risk IN ('low', 'medium')
      AND paa.information_loss_risk IN ('low', 'medium')
    THEN 'review_before_approval'

    WHEN paa.grounding_status IN ('blocked', 'ungrounded')
      OR paa.overclaim_risk = 'high'
      OR paa.information_loss_risk = 'high'
    THEN 'block_or_rewrite'

    ELSE 'manual_review'
  END AS review_recommendation,

  -- Appended at the very end, not inline near the other audit columns:
  -- CREATE OR REPLACE VIEW only allows new columns to be added at the end
  -- of the column list (Postgres errors if an existing column's position
  -- changes), and this view already shipped in 030 with
  -- review_recommendation as its last column.
  paa.audit_version

FROM profile_assets pa
JOIN latest_audit paa
  ON paa.profile_asset_id = pa.id
LEFT JOIN evidence_counts ec
  ON ec.profile_asset_id = pa.id;

COMMENT ON VIEW v_profile_asset_deepseek_review IS
  'Latest audit per asset, any audit_version. Fixed 2026-07-31: previously '
  'pinned to one hardcoded audit_version literal, which made this view (and '
  'everything built on it) silently return 0 rows after any auditor re-run.';

CREATE OR REPLACE VIEW v_profile_asset_approval_candidates AS
SELECT *
FROM v_profile_asset_deepseek_review
WHERE review_recommendation = 'ready_for_user_approval'
  AND asset_status IN ('draft', 'needs_review', 'pending_review');

CREATE OR REPLACE VIEW v_profile_asset_deepseek_audit_summary AS
SELECT
  asset_status,
  grounding_status,
  overclaim_risk,
  information_loss_risk,
  review_recommendation,
  count(*) AS asset_count
FROM v_profile_asset_deepseek_review
GROUP BY
  asset_status, grounding_status, overclaim_risk,
  information_loss_risk, review_recommendation
ORDER BY
  asset_status, grounding_status, overclaim_risk,
  information_loss_risk, review_recommendation;

-- ---------------------------------------------------------
-- 2. v_documents_pending_qa included qa_status = 'revise' rows. A
--    document that hit --max-rounds in verify_document_truth_v1.py stays
--    at qa_status='revise' forever (by design -- see that script), and
--    this view kept re-offering it to --pending runs indefinitely,
--    burning a verifier call on a row that structurally cannot change.
--    Fixed: only qa_status IS NULL is "pending". 'revise' is a terminal
--    per-row state; the *next* thing to verify is always a fresh child
--    row (revision_of = this row, qa_status IS NULL), which IS null and
--    so is already covered. The NOT EXISTS guard is a second safety net
--    in case any future code path resets a parent back to NULL after it
--    already produced a child.
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_documents_pending_qa AS
SELECT
  gd.id AS generated_document_id,
  gd.application_id,
  gd.doc_type,
  gd.version,
  gd.revision_round,
  gd.generator_version,
  gd.created_at
FROM generated_documents gd
WHERE gd.qa_status IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM generated_documents child
    WHERE child.revision_of = gd.id
  )
ORDER BY gd.created_at;

COMMENT ON VIEW v_documents_pending_qa IS
  'Fixed 2026-07-31: dropped qa_status=''revise'' from this queue. A revise '
  'verdict either already produced a fresh NULL-status child row (which IS '
  'in this view) or hit --max-rounds and is terminal (no child row) -- '
  're-verifying the same revise row forever wasted a verifier call every '
  'orchestrator cycle for no possible outcome change.';

-- ---------------------------------------------------------
-- 3. v_autofill_ready_values let through approved-but-blank values (the
--    identity half filtered the 'FILL_ME' placeholder but not empty or
--    whitespace-only strings; the sensitive_answers half had no filter
--    at all). A blank "approved" value would be typed into a form as an
--    empty string instead of being reported as missing.
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_autofill_ready_values AS
SELECT field_name, field_value, field_group, 'identity' AS source
FROM applicant_identity
WHERE approved = true
  AND field_value <> 'FILL_ME'
  AND btrim(field_value) <> ''
UNION ALL
SELECT field_name, answer, answer_kind, 'sensitive' AS source
FROM sensitive_answers
WHERE approved_by_user = true
  AND btrim(answer) <> '';

COMMENT ON VIEW v_autofill_ready_values IS
  'The only values L7 may put into a form. Unapproved, placeholder, and '
  'blank/whitespace-only rows are structurally unreachable. Fixed '
  '2026-07-31: the sensitive_answers half previously had no blank-value '
  'filter at all.';

-- ---------------------------------------------------------
-- 4. L5 fit-review gate (FGO "60-75 -> ask user"). This branch existed
--    in analyze_job_fit_v1.py (fit_decision = 'ask_user') but nothing
--    ever created an approval_request for it or paused the state
--    machine -- 'ask_user' and 'approve_research' were treated
--    identically and both sailed straight through to fit_analyzed.
--
--    New step: awaiting_fit_review. The orchestrator creates a
--    'fit_review' approval_request and parks the application here; a
--    human approves or denies via approval_service_v1.py; the
--    orchestrator (as the messenger, not the decider -- the actual human
--    consent already happened at token-redemption time, same pattern as
--    the L7 submit gate) then carries out the already-authorized
--    transition to fit_analyzed or fit_rejected.
-- ---------------------------------------------------------

INSERT INTO pipeline_steps (step, layer, description, is_terminal, requires_human, sort_order)
VALUES
  ('awaiting_fit_review', 'L5',
   'Borderline fit score (60-75). Waiting on the user to approve research/doc-gen.',
   false, true, 35)
ON CONFLICT (step) DO UPDATE
SET layer = EXCLUDED.layer,
    description = EXCLUDED.description,
    is_terminal = EXCLUDED.is_terminal,
    requires_human = EXCLUDED.requires_human,
    sort_order = EXCLUDED.sort_order;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('screened',             'awaiting_fit_review', true,  'L5 said borderline (ask_user); queued for review.'),
  ('awaiting_fit_review',  'fit_analyzed',         false, 'User approved the borderline fit via approval token.'),
  ('awaiting_fit_review',  'fit_rejected',         false, 'User denied the borderline fit via approval token.'),
  ('awaiting_fit_review',  'error',                true,  'Unrecoverable.')
ON CONFLICT (from_step, to_step) DO UPDATE
SET automated = EXCLUDED.automated, note = EXCLUDED.note;

-- ---------------------------------------------------------
-- 5. profile_briefs.is_stale never auto-flipped. generate_profile_briefs_v1.sql
--    deletes and rewrites every brief as is_stale=false on each run, but
--    nothing ever set it back to true when the underlying profile_assets
--    or profile_capabilities changed in between runs, so
--    build_profile_context_packs_v1.sql (which filters is_stale = false)
--    could silently keep using briefs that no longer reflected the
--    latest approved assets.
-- ---------------------------------------------------------

CREATE OR REPLACE FUNCTION mark_profile_briefs_stale() RETURNS trigger AS $$
BEGIN
  UPDATE profile_briefs SET is_stale = true WHERE is_stale = false;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Two triggers per table, not one combined INSERT-OR-UPDATE trigger:
-- referencing OLD in a WHEN clause on a trigger that also fires for
-- INSERT is unreliable across Postgres versions (OLD does not exist for
-- an INSERT event), so INSERT and UPDATE are kept as separate triggers
-- rather than risk a WHEN clause this migration can't test live against
-- a running server.

DROP TRIGGER IF EXISTS trg_profile_assets_stale_briefs_ins ON profile_assets;
CREATE TRIGGER trg_profile_assets_stale_briefs_ins
AFTER INSERT ON profile_assets
FOR EACH ROW
WHEN (NEW.status = 'approved')
EXECUTE FUNCTION mark_profile_briefs_stale();

DROP TRIGGER IF EXISTS trg_profile_assets_stale_briefs_upd ON profile_assets;
CREATE TRIGGER trg_profile_assets_stale_briefs_upd
AFTER UPDATE OF status ON profile_assets
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION mark_profile_briefs_stale();

DROP TRIGGER IF EXISTS trg_profile_capabilities_stale_briefs_ins ON profile_capabilities;
CREATE TRIGGER trg_profile_capabilities_stale_briefs_ins
AFTER INSERT ON profile_capabilities
FOR EACH ROW
WHEN (NEW.status = 'approved')
EXECUTE FUNCTION mark_profile_briefs_stale();

DROP TRIGGER IF EXISTS trg_profile_capabilities_stale_briefs_upd ON profile_capabilities;
CREATE TRIGGER trg_profile_capabilities_stale_briefs_upd
AFTER UPDATE OF status ON profile_capabilities
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION mark_profile_briefs_stale();

COMMENT ON FUNCTION mark_profile_briefs_stale() IS
  'Coarse but correct: any approval-relevant change to an asset or '
  'capability marks every brief stale, since generate_profile_briefs_v1.sql '
  'regenerates all briefs in one batch rather than per-role-family.';

-- ---------------------------------------------------------
-- 6. Broader autofill field coverage (services/autofill/autofill_agent_v1.py
--    FIELD_PATTERNS was expanded to recognise education, current-role, and
--    preference-style fields that show up across Greenhouse/Lever/Workday/
--    iCIMS/SmartRecruiters application forms; add the identity/preference
--    rows those new patterns resolve to, so they land in "NO VALUE
--    AVAILABLE" (reported, refused) instead of "UNRECOGNISED" (silently
--    skipped) until the user fills them in.
-- ---------------------------------------------------------

INSERT INTO applicant_identity (field_name, field_value, field_group, approved, notes)
VALUES
  ('university_name',  'FILL_ME', 'education', false, 'As it should appear on an application.'),
  ('degree',            'FILL_ME', 'education', false, 'e.g. Bachelor of Science.'),
  ('major',              'FILL_ME', 'education', false, 'Field of study.'),
  ('graduation_date',   'FILL_ME', 'education', false, 'Month/Year expected or actual.'),
  ('current_employer',  'FILL_ME', 'work',       false, 'Optional; leave FILL_ME if none.'),
  ('current_title',     'FILL_ME', 'work',       false, 'Optional; leave FILL_ME if none.'),
  ('desired_title',     'FILL_ME', 'work',       false, 'Optional.'),
  ('years_experience',  'FILL_ME', 'work',       false, 'Plain number as text, e.g. "1".'),
  ('twitter_url',       'FILL_ME', 'links',      false, 'Optional.'),
  ('other_url',         'FILL_ME', 'links',      false, 'Optional; generic "other profile" field some ATS ask for.'),
  ('referral_source',   'FILL_ME', 'misc',       false, '"How did you hear about us" answer.'),
  ('pronouns',           'FILL_ME', 'contact',    false, 'Optional.'),
  ('address_county',     'FILL_ME', 'address',    false, 'Optional; some US forms ask for it separately from city/state.')
ON CONFLICT (field_name) DO NOTHING;

INSERT INTO sensitive_answers
  (field_name, answer, answer_kind, requires_review, approved_by_user, question_hints, notes)
VALUES
  ('willing_to_relocate', 'FILL_ME', 'preference', true, false,
   '["willing to relocate", "relocation", "open to relocating"]'::jsonb,
   'Yes/No preference. Declare and approve before use; left unapproved by default.'),
  ('willing_to_travel', 'FILL_ME', 'preference', true, false,
   '["willing to travel", "travel required", "able to travel"]'::jsonb,
   'Yes/No preference. Declare and approve before use.'),
  ('remote_preference', 'FILL_ME', 'preference', true, false,
   '["remote", "hybrid", "on-site", "onsite preference", "work location preference"]'::jsonb,
   'Free-text preference. Declare and approve before use.'),
  ('employment_type_preference', 'FILL_ME', 'preference', true, false,
   '["employment type", "full-time or part-time", "contract or full time"]'::jsonb,
   'Free-text preference. Declare and approve before use.')
ON CONFLICT (field_name) DO NOTHING;

-- ---------------------------------------------------------
-- 7. Register the newly-wired components, matching the existing
--    convention of every capability having a component_registry row.
-- ---------------------------------------------------------

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('fit_review_gate', 'safety', 'L5',
   'Pause borderline-fit (60-75) applications for explicit user approval before research/doc-gen spend.',
   false, 'active',
   'Wired 2026-07-31. Previously ask_user and approve_research were treated identically.',
   now(), now()),
  ('company_research_router', 'service', 'L5',
   'Invoke company_research_v1.py as a best-effort step between fit_analyzed and doc generation.',
   false, 'active',
   'Wired 2026-07-31 into orchestrator_v1.py. Failure is non-fatal by design.',
   now(), now()),
  ('cost_gate', 'safety', 'L1',
   'Check the daily budget via cost_controller_v1.py before the first paid/LLM step of a job.',
   false, 'active',
   'Wired 2026-07-31 at the screened -> fit_analyzed transition.',
   now(), now()),
  ('ats_discovery', 'service', 'L0',
   'Poll public ATS read APIs (Greenhouse/Lever/Ashby/SmartRecruiters/Recruitee/Workable/Breezy) for new postings.',
   false, 'prototype',
   'Added 2026-07-31. Requires ats_companies rows to be populated by the user; no companies are seeded automatically.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
