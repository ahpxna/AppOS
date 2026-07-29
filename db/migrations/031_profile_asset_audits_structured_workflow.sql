CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS profile_asset_audits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_asset_id uuid NOT NULL REFERENCES profile_assets(id) ON DELETE CASCADE,
  audit_version text NOT NULL,
  audit_method text NOT NULL,
  audit_status text NOT NULL,
  severity text NOT NULL DEFAULT 'low',
  recommended_action text NOT NULL DEFAULT 'allow',
  finding_count integer NOT NULL DEFAULT 0,
  findings_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  auditor_model text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(profile_asset_id, audit_version)
);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_asset
ON profile_asset_audits(profile_asset_id);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_status
ON profile_asset_audits(audit_status);

CREATE INDEX IF NOT EXISTS idx_profile_asset_audits_action
ON profile_asset_audits(recommended_action);
