"""Managed-only OpenClaw runtime policy.

JobOS may inspect a global binary for doctor diagnostics, but it never selects
or installs one as a browser/agent runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
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


def find_global_openclaw_conflicts() -> list[Path]:
    """Report, but never select, PATH/global OpenClaw binaries."""
    candidate = shutil.which("openclaw")
    if not candidate:
        return []
    path = Path(candidate).resolve(strict=False)
    managed = MANAGED_OPENCLAW.resolve(strict=False)
    return [] if path == managed else [path]
