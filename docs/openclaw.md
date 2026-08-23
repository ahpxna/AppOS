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
- As of 2026-08-01, the `browser` service runs **headless** Chrome directly instead of
  `kasmweb/chrome`'s own noVNC desktop startup script. The Kasm image's script doesn't pass
  `--remote-debugging-port` through to Chrome, so OpenClaw's CDP endpoint (port 9222) never
  opened; overriding just the entrypoint to launch Chrome directly also failed, because
  non-headless Chrome needs an X display (Xvfb) that the Kasm script would normally start,
  and skipping the script skips that too. Headless Chrome sidesteps both problems -- CDP
  doesn't care whether Chrome is headed or not. Traded away: the noVNC desktop at port 6901
  for logging into sites by hand. If you need that back, you'd have to restore the original
  `kasmweb/chrome` entrypoint (no override) and find a different way to get
  `--remote-debugging-port` into its Chrome launch, which the image doesn't support out of
  the box.

## Browser runtime readiness

The queue worker can be checked without executing a browser task or invoking a
model:

```bash
python services/browser-controller/browser_queue_worker.py --health
```

It needs all of the following before `fetch_job_description` can run:

1. A reachable OpenClaw gateway on loopback (`127.0.0.1:18789`) with the same
   configured gateway token on client and gateway.
2. A reachable CDP browser at the configured remote profile endpoint
   (`http://browser:9222` inside the compose network for the container setup).
3. A dedicated JobOS Chrome profile that the user has signed into manually.
   JobOS never imports a cookie or password from the user's everyday browser.
   A user-initiated LinkedIn discovery task may read a small explicit result
   cap and auto-ingest validated JDs. It may not authenticate, solve CAPTCHA,
   create alerts, save jobs, message users, change preferences, or apply.

The health command and CDP browser do not require a local LLM. If a browser
task uses an OpenClaw agent, select its model via `OPENCLAW_*_MODEL`; it may be
an authenticated API provider rather than a local model. Do not put provider
tokens in this repository.

### Current form-write policy

`fill_application_form` never uses an OpenClaw LLM agent. Its deterministic
path writes only an approval-bound value to an exact field, pins one browser
target, rechecks allowed origin before/after every action, rematches refs after
each UI change, and verifies the result. Sensitive fields require an exact
user-confirmed semantic answer. The worker cannot submit an application; a
crash or ambiguous partial write becomes `needs_reconciliation`, never an
automatic replay.

## Non-interactive JobOS setup

Use the JobOS setup script instead of the interactive `openclaw onboard` wizard.
It creates the isolated config, four named agent workspaces, the remote CDP
profile, and the tool-deny policy in one run. It does **not** invoke a model,
open a browser page, or print a token.

`--generate-gateway-token` generates and privately saves a random
`OPENCLAW_GATEWAY_TOKEN` in the untracked `.env` when it is missing. Optional
`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY` values may be
kept in that same untracked file. The script copies only those provider keys to
the private OpenClaw runtime `.env`, with mode `0600`; it never stores them in
the JSON template, Git, reports, or agent workspace.

Native gateway under the dedicated OS user:

```bash
python scripts/setup_openclaw_jobos.py --mode native --install-runtime --force --generate-gateway-token
# Private Node 24 + OpenClaw, not the system/global OpenClaw installation.
python scripts/start_openclaw_jobos.py gateway
```

Docker browser/gateway overlay:

```bash
python scripts/setup_openclaw_jobos.py --mode docker --force
# optional: downloads the official provider plugin; no API key appears on CLI
python scripts/setup_openclaw_jobos.py --mode docker --force --install-deepseek-plugin
docker compose -f docker-compose.yml -f docker-compose.openclaw.yml up -d
python services/orchestrator/pipeline_preflight_v1.py --check-browser
```

`setup_openclaw_jobos.py` renders configuration. It does not download a runtime
unless `--install-runtime` is supplied; that explicit flag downloads and
checksum-verifies the pinned Node distribution, installs the pinned OpenClaw
package under ignored `data/openclaw-runtime/`, and verifies `node --version`
and `openclaw --version` before configuration validation. Docker mode writes the config under `data/openclaw-runtime/.openclaw` and
renders the remote CDP endpoint as `http://browser:9222`, the correct address
inside the Compose network. Native mode renders `http://127.0.0.1:9222` for a
locally exposed Chrome CDP listener. The host-side queue health check probes
both the OpenClaw RPC gateway and `GET /json/version` on CDP, without invoking
an LLM or opening a tab.

The four workspaces have distinct roles:

| Agent | Workspace | Autonomous safe work |
|---|---|---|
| `main` | `workspace-main` | User-initiated job/JD capture and public company research |
| `resume` | `workspace-resume` | Reserved workspace; Python L6 now drafts resumes through the shared grounded gateway |
| `cover_letter` | `workspace-cover_letter` | Reserved workspace; Python L6 now drafts cover letters through the shared grounded gateway |
| `repo_coordinator` | `workspace-repo_coordinator` | Summarise isolated worker reports only |

All four proceed without asking for confirmation for well-scoped, read-only
work. The config structurally denies shell/process execution and filesystem
writes. They must still stop for authentication, sending, uploading, form
submission, external modification, missing source evidence, or a request that
would create an unsupported candidate claim. They share concise reports and
evidence, not hidden reasoning.

### Provider routing

The Python JobOS pipeline and OpenClaw are separate consumers of model keys:

- Python stages use `services/common/llm_gateway.py`. It supports local Ollama,
  OpenAI-style APIs, and the DeepSeek URL style. Use per-role settings in
  `.env.example`: DeepSeek is suitable for bounded analysis/coordinator work;
  use an embedding-capable provider for `embed`, and choose a stronger provider
  for document generation/truth verification if desired.
- OpenClaw reads `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, and
  `OPENROUTER_API_KEY` from its private runtime
  `.env`. The official DeepSeek plugin is installed only with
  `--install-deepseek-plugin`; then model IDs can be set to
  `deepseek/deepseek-v4-flash` or `deepseek/deepseek-v4-pro`. OpenAI model IDs
  use `openai/...` after its normal provider authentication. Re-run the setup
  script after changing `OPENCLAW_*_MODEL` values.

The setup script validates the generated config without reaching a model. The
private runtime launcher keeps JobOS on its tested Node/OpenClaw pair. Run
`openclaw models status --check` only after configuring the desired provider
key/auth profile; add `--probe` only when you intentionally want a live API
call.

Legacy bootstrap details:

- `python scripts/openclaw_bootstrap.py bootstrap` remains available for a
  custom template/target, but `setup_openclaw_jobos.py` should be used for the
  standard JobOS deployment because it selects the right CDP network address.

- Put your machine-specific secrets in `bootstrap/openclaw/secrets.local.json` or export them as `OPENCLAW_*` env vars.
- If you have an old machine, first create a bundle with `python scripts/openclaw_bootstrap.py export --bundle /tmp/openclaw.bundle.tar.gz`, then restore it on the new one with `python scripts/openclaw_bootstrap.py import --bundle /tmp/openclaw.bundle.tar.gz --force`.
- The bootstrap keeps secrets out of git; only the template and workspace seed files live in the repo.

For the Windows box you mentioned:

- Prefer a dedicated Windows user for OpenClaw if you need two humans to use the repo at the same time.
- Give that user a separate Chrome profile and keep the automation tab there, not in the primary user's browser.
- If that separation is not possible, use the container fallback and keep the automation browser isolated from normal work.

## Agent profiles (main / resume / cover_letter / repo_coordinator)

`bootstrap/openclaw/openclaw.template.json` defines four named agent profiles under
`agents.list`, each with its own workspace so a resume-drafting agent and a
cover-letter-drafting agent don't share conversation context:

| id | used by (env var) | called from |
|---|---|---|
| `main` | `OPENCLAW_AGENT_RESEARCH`, `OPENCLAW_AGENT_BROWSE` | `services/research/company_research_v1.py`, `services/browser-controller/browser_queue_worker.py` |
| `resume` | `OPENCLAW_AGENT_RESUME` | Reserved; never receives a real application form or document content |
| `cover_letter` | `OPENCLAW_AGENT_COVER` | Reserved; never receives a real application form or document content |
| `repo_coordinator` | `OPENCLAW_AGENT_REPO_COORDINATOR` | `services/repo-audit/repo_coordinator_v1.py` |

`scripts/openclaw_bootstrap.py bootstrap` creates all four workspaces
(`~/.openclaw/workspace-main`, `-resume`, `-cover_letter`, `-repo_coordinator`)
and the `agentDir` folders the `resume`, `cover_letter`, and `repo_coordinator`
profiles declare.
— earlier bootstrap runs left those two directories missing, which could make
those two profiles fail to start even though `openclaw.json` itself rendered
fine. Re-run `python scripts/openclaw_bootstrap.py bootstrap --force` if your
`~/.openclaw` predates this fix.

All workspaces receive the shared safety/tool policy from
`bootstrap/openclaw/workspace/`, then receive a role-specific overlay from
`bootstrap/openclaw/workspace-profiles/`. These tracked files define the
resume, cover-letter, browser/research, and report-only coordinator boundaries;
edit the relevant overlay and re-run setup if the policy needs to evolve.

**Model note:** the template reads `OPENCLAW_*_MODEL` variables when bootstrap
runs. Each value can be a local model such as `ollama/qwen3:8b` or an
authenticated API-provider model such as `openrouter/auto`; defaults preserve
the prior OpenRouter-first/Ollama-fallback behavior. Re-run bootstrap with
`--force` after changing the variables. If you expect API-quality output and
don't see it, check `openclaw auth` status before assuming something is broken.
