-- Insert one fake browser task to test queue behavior

INSERT INTO browser_tasks (
  task_type,
  requested_by,
  status,
  priority,
  input_json,
  timeout_seconds,
  max_retries
)
VALUES (
  'test_open_url',
  'manual_seed',
  'queued',
  'normal',
  jsonb_build_object(
    'url', 'https://example.com',
    'note', 'fake task to test browser_tasks queue'
  ),
  120,
  2
)
RETURNING id, task_type, status, priority, input_json, created_at;
