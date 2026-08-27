"""Test-environment bootstrap for optional PostgreSQL integration dependencies.

Pure regression tests intentionally run without PostgreSQL.  When psycopg is
not installed and the destructive DB integration gate is *not* requested, a
minimal import-only stub keeps collection honest without pretending a database
exists.  Setting ``JOBOS_RUN_DB_INTEGRATION=1`` disables the stub so a missing
real driver fails immediately instead of hiding a broken integration setup.
"""
from __future__ import annotations

import os
import sys
import types


def _install_import_only_psycopg_stub() -> None:
    # Import-time DSN construction is still present in legacy modules.  Pure
    # tests get a non-secret placeholder so collection can proceed, while the
    # psycopg stub below guarantees an accidental DB connection still fails.
    if os.getenv("JOBOS_RUN_DB_INTEGRATION") != "1":
        os.environ.setdefault("POSTGRES_PASSWORD", "jobos-test-placeholder")

    try:
        import psycopg  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if os.getenv("JOBOS_RUN_DB_INTEGRATION") == "1":
        return

    psycopg = types.ModuleType("psycopg")
    psycopg.Error = Exception

    def _no_database(*_args, **_kwargs):
        raise RuntimeError(
            "psycopg import-only test stub cannot connect to PostgreSQL; "
            "install requirements and enable JOBOS_RUN_DB_INTEGRATION for DB tests"
        )

    psycopg.connect = _no_database
    psycopg_types = types.ModuleType("psycopg.types")
    psycopg_json = types.ModuleType("psycopg.types.json")
    psycopg_sql = types.ModuleType("psycopg.sql")

    class Jsonb:
        def __init__(self, value):
            self.obj = value

    class SQL(str):
        def format(self, *args, **kwargs):
            return SQL(super().format(*args, **kwargs))

        def join(self, values):
            return SQL(str(self).join(str(value) for value in values))

    psycopg_json.Jsonb = Jsonb
    psycopg_sql.SQL = SQL
    psycopg_sql.Identifier = lambda value: SQL(str(value))
    psycopg_sql.Literal = lambda value: SQL(repr(value))
    psycopg.sql = psycopg_sql
    sys.modules.update({
        "psycopg": psycopg,
        "psycopg.types": psycopg_types,
        "psycopg.types.json": psycopg_json,
        "psycopg.sql": psycopg_sql,
    })


_install_import_only_psycopg_stub()
