"""Managed-only OpenClaw runtime policy.

JobOS executes OpenClaw only from ``data/openclaw-runtime``.  Doctor may
*inspect* global installations and, after an explicit interactive confirmation,
remove only installations whose package-manager provenance can be proven.
Unknown standalone binaries are never deleted automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MANAGED_ROOT = REPO_ROOT / "data" / "openclaw-runtime"
MANAGED_OPENCLAW = MANAGED_ROOT / "node" / "node_modules" / ".bin" / "openclaw"


class ManagedOpenClawMissingError(RuntimeError):
    pass


class GlobalOpenClawForbiddenError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedOpenClawRuntime:
    root: Path
    openclaw: Path

    def validate(self) -> None:
        root, binary = self.root.resolve(), self.openclaw.resolve(strict=False)
        try:
            binary.relative_to(root)
        except ValueError as exc:
            raise GlobalOpenClawForbiddenError("managed OpenClaw path escapes JobOS runtime root") from exc
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ManagedOpenClawMissingError(
                "Managed OpenClaw runtime is unavailable. Run "
                "python scripts/setup_openclaw_jobos.py --mode native --install-runtime --force "
                "--generate-gateway-token."
            )


@dataclass(frozen=True)
class GlobalOpenClawInstall:
    path: Path
    provenance: str
    removal_command: tuple[str, ...] | None = None

    @property
    def removable(self) -> bool:
        return bool(self.removal_command)


def managed_runtime() -> ManagedOpenClawRuntime:
    return ManagedOpenClawRuntime(MANAGED_ROOT, MANAGED_OPENCLAW)


def resolve_managed_openclaw(*, required: bool = True) -> Path | None:
    runtime = managed_runtime()
    explicit = os.getenv("OPENCLAW_BIN", "").strip()
    if explicit:
        supplied = Path(explicit).expanduser().resolve(strict=False)
        expected = runtime.openclaw.resolve(strict=False)
        if supplied != expected:
            raise GlobalOpenClawForbiddenError(
                "OPENCLAW_BIN may only name JobOS's managed runtime; global/PATH overrides are forbidden."
            )
    try:
        runtime.validate()
    except ManagedOpenClawMissingError:
        if required:
            raise
        return None
    return runtime.openclaw.resolve()


def _candidate_global_paths() -> list[Path]:
    """Return every executable named openclaw visible in global/PATH locations.

    ``shutil.which`` only reports the first PATH hit and can therefore hide a
    second global install behind the managed runtime.  Scan every PATH entry
    plus conventional package-manager bin roots, de-duplicating by lexical
    path while preserving symlinks for provenance inspection.
    """
    candidates: list[Path] = []
    seen: set[str] = set()
    path_dirs = [Path(p).expanduser() for p in os.getenv("PATH", "").split(os.pathsep) if p]
    path_dirs.extend(Path(p) for p in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin"))
    for directory in path_dirs:
        candidate = directory / "openclaw"
        key = str(candidate.absolute())
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                candidates.append(candidate)
        except OSError:
            continue
    return candidates


def find_global_openclaw_conflicts() -> list[Path]:
    """Return all non-managed OpenClaw executables without selecting them."""
    managed_lexical = MANAGED_OPENCLAW.absolute()
    managed_resolved = MANAGED_OPENCLAW.resolve(strict=False)
    conflicts: list[Path] = []
    for candidate in _candidate_global_paths():
        try:
            if candidate.absolute() == managed_lexical or candidate.resolve(strict=False) == managed_resolved:
                continue
        except OSError:
            pass
        conflicts.append(candidate.absolute())
    return conflicts


def _npm_provenance(path: Path) -> GlobalOpenClawInstall | None:
    """Recognize a global npm OpenClaw shim only when its target proves origin."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    parts = resolved.parts
    # Typical Unix npm global target: <prefix>/lib/node_modules/openclaw/...
    marker = ("lib", "node_modules", "openclaw")
    for idx in range(0, max(0, len(parts) - len(marker) + 1)):
        if tuple(parts[idx:idx + len(marker)]) != marker:
            continue
        prefix = Path(*parts[:idx]) if idx else Path("/")
        npm_candidates = [prefix / "bin" / "npm"]
        global_npm = shutil.which("npm")
        if global_npm:
            npm_candidates.append(Path(global_npm))
        for npm in npm_candidates:
            if not npm.is_file() or not os.access(npm, os.X_OK):
                continue
            try:
                probe = subprocess.run(
                    [str(npm), "root", "--global", "--prefix", str(prefix)],
                    text=True, capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode != 0:
                continue
            root = Path(probe.stdout.strip()).resolve(strict=False)
            expected = (prefix / "lib" / "node_modules").resolve(strict=False)
            if root != expected or not (root / "openclaw").exists():
                continue
            return GlobalOpenClawInstall(
                path=path,
                provenance=f"npm global prefix {prefix}",
                removal_command=(str(npm), "uninstall", "--global", "--prefix", str(prefix), "openclaw"),
            )
    return None


def _brew_provenance(path: Path) -> GlobalOpenClawInstall | None:
    brew = shutil.which("brew")
    if not brew:
        return None
    try:
        resolved = path.resolve(strict=True)
        prefix_probe = subprocess.run([brew, "--prefix"], text=True, capture_output=True, timeout=10, check=False)
        list_probe = subprocess.run([brew, "list", "--versions", "openclaw"], text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if prefix_probe.returncode != 0 or list_probe.returncode != 0 or not list_probe.stdout.strip():
        return None
    prefix = Path(prefix_probe.stdout.strip()).resolve(strict=False)
    try:
        resolved.relative_to(prefix)
    except ValueError:
        return None
    if "Cellar" not in resolved.parts and "opt" not in resolved.parts:
        return None
    return GlobalOpenClawInstall(
        path=path,
        provenance=f"Homebrew prefix {prefix}",
        removal_command=(brew, "uninstall", "openclaw"),
    )


def inspect_global_openclaw_install(path: Path) -> GlobalOpenClawInstall:
    """Classify one conflict without ever assuming an unknown file is safe to delete."""
    candidate = Path(path).absolute()
    return (
        _npm_provenance(candidate)
        or _brew_provenance(candidate)
        or GlobalOpenClawInstall(candidate, "unknown standalone/global binary", None)
    )


def remove_proven_global_openclaw(path: Path, *, timeout_s: int = 120) -> GlobalOpenClawInstall:
    """Remove one package-manager-proven install; refuse unknown standalone files."""
    install = inspect_global_openclaw_install(path)
    if not install.removal_command:
        raise GlobalOpenClawForbiddenError(
            f"Refusing to delete {install.path}: package-manager provenance could not be proven. "
            "Remove it manually, then rerun doctor."
        )
    try:
        result = subprocess.run(
            list(install.removal_command), text=True, capture_output=True,
            timeout=max(1, int(timeout_s)), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GlobalOpenClawForbiddenError(
            f"Timed out while removing {install.path} via {install.provenance}."
        ) from exc
    except OSError as exc:
        raise GlobalOpenClawForbiddenError(
            f"Could not execute package-manager removal for {install.path}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown package-manager error").strip()[:500]
        raise GlobalOpenClawForbiddenError(
            f"Failed to remove {install.path} via {install.provenance}: {detail}"
        )
    # Package managers may leave a stale shim.  Treat that as failure rather
    # than deleting the file ourselves and losing provenance safety.
    if install.path.exists():
        raise GlobalOpenClawForbiddenError(
            f"Package manager reported success but {install.path} still exists; refusing blind deletion."
        )
    return install
