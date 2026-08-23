#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse


PLACEHOLDERS = {
    "__NOW_ISO__": lambda _: datetime.now(timezone.utc).isoformat(),
}

# These integrations are optional to the JobOS browser/runtime path.  Give
# absent values inert defaults and disable their config blocks below; only the
# gateway token is mandatory for a usable OpenClaw installation.
OPTIONAL_SECRET_DEFAULTS: Dict[str, Any] = {
    "HOOKS_TOKEN": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_ALLOW_FROM": [],
    "GMAIL_ACCOUNT": "",
    "GMAIL_TOPIC": "",
    "GMAIL_SUBSCRIPTION": "",
    "GMAIL_PUSH_TOKEN": "",
    "GOOGLE_WEBSEARCH_API_KEY": "",
}

# Configuration defaults are safe to persist in generated config and keep the
# callable bootstrap API useful for tests or automation that does not enter
# through ``load_env_values`` first.
MODEL_DEFAULTS: Dict[str, str] = {
    "MODEL_PRIMARY": "openai/gpt-5.4-mini",
    # Keep the standard installation API-only. Operators can explicitly set
    # an Ollama model when the host has enough resources for local inference.
    "MODEL_FALLBACK": "openrouter/free",
    "RESUME_MODEL": "openrouter/auto",
    "COVER_MODEL": "openrouter/auto",
    "REPO_COORDINATOR_MODEL": "openrouter/auto",
    "LINKEDIN_DISCOVERY_MODEL": "openrouter/auto",
    "HOOKS_MODEL": "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "GMAIL_MODEL": "openrouter/meta-llama/llama-3.3-70b-instruct:free",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_secrets(path: Path | None) -> Dict[str, Any]:
    secrets: Dict[str, Any] = {}
    if path is None:
        return secrets
    if not path.exists():
        raise SystemExit(f"Secrets file not found: {path}")
    if path.suffix.lower() == ".json":
        secrets.update(load_json(path))
        return secrets

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if value.startswith("[") or value.startswith("{"):
            try:
                secrets[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        if "," in value:
            items = [item.strip() for item in value.split(",") if item.strip()]
            parsed_items = []
            for item in items:
                if item.isdigit():
                    parsed_items.append(int(item))
                else:
                    parsed_items.append(item)
            secrets[key] = parsed_items
        elif value.isdigit():
            secrets[key] = int(value)
        else:
            secrets[key] = value
    return secrets


def load_env_values() -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for key in (
        "GATEWAY_TOKEN",
        "HOOKS_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOW_FROM",
        "GMAIL_ACCOUNT",
        "GMAIL_TOPIC",
        "GMAIL_SUBSCRIPTION",
        "GMAIL_PUSH_TOKEN",
        "GOOGLE_WEBSEARCH_API_KEY",
    ):
        value = os.getenv(f"OPENCLAW_{key}") or os.getenv(key)
        if value:
            values[key] = value
    # Models are configuration, not secrets. Defaults preserve the original
    # template behavior while allowing every OpenClaw agent to move between a
    # local Ollama model and an API-backed provider through one env surface.
    for key, default in MODEL_DEFAULTS.items():
        values[key] = os.getenv(f"OPENCLAW_{key}") or os.getenv(key) or default
    # The native CLI reaches a loopback Chrome sidecar.  The Docker overlay
    # renders the same template with http://browser:9222 before its config
    # volume is mounted, so the URL is not accidentally hard-coded for the
    # wrong network namespace.
    values["BROWSER_CDP_URL"] = (
        os.getenv("OPENCLAW_BROWSER_CDP_URL")
        or os.getenv("JOBOS_BROWSER_CDP_URL")
        or "http://127.0.0.1:9222"
    )
    for key, default in OPTIONAL_SECRET_DEFAULTS.items():
        values.setdefault(key, default)
    return values


def normalize_values(values: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(values)
    allow_from = normalized.get("TELEGRAM_ALLOW_FROM")
    if isinstance(allow_from, str):
        text = allow_from.strip()
        if text.startswith("["):
            normalized["TELEGRAM_ALLOW_FROM"] = json.loads(text)
        else:
            items = [item.strip() for item in text.split(",") if item.strip()]
            normalized["TELEGRAM_ALLOW_FROM"] = [int(item) if item.isdigit() else item for item in items]
    cdp_url = str(normalized.get("BROWSER_CDP_URL") or "").strip().rstrip("/")
    parsed = urlparse(cdp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(
            "OPENCLAW_BROWSER_CDP_URL must be an absolute http(s) URL, "
            "for example http://127.0.0.1:9222 or http://browser:9222."
        )
    normalized["BROWSER_CDP_URL"] = cdp_url
    return normalized


def validate_gateway_token(values: Dict[str, Any], *, allow_missing: bool) -> None:
    """Refuse placeholder gateway credentials before rendering a live config.

    The browser gateway is a privileged local control plane.  A placeholder
    token is functionally equivalent to no authentication, so it must never be
    accepted for a normal bootstrap.  ``--allow-missing-secrets`` remains only
    for non-running template inspection.
    """
    token = str(values.get("GATEWAY_TOKEN") or "").strip()
    unsafe = not token or token.casefold() in {"change-me", "change_me"} or "change_me" in token.casefold()
    if unsafe and not allow_missing:
        raise SystemExit(
            "Set OPENCLAW_GATEWAY_TOKEN (or GATEWAY_TOKEN) to a real random "
            "value before bootstrapping. Do not use CHANGE_ME."
        )


def disable_unconfigured_optional_features(config: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Disable optional channels instead of emitting a config with fake secrets.

    This makes the browser-only setup runnable with a gateway token alone.
    Adding a real secret and re-running bootstrap explicitly enables the
    matching integration; no optional channel starts by accident.
    """
    # Web search is now owned entirely by the Google plugin.  Keeping an old
    # tools.web.search block makes current OpenClaw reject the configuration.
    google_enabled = bool(str(values.get("GOOGLE_WEBSEARCH_API_KEY") or "").strip())
    if google_enabled:
        config["plugins"]["entries"]["google"]["enabled"] = True
    else:
        # Omit an unconfigured provider rather than leaving a disabled plugin
        # with secret-shaped config, which current OpenClaw warns about.
        config["plugins"]["entries"].pop("google", None)

    telegram_enabled = bool(str(values.get("TELEGRAM_BOT_TOKEN") or "").strip()) and bool(
        values.get("TELEGRAM_ALLOW_FROM")
    )
    config["channels"]["telegram"]["enabled"] = telegram_enabled
    config["plugins"]["entries"]["telegram"]["enabled"] = telegram_enabled

    gmail_fields = ("HOOKS_TOKEN", "GMAIL_ACCOUNT", "GMAIL_TOPIC", "GMAIL_SUBSCRIPTION", "GMAIL_PUSH_TOKEN")
    gmail_enabled = all(bool(str(values.get(key) or "").strip()) for key in gmail_fields)
    config["hooks"]["enabled"] = gmail_enabled
    if not gmail_enabled:
        config["hooks"]["mappings"] = []
    return config


def resolve(node: Any, values: Dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: resolve(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve(item, values) for item in node]
    if isinstance(node, str):
        if node in PLACEHOLDERS:
            return PLACEHOLDERS[node](values)
        if node.startswith("__") and node.endswith("__"):
            key = node[2:-2]
            if key in values:
                return values[key]
            raise KeyError(key)
    return node


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def copy_workspace_files(source_dir: Path, target_dir: Path) -> None:
    """Copy tracked workspace instructions without copying agent state or secrets."""
    if not source_dir.exists():
        return
    for source_file in source_dir.iterdir():
        if source_file.is_file():
            shutil.copy2(source_file, target_dir / source_file.name)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_existing(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    if path.is_dir():
        shutil.copytree(path, backup)
    else:
        shutil.copy2(path, backup)


def render_template(template_path: Path, values: Dict[str, Any], allow_missing: bool) -> Any:
    template = load_json(template_path)
    values = {**OPTIONAL_SECRET_DEFAULTS, **values}
    missing: list[str] = []

    def resolve_with_tracking(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: resolve_with_tracking(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve_with_tracking(item) for item in node]
        if isinstance(node, str):
            if node in PLACEHOLDERS:
                return PLACEHOLDERS[node](values)
            if node.startswith("__") and node.endswith("__"):
                key = node[2:-2]
                if key in values:
                    return values[key]
                missing.append(key)
                return node if allow_missing else None
        return node

    rendered = resolve_with_tracking(template)
    if missing and not allow_missing:
        unique = ", ".join(sorted(set(missing)))
        raise SystemExit(
            f"Missing OpenClaw secrets for template placeholders: {unique}\n"
            "Provide them via --secrets-file or OPENCLAW_* environment variables."
        )
    return disable_unconfigured_optional_features(rendered, values)


def bootstrap(target_home: Path, template_path: Path, values: Dict[str, Any], allow_missing: bool,
              force: bool) -> None:
    """Render an isolated OpenClaw home with an explicit browser endpoint.

    The generated configuration uses a gateway token, separate agent
    workspaces, and only optional integrations whose real credentials exist.
    """
    values = normalize_values({**OPTIONAL_SECRET_DEFAULTS, **MODEL_DEFAULTS, **values})
    validate_gateway_token(values, allow_missing=allow_missing)
    openclaw_home = target_home / ".openclaw"
    if openclaw_home.exists() and not force:
        raise SystemExit(
            f"{openclaw_home} already exists. Re-run with --force to refresh it."
        )
    if openclaw_home.exists() and force:
        for filename in ("openclaw.json", "openclaw.json.last-good"):
            backup_existing(openclaw_home / filename)

    ensure_dir(openclaw_home)
    for name in ("agents", "skills", "logs", "memory", "state", "subagents", "tmp", "workspace-main",
                 "workspace-resume", "workspace-cover_letter", "workspace-repo_coordinator",
                 "workspace-linkedin_discovery", "workspace"):
        ensure_dir(openclaw_home / name)
    # The "resume" and "cover_letter" agent profiles in openclaw.template.json
    # declare an explicit agentDir (~/.openclaw/agents/<id>/agent). Without
    # these existing, those two agent profiles can fail to start even though
    # the config JSON itself renders fine -- create them up front so
    # `openclaw agent --agent resume` / `--agent cover_letter` have
    # somewhere to write their agent-specific state.
    for agent_id in ("resume", "cover_letter", "repo_coordinator", "linkedin_discovery"):
        ensure_dir(openclaw_home / "agents" / agent_id / "agent")

    config = render_template(template_path, values, allow_missing=allow_missing)
    write_json(openclaw_home / "openclaw.json", config)
    write_json(openclaw_home / "openclaw.json.last-good", config)

    workspace_seed = template_path.parent / "workspace"
    profile_seed_root = template_path.parent / "workspace-profiles"
    workspace_targets = {
        "workspace": workspace_seed,
        "workspace-main": workspace_seed,
        "workspace-resume": workspace_seed,
        "workspace-cover_letter": workspace_seed,
        "workspace-repo_coordinator": workspace_seed,
        "workspace-linkedin_discovery": workspace_seed,
    }
    for target_name, source_dir in workspace_targets.items():
        target_dir = openclaw_home / target_name
        copy_workspace_files(source_dir, target_dir)
        # Each named agent receives a small role overlay after the shared
        # safety/tool policy. This keeps its operating instructions visible in
        # its own workspace rather than hidden in a central coordinator prompt.
        profile_name = target_name.removeprefix("workspace-")
        copy_workspace_files(profile_seed_root / profile_name, target_dir)

    print(f"Bootstrapped OpenClaw under {openclaw_home}")


def export_bundle(source_home: Path, bundle_path: Path) -> None:
    openclaw_home = source_home / ".openclaw"
    if not openclaw_home.exists():
        raise SystemExit(f"OpenClaw home not found: {openclaw_home}")
    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(openclaw_home, arcname=".openclaw")
    print(f"Exported {bundle_path}")


def _safe_extract(tar: tarfile.TarFile, path: Path) -> None:
    base = path.resolve()
    for member in tar.getmembers():
        member_path = (path / member.name).resolve()
        if not str(member_path).startswith(str(base)):
            raise SystemExit(f"Unsafe path in bundle: {member.name}")
    tar.extractall(path=path)


def import_bundle(target_home: Path, bundle_path: Path, force: bool) -> None:
    openclaw_home = target_home / ".openclaw"
    if openclaw_home.exists():
        if not force:
            raise SystemExit(
                f"{openclaw_home} already exists. Re-run with --force to replace it."
            )
        backup_existing(openclaw_home / "openclaw.json")
        backup_existing(openclaw_home / "openclaw.json.last-good")
        shutil.rmtree(openclaw_home)
    ensure_dir(target_home)
    with tarfile.open(bundle_path, "r:*") as tar:
        _safe_extract(tar, target_home)
    print(f"Imported {bundle_path} into {openclaw_home}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or export OpenClaw config safely.")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = sub.add_parser("bootstrap", help="Render ~/.openclaw from tracked templates.")
    bootstrap_parser.add_argument("--target-home", default=str(Path.home()))
    bootstrap_parser.add_argument("--template", default=str(repo_root() / "bootstrap/openclaw/openclaw.template.json"))
    bootstrap_parser.add_argument("--secrets-file")
    bootstrap_parser.add_argument("--allow-missing-secrets", action="store_true")
    bootstrap_parser.add_argument("--force", action="store_true")

    export_parser = sub.add_parser("export", help="Bundle an existing ~/.openclaw into a tarball.")
    export_parser.add_argument("--source-home", default=str(Path.home()))
    export_parser.add_argument("--bundle", required=True)

    import_parser = sub.add_parser("import", help="Restore ~/.openclaw from a tarball bundle.")
    import_parser.add_argument("--target-home", default=str(Path.home()))
    import_parser.add_argument("--bundle", required=True)
    import_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if args.command == "bootstrap":
        values = load_env_values()
        secrets = load_secrets(Path(args.secrets_file)) if args.secrets_file else {}
        values.update(secrets)
        values = normalize_values(values)
        bootstrap(
            target_home=Path(args.target_home).expanduser(),
            template_path=Path(args.template),
            values=values,
            allow_missing=args.allow_missing_secrets,
            force=args.force,
        )
        return 0

    if args.command == "import":
        import_bundle(
            target_home=Path(args.target_home).expanduser(),
            bundle_path=Path(args.bundle).expanduser(),
            force=args.force,
        )
        return 0

    export_bundle(
        source_home=Path(args.source_home).expanduser(),
        bundle_path=Path(args.bundle).expanduser(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
