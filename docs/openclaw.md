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

## Agent profiles (main / resume / cover_letter)

`bootstrap/openclaw/openclaw.template.json` defines three named agent profiles under
`agents.list`, each with its own workspace so a resume-drafting agent and a
cover-letter-drafting agent don't share conversation context:

| id | used by (env var) | called from |
|---|---|---|
| `main` | `OPENCLAW_AGENT_RESEARCH`, `OPENCLAW_AGENT_BROWSE` | `services/research/company_research_v1.py`, `services/browser-controller/browser_queue_worker.py` |
| `resume` | `OPENCLAW_AGENT_RESUME` | `services/browser-controller/browser_queue_worker.py` (doc_type == "resume") |
| `cover_letter` | `OPENCLAW_AGENT_COVER` | `services/browser-controller/browser_queue_worker.py` (doc_type == "cover_letter") |

`scripts/openclaw_bootstrap.py bootstrap` creates all three workspaces
(`~/.openclaw/workspace-main`, `-resume`, `-cover_letter`) and, as of
2026-07-31, the `agentDir` folders the `resume` and `cover_letter` profiles
declare (`~/.openclaw/agents/resume/agent`, `~/.openclaw/agents/cover_letter/agent`)
— earlier bootstrap runs left those two directories missing, which could make
those two profiles fail to start even though `openclaw.json` itself rendered
fine. Re-run `python scripts/openclaw_bootstrap.py bootstrap --force` if your
`~/.openclaw` predates this fix.

All three workspaces are seeded from the same generic files in
`bootstrap/openclaw/workspace/` (`AGENTS.md`, `IDENTITY.md`, `SOUL.md`, etc.) —
nothing differentiates the resume persona from the cover-letter persona beyond
their `id` and workspace path. If you want them to actually behave
differently (e.g. resume agent stays terse and bullet-driven, cover letter
agent writes prose), edit `~/.openclaw/workspace-resume/*.md` and
`~/.openclaw/workspace-cover_letter/*.md` by hand after bootstrapping — the
repo only ships one shared seed.

**Model note:** `resume` and `cover_letter` use `openrouter/auto` as their
model (template line ~87/94), and the shared `agents.defaults.model` block
primary is `openrouter/google/gemini-2.5-flash` with `ollama/deepseek-r1:14b`
as fallback — meaning, unless you run `openclaw auth login` for an OpenRouter
account, these two agents fall back to your local Ollama model instead of
OpenRouter. That's a real, working fallback (not a crash), but if you expect
OpenRouter-quality output and don't see it, this is why — check `openclaw
auth` status before assuming something is broken.
