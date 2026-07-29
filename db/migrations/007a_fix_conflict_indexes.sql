-- Fix ON CONFLICT support for nullable unique keys.
-- PostgreSQL ON CONFLICT(column) needs a matching non-partial unique index/constraint.

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_files_sha256_full_unique
ON raw_files(sha256);

CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_context_packs_input_hash_full_unique
ON profile_context_packs(input_hash);
