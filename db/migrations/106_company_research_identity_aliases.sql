BEGIN;

CREATE TABLE IF NOT EXISTS company_research_identity_aliases (
  identity_key text PRIMARY KEY,
  research_cache_id uuid NOT NULL REFERENCES company_research_cache(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT company_research_identity_aliases_key_nonblank
    CHECK (identity_key ~ '^(domain|name):[^[:space:]].*$')
);

-- Every canonical key resolves to itself.  For historical cache rows, retain
-- the newest observation for a name-only alias; future writes update aliases
-- atomically with the canonical row.
INSERT INTO company_research_identity_aliases(identity_key,research_cache_id)
SELECT identity_key,id FROM company_research_cache
ON CONFLICT (identity_key) DO UPDATE SET research_cache_id=EXCLUDED.research_cache_id,updated_at=now();

WITH ranked AS (
  SELECT id,
         'name:' || lower(regexp_replace(trim(company_name), '\\s+', ' ', 'g')) AS identity_key,
         row_number() OVER (
           PARTITION BY lower(regexp_replace(trim(company_name), '\\s+', ' ', 'g'))
           ORDER BY last_refreshed_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
         ) AS ordinal
  FROM company_research_cache
  WHERE nullif(trim(company_name),'') IS NOT NULL
)
INSERT INTO company_research_identity_aliases(identity_key,research_cache_id)
SELECT identity_key,id FROM ranked WHERE ordinal=1
ON CONFLICT (identity_key) DO NOTHING;

COMMIT;
