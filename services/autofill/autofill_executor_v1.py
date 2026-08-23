"""Least-privilege executor contracts for OpenClaw, Playwright, or CDP."""
from __future__ import annotations

from typing import Protocol

from services.autofill.autofill_planner_v1 import PlannedAction


class AutofillTransport(Protocol):
    def execute(self, command: dict[str, str]) -> None: ...


def narrow_commands(actions: list[PlannedAction]) -> list[dict[str, str]]:
    """Expose only one needed value per action; never pass a whole profile."""
    commands: list[dict[str, str]] = []
    for item in actions:
        if item.action not in {"fill", "select", "check", "upload"} or item.value is None:
            continue
        commands.append({"action": item.action, "target": item.ref, "value": item.value})
    return commands


def execute_actions(transport: AutofillTransport, actions: list[PlannedAction]) -> list[dict[str, str]]:
    commands = narrow_commands(actions)
    for command in commands:
        transport.execute(command)
    return commands
