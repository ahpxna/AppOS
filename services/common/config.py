"""Dependency-free, fail-closed configuration for JobOS executables."""
from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    """Raised when an operational service lacks required local configuration."""


def _parse_dotenv_value(raw: str) -> str:
    """Parse JobOS ``KEY=VALUE`` lines without mangling valid secrets.

    One matching quote pair is syntax; quote/backslash characters inside the
    value remain data according to the quote mode. A quoted value may be
    followed only by whitespace and an optional ``#`` comment. Unquoted values
    use `` #`` as the conservative inline-comment delimiter.
    """
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        close = None
        for index in range(1, len(value)):
            ch = value[index]
            if quote == '"' and ch == "\\" and not escaped:
                escaped = True
                continue
            if ch == quote and not escaped:
                close = index
                break
            escaped = False
        if close is None:
            raise ConfigurationError("Malformed .env value: quoted value is not terminated.")
        trailer = value[close + 1:].strip()
        if trailer and not trailer.startswith("#"):
            raise ConfigurationError("Malformed .env value: unexpected data after quoted value.")
        body = value[1:close]
        if quote == "'":
            return body
        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append(escapes.get(nxt, "\\" + nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    marker = value.find(" #")
    if marker >= 0:
        value = value[:marker]
    return value.rstrip()


def load_repo_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, _parse_dotenv_value(raw_value))


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Read a bounded integer env setting without turning a typo into a node crash."""
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """Read a bounded float env setting with the same resilient config contract."""
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def require_env(name: str) -> str:
    """Read one required setting without changing its credential bytes."""
    load_repo_env()
    value = os.getenv(name) or ""
    checked = value.strip()
    if not checked or checked.startswith("CHANGE_ME"):
        raise ConfigurationError(
            f"{name} is missing. Run bash scripts/bootstrap_ubuntu_24.sh first, "
            "then configure the untracked .env file."
        )
    return value


def _conninfo_quote(value: object) -> str:
    """Quote one libpq conninfo value so whitespace/quotes cannot change fields."""
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def database_dsn() -> str:
    load_repo_env()
    password = require_env("JOBOS_DB_PASSWORD") if os.getenv("JOBOS_DB_PASSWORD") else require_env("POSTGRES_PASSWORD")
    fields = {
        "host": os.getenv("JOBOS_DB_HOST", "127.0.0.1"),
        "port": os.getenv("JOBOS_DB_PORT", os.getenv("POSTGRES_HOST_PORT", "5433")),
        "dbname": os.getenv("JOBOS_DB_NAME", "job_apply_os"),
        "user": os.getenv("JOBOS_DB_USER", os.getenv("POSTGRES_USER", "jobos")),
        "password": password,
    }
    return " ".join(f"{key}={_conninfo_quote(value)}" for key, value in fields.items())
