# OpenClaw setup

Recommended setup:

- Run OpenClaw under a dedicated OS user, separate from the human users who work in this repo.
- Give that OS user its own `~/.openclaw` config, browser profile, and workspace.
- Keep the browser session separate from your normal browsing tab/profile.

Why this matters:

- Sharing one logged-in Chrome profile between multiple people makes session bleed and accidental cross-account actions much more likely.
- A dedicated OS user keeps the agent's cookies, gateway token, and workspace isolated from your day-to-day browser state.

Fallback:

- `docker-compose.openclaw.yml` stays available as a containerized fallback when native installation is not practical.
- If you use the container fallback, keep the browser gateway bound to loopback only and treat it as the only browser session that automation may touch.

Bootstrap for a fresh machine:

- Run `python scripts/openclaw_bootstrap.py bootstrap` from this repo to render a local `~/.openclaw`.
- Put your machine-specific secrets in `bootstrap/openclaw/secrets.local.json` or export them as `OPENCLAW_*` env vars.
- If you have an old machine, first create a bundle with `python scripts/openclaw_bootstrap.py export --bundle /tmp/openclaw.bundle.tar.gz`, then restore it on the new one with `python scripts/openclaw_bootstrap.py import --bundle /tmp/openclaw.bundle.tar.gz --force`.
- The bootstrap keeps secrets out of git; only the template and workspace seed files live in the repo.

For the Windows box you mentioned:

- Prefer a dedicated Windows user for OpenClaw if you need two humans to use the repo at the same time.
- Give that user a separate Chrome profile and keep the automation tab there, not in the primary user's browser.
- If that separation is not possible, use the container fallback and keep the automation browser isolated from normal work.
