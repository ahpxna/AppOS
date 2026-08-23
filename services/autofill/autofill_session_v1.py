"""Capability-gated, one-action-at-a-time deterministic form session."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from services.autofill.autofill_executor_v1 import AutofillTransport, _narrow_commands
from services.autofill.autofill_planner_v1 import PlannedAction
from services.autofill.form_inspector_v1 import FormField, QuestionGroup


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotState:
    fields: tuple[FormField, ...]
    groups: tuple[QuestionGroup, ...]


@dataclass(frozen=True)
class SessionResult:
    status: str
    verified_refs: tuple[str, ...]
    failed_refs: tuple[str, ...]
    executed_refs: tuple[str, ...]


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SessionError("Browser no longer has a valid HTTP(S) origin.")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _filename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


class AutofillSession:
    """Enforces origin and verification around every allowed side effect."""
    def __init__(self, *, transport: AutofillTransport, expected_origin: str,
                 snapshot_state: Callable[[], SnapshotState], origin_allowed: Callable[[str], None]):
        self.transport = transport
        self.expected_origin = _origin(expected_origin)
        self.snapshot_state = snapshot_state
        self.origin_allowed = origin_allowed

    def _assert_origin(self) -> None:
        current = self.transport.current_url()
        self.origin_allowed(current)
        if _origin(current) != self.expected_origin:
            raise SessionError(f"Focused browser origin changed to {_origin(current)}; expected {self.expected_origin}.")

    @staticmethod
    def _verified(action: PlannedAction, state: SnapshotState) -> bool:
        if action.action == "check":
            return any(option.ref == action.ref and option.selected is True
                       for group in state.groups for option in group.options)
        field = next((item for item in state.fields if item.ref == action.ref), None)
        if field is None or action.value is None:
            return False
        if action.action == "upload":
            return _filename(field.value) == _filename(action.value)
        return field.value == action.value

    def execute(self, actions: list[PlannedAction], *, on_first_verified_write: Callable[[], None]) -> SessionResult:
        self._assert_origin()
        verified: list[str] = []
        failed: list[str] = []
        executed: list[str] = []
        consumed = False
        for command in _narrow_commands(actions):
            self._assert_origin()
            self.transport.execute(command)
            executed.append(command["target"])
            self._assert_origin()
            original = next(item for item in actions if item.ref == command["target"] and item.action == command["action"])
            if self._verified(original, self.snapshot_state()):
                verified.append(command["target"])
                if not consumed:
                    on_first_verified_write()
                    consumed = True
            else:
                failed.append(command["target"])
                break
        pauses = any(item.action == "pause" for item in actions)
        status = "partial" if failed else ("needs_review" if pauses else "completed")
        return SessionResult(status, tuple(verified), tuple(failed), tuple(executed))
