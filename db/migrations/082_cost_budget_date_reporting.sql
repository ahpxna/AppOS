BEGIN;

-- Daily reports and quota admission must use immutable reservation ownership,
-- not the wall-clock date on which an API response happened to settle.
CREATE OR REPLACE VIEW v_cost_today AS
SELECT
  CURRENT_DATE                                   AS date,
  COALESCE(SUM(cl.estimated_cost_usd), 0)        AS spent_usd,
  COUNT(*)                                       AS calls,
  COUNT(*) FILTER (WHERE cl.is_local)            AS local_calls,
  COUNT(*) FILTER (WHERE NOT cl.is_local)        AS paid_calls,
  COALESCE(SUM(cl.input_tokens), 0)              AS input_tokens,
  COALESCE(SUM(cl.output_tokens), 0)             AS output_tokens
FROM cost_ledger cl
WHERE coalesce(cl.budget_date, cl.created_at::date) = CURRENT_DATE;

COMMIT;
