-- =========================================================
-- 040_message_reply_layer.sql
-- L8 -- UNIFIED MESSAGE REPLY
--
-- message_threads already exists but nothing stores the individual
-- messages, so a thread has no history to reply against. This adds:
--
--   messages            -- one row per message, inbound or outbound
--   thread_classifications -- the vocabulary the classifier may use
--   drafted_replies     -- replies awaiting approval, with evidence_map
--
-- Grounding contract, identical to L6:
--   A reply may only assert things backed by approved profile assets.
--   Scheduling and courtesy text carries no claim and needs no source.
--   Anything about experience, skills, or availability does.
--
-- Sending is gated the same way submitting is: an approval_request of
-- type 'send_message' must be redeemed first. Nothing here sends mail.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. Messages
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id         uuid NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,

  direction         text NOT NULL,          -- inbound / outbound
  external_id       text,                   -- provider message id, for dedupe
  sender            text,
  recipient         text,
  subject           text,
  body_text         text NOT NULL,

  sent_at           timestamptz,
  received_at       timestamptz,

  -- Inbound message bodies are third-party content. Anything derived from
  -- them is treated as data, never as instructions.
  is_processed      boolean NOT NULL DEFAULT false,
  processed_at      timestamptz,

  created_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed
  ON messages(is_processed) WHERE direction = 'inbound' AND is_processed = false;
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_external
  ON messages(external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------
-- 2. Classification vocabulary
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS thread_classifications (
  classification  text PRIMARY KEY,
  description     text NOT NULL,
  needs_reply     boolean NOT NULL DEFAULT true,
  needs_human     boolean NOT NULL DEFAULT false,
  triggers_l9     boolean NOT NULL DEFAULT false,
  sort_order      int NOT NULL
);

INSERT INTO thread_classifications
  (classification, description, needs_reply, needs_human, triggers_l9, sort_order)
VALUES
  ('interview_invite',  'Invitation to interview or schedule a call.',
   true,  false, true,  10),
  ('scheduling',        'Coordinating a time for an already-agreed meeting.',
   true,  false, false, 20),
  ('info_request',      'Recruiter asking for details, documents, or clarification.',
   true,  false, false, 30),
  ('assessment_invite', 'Take-home task or online assessment link.',
   true,  true,  false, 40),
  ('offer',             'Job offer or compensation discussion.',
   true,  true,  false, 50),
  ('rejection',         'Application declined.',
   false, false, false, 60),
  ('status_update',     'Progress update requiring no action.',
   false, false, false, 70),
  ('recruiter_outreach','Unsolicited approach about a different role.',
   true,  false, false, 80),
  ('automated',         'No-reply system notification.',
   false, false, false, 90),
  ('spam',              'Irrelevant or fraudulent.',
   false, false, false, 100),
  ('unclear',           'Could not be classified confidently.',
   false, true,  false, 110)
ON CONFLICT (classification) DO UPDATE
SET description = EXCLUDED.description,
    needs_reply = EXCLUDED.needs_reply,
    needs_human = EXCLUDED.needs_human,
    triggers_l9 = EXCLUDED.triggers_l9,
    sort_order  = EXCLUDED.sort_order;

-- Offers and assessments always route to a human. An offer is a negotiation
-- and an assessment is work the candidate must actually do; neither is
-- something a drafting tool should answer on its own.

ALTER TABLE message_threads
  ADD COLUMN IF NOT EXISTS classification_confidence numeric,
  ADD COLUMN IF NOT EXISTS classified_at             timestamptz,
  ADD COLUMN IF NOT EXISTS classifier_version        text;

-- ---------------------------------------------------------
-- 3. Drafted replies
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS drafted_replies (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id         uuid NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
  in_reply_to       uuid REFERENCES messages(id) ON DELETE SET NULL,
  application_id    uuid REFERENCES applications(id) ON DELETE SET NULL,

  subject           text,
  body_text         text NOT NULL,

  classification    text REFERENCES thread_classifications(classification),
  asset_ids_used    jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_map      jsonb NOT NULL DEFAULT '{}'::jsonb,

  writer_version    text,
  writer_model      text,

  qa_status         text,                   -- pass / revise / fail
  qa_report         jsonb NOT NULL DEFAULT '{}'::jsonb,
  qa_checked_at     timestamptz,

  approved          boolean NOT NULL DEFAULT false,
  sent              boolean NOT NULL DEFAULT false,
  sent_at           timestamptz,

  version           int NOT NULL DEFAULT 1,
  revision_of       uuid REFERENCES drafted_replies(id) ON DELETE SET NULL,
  revision_round    int NOT NULL DEFAULT 0,

  created_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_drafted_replies_thread
  ON drafted_replies(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_drafted_replies_qa ON drafted_replies(qa_status);

-- Approval requires passing QA, and sending requires approval. Enforced in
-- the schema so a bug in the writer cannot skip either step.
ALTER TABLE drafted_replies
  DROP CONSTRAINT IF EXISTS chk_reply_approval_requires_qa;
ALTER TABLE drafted_replies
  ADD CONSTRAINT chk_reply_approval_requires_qa
  CHECK (approved = false OR qa_status = 'pass');

ALTER TABLE drafted_replies
  DROP CONSTRAINT IF EXISTS chk_reply_sent_requires_approval;
ALTER TABLE drafted_replies
  ADD CONSTRAINT chk_reply_sent_requires_approval
  CHECK (sent = false OR approved = true);

-- ---------------------------------------------------------
-- 4. Views
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_threads_needing_reply AS
SELECT
  mt.id AS thread_id,
  mt.source,
  mt.company,
  mt.person_name,
  mt.classification,
  tc.needs_human,
  tc.triggers_l9,
  mt.linked_application_id,
  mt.last_message_at,
  (SELECT count(*) FROM messages m
    WHERE m.thread_id = mt.id AND m.direction = 'inbound'
      AND m.is_processed = false) AS unprocessed_inbound
FROM message_threads mt
LEFT JOIN thread_classifications tc ON tc.classification = mt.classification
WHERE tc.needs_reply = true
  AND NOT EXISTS (
    SELECT 1 FROM drafted_replies dr
    WHERE dr.thread_id = mt.id AND dr.sent = false
  )
ORDER BY mt.last_message_at DESC NULLS LAST;

CREATE OR REPLACE VIEW v_replies_pending_qa AS
SELECT id AS reply_id, thread_id, classification, version, revision_round, created_at
FROM drafted_replies
WHERE qa_status IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM drafted_replies child WHERE child.revision_of = drafted_replies.id
  )
ORDER BY created_at;

CREATE OR REPLACE VIEW v_replies_awaiting_approval AS
SELECT
  dr.id AS reply_id,
  mt.company,
  mt.person_name,
  dr.classification,
  dr.subject,
  left(dr.body_text, 160) AS preview,
  dr.created_at
FROM drafted_replies dr
JOIN message_threads mt ON mt.id = dr.thread_id
WHERE dr.qa_status = 'pass' AND dr.approved = false AND dr.sent = false
ORDER BY dr.created_at;

CREATE OR REPLACE VIEW v_threads_needing_human AS
SELECT
  mt.id AS thread_id,
  mt.company,
  mt.person_name,
  mt.classification,
  tc.description,
  mt.last_message_at
FROM message_threads mt
JOIN thread_classifications tc ON tc.classification = mt.classification
WHERE tc.needs_human = true OR mt.needs_user_attention = true
ORDER BY mt.last_message_at DESC NULLS LAST;

-- ---------------------------------------------------------
-- 5. Register components
-- ---------------------------------------------------------

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('message_classifier', 'agent', 'L8',
   'Classify an inbound recruiter message into a handling category.',
   true, 'prototype',
   'Message bodies are third-party content and are treated as data only.',
   now(), now()),
  ('reply_writer', 'agent', 'L8',
   'Draft a reply grounded in approved profile assets.',
   true, 'prototype',
   'Factual claims must cite an asset. Offers and assessments are not drafted.',
   now(), now()),
  ('reply_truth_checker', 'safety', 'L8',
   'Verify each factual claim in a drafted reply against its cited asset.',
   true, 'prototype',
   'Reuses the L6 per-claim verification approach.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
