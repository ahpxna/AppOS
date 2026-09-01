-- 104 -- Bind auth/browser session observations to immutable application/JD identity.
BEGIN;

ALTER TABLE application_auth_sessions
  ADD COLUMN IF NOT EXISTS binding_job_url text,
  ADD COLUMN IF NOT EXISTS binding_jd_hash text;

UPDATE application_auth_sessions s
   SET binding_job_url=coalesce(a.job_url,''),binding_jd_hash=coalesce(a.jd_hash,'')
  FROM applications a
 WHERE a.id=s.application_id
   AND (s.binding_job_url IS NULL OR s.binding_jd_hash IS NULL);

ALTER TABLE application_auth_sessions
  ALTER COLUMN binding_job_url SET NOT NULL,
  ALTER COLUMN binding_jd_hash SET NOT NULL;

CREATE OR REPLACE FUNCTION jobos_validate_auth_session_application_identity()
RETURNS trigger AS $$
DECLARE
  v_job_url text;
  v_jd_hash text;
BEGIN
  SELECT coalesce(job_url,''),coalesce(jd_hash,'') INTO v_job_url,v_jd_hash
    FROM applications WHERE id=NEW.application_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'auth session application does not exist';
  END IF;
  IF NEW.binding_job_url IS DISTINCT FROM v_job_url
     OR NEW.binding_jd_hash IS DISTINCT FROM v_jd_hash THEN
    RAISE EXCEPTION 'auth session application/JD identity mismatch';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobos_validate_auth_session_application_identity
  ON application_auth_sessions;
CREATE TRIGGER trg_jobos_validate_auth_session_application_identity
BEFORE INSERT OR UPDATE OF application_id,binding_job_url,binding_jd_hash
ON application_auth_sessions
FOR EACH ROW EXECUTE FUNCTION jobos_validate_auth_session_application_identity();

INSERT INTO component_registry
  (name,component_type,layer,purpose,trainable,status,notes,created_at,updated_at)
VALUES
  ('auth_session_application_identity','safety','L3',
   'Bind same-tab auth redirects to the durable application job URL and JD identity before browser observations can advance workflow state.',
   false,'active','Legitimate redirects remain allowed; stale application/JD sessions and foreign application forms fail closed.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose,status=EXCLUDED.status,notes=EXCLUDED.notes,updated_at=now();

COMMIT;
