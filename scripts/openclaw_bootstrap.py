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


PLACEHOLDERS = {
    "__NOW_ISO__": lambda _: datetime.now(timezone.utc).isoformat(),
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
    return normalized


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
    return rendered


def bootstrap(target_home: Path, template_path: Path, values: Dict[str, Any], allow_missing: bool,
              force: bool) -> None:
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
                 "workspace-resume", "workspace-cover_letter", "workspace"):
        ensure_dir(openclaw_home / name)

    config = render_template(template_path, values, allow_missing=allow_missing)
    write_json(openclaw_home / "openclaw.json", config)
    write_json(openclaw_home / "openclaw.json.last-good", config)

    workspace_seed = template_path.parent / "workspace"
    workspace_targets = {
        "workspace": workspace_seed,
        "workspace-main": workspace_seed,
        "workspace-resume": workspace_seed,
        "workspace-cover_letter": workspace_seed,
    }
    for target_name, source_dir in workspace_targets.items():
        target_dir = openclaw_home / target_name
        for source_file in source_dir.iterdir():
            if source_file.is_file():
                shutil.copy2(source_file, target_dir / source_file.name)

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
