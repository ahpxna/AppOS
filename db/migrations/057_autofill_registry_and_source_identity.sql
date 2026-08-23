-- 057 -- Complete approved-field registry and source posting identities.
BEGIN;

INSERT INTO applicant_identity (field_name, field_value, field_group, approved, notes)
VALUES
  ('middle_name', 'FILL_ME', 'contact', false, 'Optional legal middle name or initial.'),
  ('phone_country_code', 'FILL_ME', 'contact', false, 'Optional phone dialing prefix.'),
  ('address_line2', 'FILL_ME', 'address', false, 'Optional apartment, suite, or unit.'),
  ('address_postal_ext', 'FILL_ME', 'address', false, 'Optional ZIP/postal extension.')
ON CONFLICT (field_name) DO NOTHING;

-- LinkedIn has a source-wide numeric posting id, unlike ATS providers where
-- the same external id can exist under separate company accounts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_linkedin_source_job
  ON applications (source, source_job_id)
  WHERE source = 'linkedin' AND source_job_id IS NOT NULL;

COMMIT;
