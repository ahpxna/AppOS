-- =========================================================
-- 031_profile_asset_audits_structured_workflow.sql
--
-- FIXED 2026-07-31 (verification pass): this file originally assumed
-- profile_asset_audits had NOT YET been created, and tried to create it
-- with a schema (audit_status/severity/recommended_action/finding_count/
-- findings_json/auditor_model + UNIQUE(profile_asset_id, audit_version))
-- that does not match reality.
--
-- profile_asset_audits is actually created earlier, in
-- 027_profile_intelligence_layer.sql, with a DIFFERENT schema:
-- audit_type/audit_model/audit_version/grounding_status/overclaim_risk/
-- information_loss_risk/evidence_coverage_score/specificity_score/
-- job_relevance_score/supported_claims/unsupported_claims/required_edits/
-- audit_notes -- and NO unique constraint.
--
-- On a fresh install, 027 runs first and creates the real table, so this
-- file's `CREATE TABLE IF NOT EXISTS` below became a silent no-op --
-- except the two `CREATE INDEX` statements that followed it referenced
-- `audit_status` and `recommended_action`, columns that do not exist on
-- the real table. That raised `column "audit_status" does not exist`,
-- which aborted this migration's transaction and everything after it in
-- this file (nothing else was in this file, but on a from-scratch install
-- this would also block every later migration from running via a plain
-- `for f in db/migrations/*.sql; do psql -f "$f"; done` loop, since most
-- such loops stop -- or leave the DB half-migrated -- on the first error).
--
-- This version is idempotent and safe to run against a database that has
-- already run 027, which is the only case that matters: this file never
-- successfully created its own table, so there is no prior state from
-- THIS file to preserve.
-- =========================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Belt-and-suspenders: create the 027 schema here too, in case migrations
-- are ever applied out of order. Column list matches 027 exactly.
CREATE TABLE IF NOT EXISTS profile_asset_audits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_asset_id uuid NOT NULL REFERENCES profile_assets(id) ON DELETE CASCADE,

  audit_type text NOT NULL DEFAULT 'grounding_overclaim_audit',
  audit_model text,
  audit_version text,

  grounding_status text NOT NULL DEFAULT 'pending',
  overclaim_risk text NOT NULL DEFAULT 'unknown',
  information_loss_risk text NOT NULL DEFAULT 'unknown',

  evidence_coverage_score numeric,
  specificity_score numeric,
  job_relevance_score numeric,

  supported_claims text[] NOT NULL DEFAULT '{}'::text[],
  unsupported_claims text[] NOT NULL DEFAULT '{}'::text[],
  required_edits text[] NOT NULL DEFAULT '{}'::text[],
  audit_notes text,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_asset
ON profile_asset_audits(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_grounding_status
ON profile_asset_audits(grounding_status);

-- Indexes actually usable by the real (027) schema. These support
-- v_profile_asset_deepseek_review's DISTINCT ON (profile_asset_id)
-- ... ORDER BY created_at DESC lookup, fixed in 041.
CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_type_version
ON profile_asset_audits(audit_type, audit_version);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_asset_created
ON profile_asset_audits(profile_asset_id, created_at DESC);

-- No UNIQUE constraint is added here on purpose: the real table allows
-- multiple audit rows per asset (one per re-run), which is exactly what
-- the DISTINCT ON fix in 041 is for. Adding a UNIQUE(profile_asset_id,
-- audit_version) now would fail immediately on any DB that already has
-- duplicate rows from repeated audit runs.
