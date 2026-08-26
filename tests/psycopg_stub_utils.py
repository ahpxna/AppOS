"""Collection-safe optional psycopg stub for pure unit modules.

Tests that need to import a module with an unconditional psycopg import may use
this only when psycopg is absent, then immediately restore sys.modules after the
target module is loaded. This prevents collection order from leaking a fake
Jsonb implementation into unrelated tests.
"""
from __future__ import annotations

import sys
import types
from typing import Any

_NAMES = ("psycopg", "psycopg.types", "psycopg.types.json")
_MISSING = object()


def install_if_missing() -> dict[str, Any] | None:
    try:
        from psycopg.types.json import Jsonb  # noqa: F401
        return None
    except ModuleNotFoundError:
        saved = {name: sys.modules.get(name, _MISSING) for name in _NAMES}
        psycopg = types.ModuleType("psycopg")
        psycopg.connect = lambda *_a, **_k: None
        psycopg.Error = Exception
        psycopg_types = types.ModuleType("psycopg.types")
        psycopg_json = types.ModuleType("psycopg.types.json")

        class Jsonb:
            def __init__(self, value):
                self.obj = value

        psycopg_json.Jsonb = Jsonb
        sys.modules["psycopg"] = psycopg
        sys.modules["psycopg.types"] = psycopg_types
        sys.modules["psycopg.types.json"] = psycopg_json
        return saved


def restore(saved: dict[str, Any] | None) -> None:
    if saved is None:
        return
    for name, value in saved.items():
        if value is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value
