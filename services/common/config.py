"""Dependency-free, fail-closed configuration for JobOS executables."""
from __future__ import annotations
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    """Raised when an operational service lacks required local configuration."""


def load_repo_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    """Read one required setting after safely loading the repository .env."""
    load_repo_env()
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith("CHANGE_ME"):
        raise ConfigurationError(
            f"{name} is missing. Run bash scripts/bootstrap_ubuntu_24.sh first, "
            "then configure the untracked .env file."
        )
    return value


def database_dsn() -> str:
    load_repo_env()
    password = require_env("JOBOS_DB_PASSWORD") if os.getenv("JOBOS_DB_PASSWORD") else require_env("POSTGRES_PASSWORD")
    return " ".join((f"host={os.getenv('JOBOS_DB_HOST', '127.0.0.1')}",
                     f"port={os.getenv('JOBOS_DB_PORT', os.getenv('POSTGRES_HOST_PORT', '5433'))}",
                     f"dbname={os.getenv('JOBOS_DB_NAME', 'job_apply_os')}",
                     f"user={os.getenv('JOBOS_DB_USER', os.getenv('POSTGRES_USER', 'jobos'))}",
                     f"password={password}"))
