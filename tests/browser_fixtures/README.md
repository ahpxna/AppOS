# Local fake ATS fixtures

These pages are intentionally local and contain no real applicant data. They
are the only pages permitted for the first OpenClaw browser smoke tests:

```bash
python -m http.server 8000 --directory tests/browser_fixtures
```

Open `http://127.0.0.1:8000/basic_form.html?job=123`. Do not use a real ATS
until the DB lifecycle gate and local fixture tests pass. `dynamic_form.html`
replaces input elements on every input event so an accessibility snapshot must
receive new refs; `wrong_job_same_origin.html` is the same host but another
job identity.
