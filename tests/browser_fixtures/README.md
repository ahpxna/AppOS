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

## v0.1.0 release CDP smoke

The release gate runs the repository-controlled fixture directly:

```bash
python scripts/release_cdp_smoke.py
```

It starts a temporary loopback HTTP server, opens only `basic_form.html?job=123`
in the isolated JobOS browser, pins the new target, snapshots it, fills the
`First name` field with a harmless smoke value, verifies the value from a fresh
snapshot, then closes the tab. It never invokes an agent/model, uploads,
submits, authenticates, or handles CAPTCHA/checkpoints. `verify-release --profile
v0.1.0` invokes this script itself; there is no arbitrary command environment
variable that can turn the CDP gate into a no-op.
