#!/usr/bin/env python3
"""Install JobOS's pinned private Node + OpenClaw runtime.

This is intentionally separate from config rendering: it is the only setup
step that downloads executable code.  It installs beneath the ignored
``data/openclaw-runtime`` directory, never changes system Node/npm, and does
not start a gateway, browser, model, or agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "data" / "openclaw-runtime"
DEFAULT_NODE_VERSION = "24.15.0"
DEFAULT_OPENCLAW_VERSION = "2026.7.1-2"


class RuntimeInstallError(RuntimeError):
    pass


def platform_tag() -> tuple[str, str]:
    """Return the official Node archive suffix for the supported platforms."""
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not os_name or not arch:
        raise RuntimeInstallError(
            f"Unsupported platform {platform.system()} / {platform.machine()}. "
            "JobOS's managed OpenClaw installer supports Linux/macOS x64 and arm64."
        )
    return os_name, arch


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "jobos-runtime-installer/1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def expected_node_digest(version: str, archive_name: str, directory: Path) -> str:
    checksums = directory / "SHASUMS256.txt"
    download(f"https://nodejs.org/dist/v{version}/SHASUMS256.txt", checksums)
    pattern = re.compile(rf"^([0-9a-f]{{64}})\s+{re.escape(archive_name)}$", re.MULTILINE)
    match = pattern.search(checksums.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeInstallError(f"Node checksum was not published for {archive_name}.")
    return match.group(1)


def safe_extract(archive: Path, destination: Path) -> None:
    """Reject path traversal before extracting a trusted-but-downloaded tarball."""
    with tarfile.open(archive, "r:xz") as tar:
        root = destination.resolve()
        for member in tar.getmembers():
            candidate = (destination / member.name).resolve()
            if candidate != root and root not in candidate.parents:
                raise RuntimeInstallError("Refusing a Node archive with an unsafe member path.")
            if member.issym() or member.islnk():
                raise RuntimeInstallError("Refusing a Node archive containing links.")
        tar.extractall(destination)


def install_node(version: str, runtime_root: Path) -> Path:
    os_name, arch = platform_tag()
    archive_name = f"node-v{version}-{os_name}-{arch}.tar.xz"
    destination = runtime_root / f"node-runtime-{version}"
    node_bin = destination / "bin" / "node"
    if node_bin.is_file():
        return destination
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jobos-node-") as temp:
        temp_root = Path(temp)
        archive = temp_root / archive_name
        expected = expected_node_digest(version, archive_name, temp_root)
        download(f"https://nodejs.org/dist/v{version}/{archive_name}", archive)
        if sha256_file(archive) != expected:
            raise RuntimeInstallError("Node archive SHA-256 does not match nodejs.org SHASUMS256.txt.")
        safe_extract(archive, temp_root)
        extracted = temp_root / archive_name.removesuffix(".tar.xz")
        if not (extracted / "bin" / "node").is_file():
            raise RuntimeInstallError("Downloaded Node archive has no node binary.")
        if destination.exists():
            raise RuntimeInstallError(f"Incomplete runtime directory already exists: {destination}. Remove it deliberately and retry.")
        shutil.move(str(extracted), str(destination))
    return destination


def install_openclaw(node_root: Path, runtime_root: Path, version: str) -> Path:
    prefix = runtime_root / "node"
    binary = prefix / "node_modules" / ".bin" / "openclaw"
    if binary.is_file():
        return binary
    node_bin = node_root / "bin"
    npm = node_bin / "npm"
    if not npm.is_file():
        raise RuntimeInstallError("Managed Node runtime has no npm binary.")
    env = dict(os.environ)
    env["PATH"] = f"{node_bin}{os.pathsep}{env.get('PATH', '')}"
    env["npm_config_audit"] = "false"
    env["npm_config_fund"] = "false"
    command = [str(npm), "install", "--prefix", str(prefix), "--no-save", "--package-lock=false", "--omit=dev", f"openclaw@{version}"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not binary.is_file():
        detail = (result.stderr or result.stdout or "npm install failed").strip()[-800:]
        raise RuntimeInstallError(f"Pinned OpenClaw install failed: {detail}")
    return binary


def run_checked(command: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeInstallError((result.stderr or result.stdout or "runtime verification failed").strip()[-800:])
    return (result.stdout or result.stderr).strip()


def install(*, node_version: str, openclaw_version: str, runtime_root: Path) -> dict[str, str]:
    node_root = install_node(node_version, runtime_root)
    binary = install_openclaw(node_root, runtime_root, openclaw_version)
    env = dict(os.environ)
    env["PATH"] = f"{node_root / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    node_actual = run_checked([str(node_root / "bin" / "node"), "--version"], env)
    claw_actual = run_checked([str(binary), "--version"], env)
    if node_actual.lstrip("v") != node_version:
        raise RuntimeInstallError(f"Managed Node version mismatch: expected {node_version}, got {node_actual}.")
    if openclaw_version not in claw_actual:
        raise RuntimeInstallError(f"Managed OpenClaw version mismatch: expected {openclaw_version}, got {claw_actual}.")
    return {
        "runtime_root": str(runtime_root), "node": node_actual,
        "openclaw": claw_actual, "openclaw_binary": str(binary),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install pinned private Node + OpenClaw for JobOS.")
    parser.add_argument("--node-version", default=os.getenv("JOBOS_OPENCLAW_NODE_VERSION", DEFAULT_NODE_VERSION))
    parser.add_argument("--openclaw-version", default=os.getenv("JOBOS_OPENCLAW_VERSION", DEFAULT_OPENCLAW_VERSION))
    parser.add_argument("--runtime-root", type=Path, default=RUNTIME_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = install(node_version=args.node_version, openclaw_version=args.openclaw_version,
                         runtime_root=args.runtime_root.expanduser().resolve())
    except (RuntimeInstallError, OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        payload = {"status": "error", "error": str(exc)}
        print(json.dumps(payload) if args.json else f"ERROR: {payload['error']}")
        return 1
    payload = {"status": "ok", **result}
    print(json.dumps(payload, indent=2) if args.json else "\n".join(f"{key}: {value}" for key, value in payload.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
