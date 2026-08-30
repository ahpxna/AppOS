-- 099 -- Database authority for safe discovery regex preferences.
BEGIN;

CREATE OR REPLACE FUNCTION jobos_validate_job_search_preferences()
RETURNS trigger AS $$
DECLARE
  pattern text;
BEGIN
  FOREACH pattern IN ARRAY NEW.location_allow_patterns LOOP
    IF pattern IS NULL OR length(btrim(pattern)) < 1 OR length(pattern) > 160 THEN
      RAISE EXCEPTION 'location_allow_patterns entries must be 1..160 characters';
    END IF;

    -- Keep the DB boundary at least as conservative as the Python writer for
    -- common catastrophic nested/unbounded repetition shapes.  Runtime still
    -- uses a bounded regex engine as defense in depth.
    IF pattern ~ E'\\([^)]*[+*][^)]*\\)[+*]'
       OR pattern ~ E'\\.\\*[+*]'
       OR pattern ~ E'\\{[0-9]{3,}(,[0-9]*)?\\}' THEN
      RAISE EXCEPTION 'location_allow_patterns contains unsupported pathological repetition: %', pattern;
    END IF;

    BEGIN
      PERFORM '' ~* pattern;
    EXCEPTION WHEN SQLSTATE '2201B' THEN
      RAISE EXCEPTION 'invalid location_allow_patterns regex: %', pattern;
    END;
  END LOOP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobos_validate_job_search_preferences ON job_search_preferences;
CREATE TRIGGER trg_jobos_validate_job_search_preferences
BEFORE INSERT OR UPDATE OF location_allow_patterns ON job_search_preferences
FOR EACH ROW EXECUTE FUNCTION jobos_validate_job_search_preferences();

-- Validate legacy rows now rather than leaving a hidden invalid preference that
-- future writers assume satisfies the new DB authority.
UPDATE job_search_preferences
   SET location_allow_patterns = location_allow_patterns;

COMMIT;
