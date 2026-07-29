-- =========================================================
-- 038_l7_autofill_foundation.sql
-- L7 -- AUTOFILL + SUBMIT (data layer)
--
-- Three things L7 needs before it can fill anything:
--
-- 1. applicant_identity -- the basic PII that goes in every form.
--    Kept in the database, not in prompts. The autofill worker reads it
--    directly and substitutes it into the browser tool call, so the model
--    driving the browser never sees the values.
--
-- 2. sensitive_answers -- pre-declared answers to questions a model must
--    never guess. Split into two kinds:
--      eligibility : factual, legally consequential, must be truthful
--      eeo         : voluntary self-identification, declining is normal
--    Only rows with approved_by_user = true are ever used.
--
-- 3. allowed_domains -- where the browser agent may go at all. This is not
--    about restricting the user; it is about a job posting containing a link
--    that sends the agent somewhere it should not be.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. Applicant identity
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS applicant_identity (
  field_name    text PRIMARY KEY,
  field_value   text NOT NULL,
  field_group   text NOT NULL DEFAULT 'contact',
    -- contact / address / links / eligibility
  is_pii        boolean NOT NULL DEFAULT true,
  approved      boolean NOT NULL DEFAULT false,
  notes         text,
  updated_at    timestamptz DEFAULT now()
);

COMMENT ON TABLE applicant_identity IS
  'Values substituted into forms by the worker. Never placed in a model prompt.';

-- Placeholders. Fill these in before running L7; the worker refuses to
-- substitute a value that is still a placeholder or not approved.
INSERT INTO applicant_identity (field_name, field_value, field_group, approved, notes)
VALUES
  ('legal_first_name', 'FILL_ME', 'contact', false, 'As it appears on official documents.'),
  ('legal_last_name',  'FILL_ME', 'contact', false, NULL),
  ('preferred_name',   'FILL_ME', 'contact', false, 'Optional.'),
  ('email',            'FILL_ME', 'contact', false, NULL),
  ('phone',            'FILL_ME', 'contact', false, 'Include country code.'),
  ('address_line1',    'FILL_ME', 'address', false, NULL),
  ('address_city',     'FILL_ME', 'address', false, NULL),
  ('address_state',    'FILL_ME', 'address', false, NULL),
  ('address_postal',   'FILL_ME', 'address', false, NULL),
  ('address_country',  'FILL_ME', 'address', false, NULL),
  ('linkedin_url',     'FILL_ME', 'links',   false, 'Optional.'),
  ('github_url',       'FILL_ME', 'links',   false, 'Optional.'),
  ('portfolio_url',    'FILL_ME', 'links',   false, 'Optional.')
ON CONFLICT (field_name) DO NOTHING;

-- ---------------------------------------------------------
-- 2. Sensitive answers
-- ---------------------------------------------------------

ALTER TABLE sensitive_answers
  ADD COLUMN IF NOT EXISTS answer_kind    text NOT NULL DEFAULT 'eeo',
  ADD COLUMN IF NOT EXISTS question_hints jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN sensitive_answers.answer_kind IS
  'eligibility = factual and legally consequential, must be truthful. '
  'eeo = voluntary self-identification, declining is a normal choice.';

-- Eligibility answers. These are conditions of employment, not voluntary
-- disclosures. Leaving them blank usually auto-rejects an application, and
-- answering them untruthfully is a misrepresentation to the employer, so they
-- are declared once by the user and then reused verbatim.
INSERT INTO sensitive_answers
  (field_name, answer, answer_kind, requires_review, approved_by_user, question_hints, notes)
VALUES
  ('work_authorization', 'Yes', 'eligibility', false, true,
   '["legally authorized to work", "authorized to work in the", "work authorization"]'::jsonb,
   'Declared by user.'),

  ('require_sponsorship', 'No', 'eligibility', false, true,
   '["require sponsorship", "need visa sponsorship", "now or in the future require"]'::jsonb,
   'Declared by user.'),

  ('age_18_or_older', 'Yes', 'eligibility', false, true,
   '["18 years of age", "at least 18", "18 or older"]'::jsonb,
   'Declared by user.')
ON CONFLICT (field_name) DO UPDATE
SET answer = EXCLUDED.answer,
    answer_kind = EXCLUDED.answer_kind,
    approved_by_user = EXCLUDED.approved_by_user,
    question_hints = EXCLUDED.question_hints,
    updated_at = now();

-- EEO answers. Voluntary under US federal contractor reporting rules; every
-- such form offers a decline option, and choosing it carries no penalty.
INSERT INTO sensitive_answers
  (field_name, answer, answer_kind, requires_review, approved_by_user, question_hints, notes)
VALUES
  ('race_ethnicity', 'I do not wish to self-identify', 'eeo', false, true,
   '["race", "ethnicity", "racial", "hispanic or latino"]'::jsonb,
   'Voluntary EEO question.'),

  ('gender', 'I do not wish to self-identify', 'eeo', false, true,
   '["gender", "sex"]'::jsonb, 'Voluntary EEO question.'),

  ('veteran_status', 'I do not wish to self-identify', 'eeo', false, true,
   '["veteran", "protected veteran", "military service"]'::jsonb,
   'Voluntary EEO question.'),

  ('disability_status', 'I do not wish to answer', 'eeo', false, true,
   '["disability", "disabled", "CC-305"]'::jsonb,
   'Voluntary EEO question.')
ON CONFLICT (field_name) DO UPDATE
SET answer = EXCLUDED.answer,
    answer_kind = EXCLUDED.answer_kind,
    approved_by_user = EXCLUDED.approved_by_user,
    question_hints = EXCLUDED.question_hints,
    updated_at = now();

-- Questions that must always stop and wait for the user, whatever else
-- happens. No stored answer exists for these on purpose.
CREATE TABLE IF NOT EXISTS always_pause_fields (
  pattern   text PRIMARY KEY,
  reason    text NOT NULL
);

INSERT INTO always_pause_fields (pattern, reason)
VALUES
  ('(?i)social security|\bssn\b',           'Government identifier. Never autofilled.'),
  ('(?i)date of birth|\bdob\b|birth date',  'Identity data with fraud exposure.'),
  ('(?i)driver.?s licen[cs]e',              'Government identifier.'),
  ('(?i)passport',                          'Government identifier.'),
  ('(?i)bank|routing number|account number','Financial data.'),
  ('(?i)salary expectation|desired salary|compensation expectation',
   'Negotiation decision belonging to the user.'),
  ('(?i)current salary|salary history',     'Often unlawful to ask; user decides.'),
  ('(?i)criminal|conviction|felony|background check',
   'Legally consequential disclosure.'),
  ('(?i)reference.{0,20}(name|email|phone)','Involves third parties who must consent.'),
  ('(?i)electronic signature|e-sign|\bsign here\b',
   'A signature is an act of attestation by the user.'),
  ('(?i)password|security question',        'Credential.')
ON CONFLICT (pattern) DO UPDATE SET reason = EXCLUDED.reason;

-- ---------------------------------------------------------
-- 3. Browser domain whitelist
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS allowed_domains (
  domain      text PRIMARY KEY,
  category    text NOT NULL DEFAULT 'ats',
  enabled     boolean NOT NULL DEFAULT true,
  notes       text,
  created_at  timestamptz DEFAULT now()
);

COMMENT ON TABLE allowed_domains IS
  'Domains the browser agent may visit. Guards against a job posting linking '
  'the agent somewhere it should not go, not against the user.';

INSERT INTO allowed_domains (domain, category, notes) VALUES
  ('linkedin.com',        'job_board', NULL),
  ('indeed.com',          'job_board', NULL),
  ('glassdoor.com',       'job_board', NULL),
  ('joinhandshake.com',   'job_board', 'University careers platform.'),
  ('ziprecruiter.com',    'job_board', NULL),
  ('greenhouse.io',       'ats', NULL),
  ('boards.greenhouse.io','ats', NULL),
  ('lever.co',            'ats', NULL),
  ('jobs.lever.co',       'ats', NULL),
  ('ashbyhq.com',         'ats', NULL),
  ('jobs.ashbyhq.com',    'ats', NULL),
  ('myworkdayjobs.com',   'ats', 'Workday tenant domain.'),
  ('workday.com',         'ats', NULL),
  ('smartrecruiters.com', 'ats', NULL),
  ('icims.com',           'ats', NULL),
  ('taleo.net',           'ats', NULL),
  ('successfactors.com',  'ats', NULL),
  ('bamboohr.com',        'ats', NULL),
  ('workable.com',        'ats', NULL),
  ('breezy.hr',           'ats', NULL),
  ('jobvite.com',         'ats', NULL),
  ('example.com',         'test', 'Used by the worker smoke test.')
ON CONFLICT (domain) DO NOTHING;

-- ---------------------------------------------------------
-- 4. Views
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_autofill_ready_values AS
SELECT field_name, field_value, field_group, 'identity' AS source
FROM applicant_identity
WHERE approved = true AND field_value <> 'FILL_ME'
UNION ALL
SELECT field_name, answer, answer_kind, 'sensitive' AS source
FROM sensitive_answers
WHERE approved_by_user = true;

COMMENT ON VIEW v_autofill_ready_values IS
  'The only values L7 may put into a form. Unapproved and placeholder rows '
  'are structurally unreachable.';

CREATE OR REPLACE VIEW v_identity_incomplete AS
SELECT field_name, field_group, approved,
       CASE WHEN field_value = 'FILL_ME' THEN 'placeholder' ELSE 'not approved' END AS problem
FROM applicant_identity
WHERE field_value = 'FILL_ME' OR approved = false
ORDER BY field_group, field_name;

-- ---------------------------------------------------------
-- 5. Register components
-- ---------------------------------------------------------

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('autofill_agent', 'agent', 'L7',
   'Fill application form fields from approved database values.',
   false, 'prototype',
   'Reads values from the database and substitutes them into browser tool '
   'calls. PII is never placed in a model prompt.',
   now(), now()),
  ('sensitive_field_gate', 'safety', 'L7',
   'Detect fields that must not be autofilled and pause for the user.',
   false, 'prototype',
   'Pattern-matched against always_pause_fields before any typing occurs.',
   now(), now()),
  ('final_submit_gate', 'safety', 'L7',
   'Prevent any automated path from submitting an application.',
   false, 'prototype',
   'There is no code path that clicks submit. This is deliberate.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
