"""Narrow OpenClaw browser transport; it never receives a profile or prompt."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

from services.autofill.autofill_planner_v1 import PlannedAction


class AutofillTransport(Protocol):
    def current_url(self) -> str: ...
    def snapshot(self) -> dict[str, Any]: ...
    def execute(self, command: dict[str, str]) -> None: ...


class TransportError(RuntimeError):
    pass


class OpenClawTransport:
    """Direct adapter for one attached OpenClaw browser profile only."""
    def __init__(self, *, binary: str = "openclaw", profile: str = "remote", timeout: int = 60,
                 environment: dict[str, str] | None = None):
        self.binary, self.profile, self.timeout = binary, profile, timeout
        self.environment = environment or dict(os.environ)

    def _run(self, args: list[str], *, json_output: bool = False) -> str:
        if shutil.which(self.binary) is None:
            raise TransportError(f"OpenClaw binary not found: {self.binary}")
        command = [self.binary, "browser", "--browser-profile", self.profile, *args]
        if json_output:
            command.append("--json")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, env=self.environment)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"OpenClaw browser command timed out: {args[0]}") from exc
        if result.returncode != 0:
            raise TransportError((result.stderr or result.stdout or "OpenClaw browser failure").strip()[:500])
        return result.stdout

    @staticmethod
    def _json(output: str) -> Any:
        starts = [index for index in (output.find("{"), output.find("[")) if index >= 0]
        if not starts:
            raise TransportError("OpenClaw browser command did not return JSON.")
        first = min(starts)
        last = max(output.rfind("}"), output.rfind("]"))
        if last <= first:
            raise TransportError("OpenClaw browser JSON was incomplete.")
        try:
            data = json.loads(output[first:last + 1])
        except json.JSONDecodeError as exc:
            raise TransportError("OpenClaw browser returned malformed JSON.") from exc
        return data

    def snapshot(self) -> dict[str, Any]:
        data = self._json(self._run(["snapshot", "--efficient"], json_output=True))
        if not isinstance(data, dict):
            raise TransportError("OpenClaw snapshot JSON is not an object.")
        return data

    def current_url(self) -> str:
        data = self._json(self._run(["tabs"], json_output=True))
        nested = data.get("data") if isinstance(data, dict) else None
        tabs = data if isinstance(data, list) else (
            data.get("tabs") or data.get("pages") or
            (nested.get("tabs") if isinstance(nested, dict) else []) or []
        )
        if not isinstance(tabs, list):
            raise TransportError("OpenClaw did not provide a tab list.")
        active = [
            tab for tab in tabs if isinstance(tab, dict)
            and any(tab.get(key) is True for key in ("active", "focused", "selected"))
        ]
        candidates = active or (tabs if len(tabs) == 1 else [])
        if len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise TransportError("Cannot identify exactly one focused browser tab.")
        url = str(candidates[0].get("url") or "")
        if not url.startswith(("https://", "http://")):
            raise TransportError("Focused tab has no HTTP(S) URL.")
        return url

    def execute(self, command: dict[str, str]) -> None:
        action, target, value = command.get("action"), command.get("target"), command.get("value")
        if not action or not target or value is None:
            raise TransportError("Malformed deterministic browser command.")
        if action == "fill":
            self._run(["fill", target, value])
        elif action == "select":
            self._run(["select", target, value])
        elif action == "check":
            self._run(["click", target])
        elif action == "upload":
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise TransportError(f"Approved upload artifact is missing: {path}")
            self._run(["upload", target, str(path)])
        else:
            raise TransportError(f"Unsupported deterministic browser action: {action}")


def _narrow_commands(actions: list[PlannedAction]) -> list[dict[str, str]]:
    """Internal helper; execution is only authorized through AutofillSession."""
    return [
        {"action": item.action, "target": item.ref, "value": item.value}
        for item in actions
        if item.action in {"fill", "select", "check", "upload"} and item.value is not None
    ]
