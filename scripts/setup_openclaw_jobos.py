#!/usr/bin/env python3
"""One-command, non-interactive OpenClaw setup for the JobOS architecture.

This script creates the isolated OpenClaw home, four agent workspaces, the
browser/CDP profile, and the least-privilege tool policy from tracked files.
It reads an untracked ``.env`` and optional secrets file without printing their
contents. It does not start a gateway, invoke an LLM, log in to a site, or
install arbitrary skills.

Examples:
  python scripts/setup_openclaw_jobos.py --mode native
  python scripts/setup_openclaw_jobos.py --mode docker --force
  python scripts/setup_openclaw_jobos.py --mode docker --dry-run
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import sys
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = REPO_ROOT / "scripts" / "openclaw_bootstrap.py"
RUNTIME_INSTALLER = REPO_ROOT / "scripts" / "install_openclaw_runtime.py"
REQUIRED_AGENTS = {"main", "resume", "cover_letter", "repo_coordinator"}
REQUIRED_DENIES = {"exec", "process", "write", "edit", "apply_patch", "file_write"}
RUNTIME_PROVIDER_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple dotenv assignments locally without shell evaluation.

    Shell evaluation would make a setup file an execution path. Values are
    needed only in this child process and are never printed.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def ensure_gateway_token(env_path: Path) -> bool:
    """Append a private random gateway token only when no usable token exists.

    The token is an operational secret, not a user credential. Generating it
    locally avoids making setup depend on a copy/paste step while preserving
    any token the operator already configured. The value is never returned,
    printed, or written to tracked files.
    """
    current = read_env_file(env_path)
    existing = current.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if existing and not existing.startswith(("CHANGE_ME", "__")):
        return False
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with env_path.open("a", encoding="utf-8") as handle:
        if env_path.stat().st_size:
            handle.write("\n")
        handle.write(f"OPENCLAW_GATEWAY_TOKEN={secrets.token_urlsafe(32)}\n")
    os.chmod(env_path, 0o600)
    return True


@contextmanager
def temporary_environment(values: dict[str, str]) -> Iterator[None]:
    """Apply untracked configuration only while rendering this local config."""
    old_values = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in old_values.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("jobos_openclaw_bootstrap", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bootstrap helper: {BOOTSTRAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inspect_rendered_config(config_path: Path) -> list[dict[str, str]]:
    """Verify architecture contracts without revealing config tokens or keys."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    agents = {item.get("id") for item in config.get("agents", {}).get("list", [])}
    denied = set(config.get("tools", {}).get("deny", []))
    cdp_url = str(config.get("browser", {}).get("profiles", {}).get("remote", {}).get("cdpUrl") or "")
    checks = [
        {"name": "gateway_token_configured", "status": "pass" if config.get("gateway", {}).get("auth", {}).get("token") else "fail"},
        {"name": "remote_cdp_profile", "status": "pass" if cdp_url.startswith(("http://", "https://")) else "fail"},
        {"name": "four_isolated_agents", "status": "pass" if REQUIRED_AGENTS <= agents else "fail"},
        {"name": "tool_side_effect_denies", "status": "pass" if REQUIRED_DENIES <= denied else "fail"},
        {"name": "browser_plugin_enabled", "status": "pass" if config.get("plugins", {}).get("entries", {}).get("browser", {}).get("enabled") else "fail"},
    ]
    return checks


def write_provider_runtime_env(openclaw_home: Path) -> list[str]:
    """Stage only explicitly supplied model keys in the private runtime env.

    OpenClaw reads ``~/.openclaw/.env`` on the gateway host. Keeping provider
    keys there (rather than in JSON config or a workspace file) works for a
    native gateway and the Docker volume, while this function never prints a
    value or replaces an existing key with an empty one.
    """
    provided = {
        key: os.environ[key].strip()
        for key in RUNTIME_PROVIDER_KEYS
        if os.environ.get(key, "").strip()
    }
    if not provided:
        return []
    env_path = openclaw_home / ".env"
    current = read_env_file(env_path)
    current.update(provided)
    text = "".join(f"{key}={value}\n" for key, value in sorted(current.items()))
    env_path.write_text(text, encoding="utf-8")
    os.chmod(env_path, 0o600)
    return sorted(provided)


def openclaw_cli_env(target_home: Path) -> dict[str, str]:
    """Use JobOS's bundled Node for its private OpenClaw when available.

    Recent OpenClaw releases deliberately reject unsupported Node versions.
    The local runtime is preferred for validation/plugin setup so a global
    Homebrew Node cannot make an otherwise valid JobOS bootstrap fail.
    """
    env = dict(os.environ)
    node_candidates = sorted(
        (REPO_ROOT / "data" / "openclaw-runtime").glob("node-runtime-*/bin"),
        reverse=True,
    )
    if node_candidates and (node_candidates[0] / "node").is_file():
        env["PATH"] = f"{node_candidates[0]}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(target_home)
    env["OPENCLAW_CONFIG_PATH"] = str(target_home / ".openclaw" / "openclaw.json")
    return env


def validate_with_cli(target_home: Path, binary: str) -> dict[str, str]:
    """Run schema validation only; it does not start the gateway or a model."""
    binary_path = shutil.which(binary)
    if not binary_path:
        return {"name": "openclaw_config_validate", "status": "skipped", "detail": "openclaw binary not on PATH"}
    env = openclaw_cli_env(target_home)
    try:
        proc = subprocess.run(
            [binary_path, "config", "validate"], env=env, capture_output=True,
            text=True, timeout=45,
        )
    except subprocess.TimeoutExpired:
        return {"name": "openclaw_config_validate", "status": "fail", "detail": "timed out"}
    detail = (proc.stdout or proc.stderr or "").strip().replace("\n", " ")[:300]
    return {
        "name": "openclaw_config_validate",
        "status": "pass" if proc.returncode == 0 else "fail",
        "detail": detail or f"exit={proc.returncode}",
    }


def install_deepseek_plugin(target_home: Path, binary: str) -> dict[str, str]:
    """Install only the official DeepSeek provider plugin when requested.

    This is opt-in because it downloads a package. Model keys are not required
    for the installation and are never passed on the command line.
    """
    binary_path = shutil.which(binary)
    if not binary_path:
        return {"name": "deepseek_provider_plugin", "status": "fail", "detail": "openclaw binary not on PATH"}
    env = openclaw_cli_env(target_home)
    try:
        proc = subprocess.run(
            [binary_path, "plugins", "install", "@openclaw/deepseek-provider"],
            env=env, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"name": "deepseek_provider_plugin", "status": "fail", "detail": "installation timed out"}
    detail = (proc.stdout or proc.stderr or "").strip().replace("\n", " ")[-300:]
    return {
        "name": "deepseek_provider_plugin",
        "status": "pass" if proc.returncode == 0 else "fail",
        "detail": detail or f"exit={proc.returncode}",
    }


def install_private_runtime() -> dict[str, str]:
    """Run the explicit networked runtime installer without exposing secrets."""
    try:
        proc = subprocess.run(
            [sys.executable, str(RUNTIME_INSTALLER), "--json"],
            capture_output=True, text=True, timeout=720,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Pinned Node/OpenClaw runtime installation timed out.")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Runtime installer returned invalid status output.") from exc
    if proc.returncode != 0 or result.get("status") != "ok":
        raise RuntimeError(str(result.get("error") or "Pinned runtime installation failed."))
    return {"name": "private_openclaw_runtime", "status": "pass", "detail": result.get("openclaw", "installed")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up isolated JobOS OpenClaw agents non-interactively.")
    parser.add_argument("--mode", choices=("native", "docker"), default="native")
    parser.add_argument("--target-home", type=Path, help="Parent directory that will contain .openclaw.")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--secrets-file", type=Path, help="Optional OpenClaw-only secrets file.")
    parser.add_argument("--cdp-url", help="Override remote Chrome CDP URL for this setup.")
    parser.add_argument("--openclaw-bin", help="Override the private OpenClaw binary (normally unnecessary).")
    parser.add_argument("--install-runtime", action="store_true",
                        help="Download and verify the pinned private Node/OpenClaw runtime before rendering config.")
    parser.add_argument("--force", action="store_true", help="Refresh an existing generated OpenClaw home with backups.")
    parser.add_argument("--dry-run", action="store_true", help="Render and inspect only; do not write files.")
    parser.add_argument("--install-deepseek-plugin", action="store_true", help="Install the official provider plugin after setup.")
    parser.add_argument("--generate-gateway-token", action="store_true",
                        help="Generate and privately append a gateway token to .env when none exists.")
    parser.add_argument("--skip-cli-validation", action="store_true")
    args = parser.parse_args()

    runtime_check = None
    if args.install_runtime and not args.dry_run:
        try:
            runtime_check = install_private_runtime()
        except RuntimeError as exc:
            print(json.dumps({"status": "error", "stage": "private_openclaw_runtime", "error": str(exc)}, indent=2))
            return 1
    private_runtime_bin = REPO_ROOT / "data" / "openclaw-runtime" / "node" / "node_modules" / ".bin" / "openclaw"
    args.openclaw_bin = args.openclaw_bin or os.getenv("OPENCLAW_BIN") or (
        str(private_runtime_bin) if private_runtime_bin.is_file() else "openclaw"
    )

    target_home = args.target_home
    if target_home is None:
        target_home = Path.home() if args.mode == "native" else REPO_ROOT / "data" / "openclaw-runtime"
    target_home = target_home.expanduser().resolve()
    cdp_url = args.cdp_url or (
        "http://browser:9222" if args.mode == "docker" else "http://127.0.0.1:9222"
    )
    env_path = args.env_file.expanduser()
    generated_gateway_token = False
    if args.generate_gateway_token and not args.dry_run:
        generated_gateway_token = ensure_gateway_token(env_path)
    env_values = read_env_file(env_path)
    env_values["OPENCLAW_BROWSER_CDP_URL"] = cdp_url

    module = load_bootstrap_module()
    with temporary_environment(env_values):
        values: dict[str, Any] = module.load_env_values()
        secrets_path = args.secrets_file
        if secrets_path is None:
            candidate = REPO_ROOT / "bootstrap" / "openclaw" / "secrets.local.json"
            secrets_path = candidate if candidate.exists() else None
        if secrets_path:
            values.update(module.load_secrets(secrets_path.expanduser()))
        values = module.normalize_values(values)
        module.validate_gateway_token(values, allow_missing=False)
        rendered = module.render_template(
            REPO_ROOT / "bootstrap" / "openclaw" / "openclaw.template.json", values, False
        )

        if args.dry_run:
            agents = {item.get("id") for item in rendered.get("agents", {}).get("list", [])}
            report = {
                "mode": args.mode,
                "target_home": str(target_home),
                "writes": False,
                "remote_cdp_configured": bool(rendered["browser"]["profiles"]["remote"].get("cdpUrl")),
                "agents": sorted(agents),
            "optional_features": {
                    "web_search": bool(rendered["plugins"]["entries"].get("google", {}).get("enabled")),
                    "telegram": bool(rendered["channels"]["telegram"].get("enabled")),
                    "gmail_hooks": bool(rendered["hooks"].get("enabled")),
                },
            }
            print(json.dumps(report, indent=2))
            return 0

        module.bootstrap(
            target_home=target_home,
            template_path=REPO_ROOT / "bootstrap" / "openclaw" / "openclaw.template.json",
            values=values,
            allow_missing=False,
            force=args.force,
        )
        provider_keys = write_provider_runtime_env(target_home / ".openclaw")

    config_path = target_home / ".openclaw" / "openclaw.json"
    checks = inspect_rendered_config(config_path)
    if runtime_check:
        checks.append(runtime_check)
    for agent_id in sorted(REQUIRED_AGENTS):
        workspace = target_home / ".openclaw" / f"workspace-{agent_id}"
        if agent_id == "main":
            workspace = target_home / ".openclaw" / "workspace-main"
        checks.append({"name": f"workspace_{agent_id}", "status": "pass" if workspace.is_dir() else "fail"})
    if not args.skip_cli_validation:
        checks.append(validate_with_cli(target_home, args.openclaw_bin))
    if args.install_deepseek_plugin:
        checks.append(install_deepseek_plugin(target_home, args.openclaw_bin))
    report = {
        "mode": args.mode,
        "target_home": str(target_home),
        "writes": True,
        "provider_keys_staged": provider_keys,
        "gateway_token_generated": generated_gateway_token,
        "checks": checks,
        "next": (
            "Start the Docker overlay with docker compose -f docker-compose.yml -f docker-compose.openclaw.yml up -d"
            if args.mode == "docker" else
            "Start the gateway under this dedicated OS user, then run the JobOS preflight with --check-browser."
        ),
    }
    print(json.dumps(report, indent=2))
    return 0 if all(item["status"] in {"pass", "skipped"} for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
