-- 103 -- Bind recruiter reply authority to the exact latest inbound message.
BEGIN;

ALTER TABLE drafted_replies
  ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_drafted_replies_active_thread
  ON drafted_replies(thread_id,created_at DESC)
  WHERE sent=false AND superseded_at IS NULL;

CREATE OR REPLACE FUNCTION jobos_supersede_reply_on_new_inbound()
RETURNS trigger AS $$
DECLARE
  v_latest_inbound_id uuid;
BEGIN
  IF NEW.direction <> 'inbound' THEN
    RETURN NEW;
  END IF;

  SELECT m.id INTO v_latest_inbound_id
    FROM messages m
   WHERE m.thread_id=NEW.thread_id AND m.direction='inbound'
   ORDER BY coalesce(m.received_at,m.created_at) DESC,m.created_at DESC,m.id DESC
   LIMIT 1;
  IF v_latest_inbound_id IS DISTINCT FROM NEW.id THEN
    RETURN NEW;
  END IF;

  UPDATE drafted_replies
     SET approved=false,superseded_at=coalesce(superseded_at,now())
   WHERE thread_id=NEW.thread_id AND sent=false AND superseded_at IS NULL
     AND in_reply_to IS DISTINCT FROM NEW.id;

  UPDATE approval_requests ar
     SET status='expired',executing_task_id=NULL
    FROM drafted_replies dr
   WHERE dr.thread_id=NEW.thread_id AND dr.superseded_at IS NOT NULL
     AND ar.type='send_message'
     AND ar.payload_json->>'drafted_reply_id'=dr.id::text
     AND ar.status IN ('pending','approved');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobos_supersede_reply_on_new_inbound ON messages;
CREATE TRIGGER trg_jobos_supersede_reply_on_new_inbound
AFTER INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION jobos_supersede_reply_on_new_inbound();

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
    WHERE dr.thread_id = mt.id AND dr.sent = false AND dr.superseded_at IS NULL
  )
ORDER BY mt.last_message_at DESC NULLS LAST;

CREATE OR REPLACE VIEW v_replies_pending_qa AS
SELECT id AS reply_id, thread_id, classification, version, revision_round, created_at
FROM drafted_replies
WHERE qa_status IS NULL AND superseded_at IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM drafted_replies child
     WHERE child.revision_of = drafted_replies.id AND child.superseded_at IS NULL
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
  AND dr.superseded_at IS NULL
ORDER BY dr.created_at;

INSERT INTO component_registry
  (name,component_type,layer,purpose,trainable,status,notes,created_at,updated_at)
VALUES
  ('message_reply_conversation_binding','safety','L8',
   'Invalidate unsent recruiter drafts and send approvals when a newer inbound message changes the exact conversation.',
   false,'active','Reply approval is bound to exact in_reply_to and latest inbound identity.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
