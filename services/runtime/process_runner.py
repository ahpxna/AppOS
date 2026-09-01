"""One bounded, typed boundary for child-process execution.

Feature modules receive a ``ProcessResult`` instead of catching a mixture of
``TimeoutExpired``, OS errors and ad-hoc stderr strings.  Daemon supervision
uses ``Popen`` separately; this module is for finite commands only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Mapping, Sequence


_TRANSIENT_MARKERS = (
    "connection refused", "connection reset", "temporarily unavailable",
    "temporary failure", "timed out", "timeout", "rate limit", "429",
    "502", "503", "504", "urlerror",
    "jobos_retryable_block:", "github api unavailable",
    "profile facts require review",
)


@dataclass(frozen=True)
class ProcessResult:
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    transient: bool
    elapsed_seconds: float
    start_error: str | None = None

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _bounded(value: str | bytes | None, *, limit: int = 16_000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[output truncated]"


def _is_transient(text: str) -> bool:
    return any(marker in text.casefold() for marker in _TRANSIENT_MARKERS)


class ProcessRunner:
    """Run one finite argv vector without a shell and normalize failures."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> ProcessResult:
        if not argv:
            raise ValueError("ProcessRunner requires a non-empty argv")
        timeout = max(0.1, float(timeout_s))
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv), cwd=cwd, env=dict(env) if env is not None else None,
                input=input_text, capture_output=True, text=True, timeout=timeout,
                shell=False, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                False, None, _bounded(exc.stdout), _bounded(exc.stderr), True, True,
                time.monotonic() - started, f"timed out after {timeout:g}s",
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ProcessResult(
                False, None, "", "", False, False,
                time.monotonic() - started, f"{type(exc).__name__}: {exc}",
            )
        stdout, stderr = _bounded(completed.stdout), _bounded(completed.stderr)
        output = stdout + stderr
        return ProcessResult(
            completed.returncode == 0, completed.returncode, stdout, stderr,
            False, completed.returncode != 0 and _is_transient(output),
            time.monotonic() - started,
        )


DEFAULT_PROCESS_RUNNER = ProcessRunner()
