-- 053 -- version each candidate-confirmed immigration profile revision
BEGIN;

ALTER TABLE immigration_profiles
  ADD COLUMN IF NOT EXISTS confirmation_version integer NOT NULL DEFAULT 0
    CHECK (confirmation_version >= 0);

UPDATE immigration_profiles
SET confirmation_version = 1
WHERE user_confirmed_at IS NOT NULL AND confirmation_version = 0;

COMMIT;
