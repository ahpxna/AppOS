"""Compatibility import for the managed-only OpenClaw runtime policy."""
from __future__ import annotations

from services.runtime.openclaw_runtime import (
    MANAGED_OPENCLAW as PRIVATE_OPENCLAW_BIN,
    GlobalOpenClawForbiddenError,
    ManagedOpenClawMissingError,
    find_global_openclaw_conflicts,
    inspect_global_openclaw_install,
    remove_proven_global_openclaw,
    resolve_managed_openclaw,
)


def resolve_openclaw_binary(*, required: bool = False) -> str:
    """Return only the managed runtime; global/PATH fallback is forbidden.

    The compatibility non-required result remains a deterministic managed path
    string so old lazy callers can display an actionable missing-runtime error
    rather than accidentally executing a shell PATH binary.
    """
    resolved = resolve_managed_openclaw(required=required)
    return str(resolved or PRIVATE_OPENCLAW_BIN)
