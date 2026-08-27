"""Small deterministic coercers for external/LLM JSON boundaries.

Do not use Python truthiness directly on model/API scalar values: for example,
``bool("false")`` is ``True``.  These helpers accept only explicit, familiar
representations and otherwise return the caller's conservative default.
"""
from __future__ import annotations

from typing import Any

_TRUE_STRINGS = {"1", "true", "yes", "y", "on"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", "", "none", "null"}


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Return a strict boolean for common JSON/API scalar representations."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return default
