"""Pinned, journaled session that rematches after each external side effect."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from services.autofill.autofill_executor_v1 import AutofillTransport, BrowserTarget
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
    target_id: str


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SessionError("Browser no longer has a valid HTTP(S) origin.")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _filename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def action_identity(action: PlannedAction) -> tuple[str, str | None, str | None, str | None]:
    return action.action, action.profile_key, action.question_label, action.value


class AutofillSession:
    """Replans from a fresh snapshot; old refs never survive an action."""
    def __init__(self, *, transport: AutofillTransport, expected_origin: str,
                 snapshot_state: Callable[[str], SnapshotState], origin_allowed: Callable[[str], None],
                 begin_execution: Callable[[str], None], before_action: Callable[[PlannedAction, str], str],
                 after_verified: Callable[[PlannedAction, str, str], None],
                 after_failed: Callable[[PlannedAction, str, str], None]):
        self.transport = transport
        self.expected_origin = _origin(expected_origin)
        self.snapshot_state = snapshot_state
        self.origin_allowed = origin_allowed
        self.begin_execution, self.before_action = begin_execution, before_action
        self.after_verified, self.after_failed = after_verified, after_failed

    def _assert_origin(self, target: BrowserTarget) -> None:
        current = self.transport.current_url(target.target_id)
        self.origin_allowed(current)
        if _origin(current) != self.expected_origin:
            raise SessionError(f"Pinned browser target moved to {_origin(current)}; expected {self.expected_origin}.")

    @staticmethod
    def _verified(action: PlannedAction, state: SnapshotState) -> bool:
        if action.action in {"check", "verify"}:
            return any(option.ref == action.ref and option.selected is True
                       for group in state.groups for option in group.options)
        field = next((item for item in state.fields if item.ref == action.ref), None)
        if field is None or action.value is None:
            return False
        return _filename(field.value) == _filename(action.value) if action.action == "upload" else field.value == action.value

    def execute(self, plan: Callable[[SnapshotState], list[PlannedAction]]) -> SessionResult:
        target = self.transport.resolve_target()
        self._assert_origin(target)
        # Do not consume a capability merely to inspect a form with no
        # deterministic write.  The preflight snapshot is read-only.
        state = self.snapshot_state(target.target_id)
        initial_actions = plan(state)
        if not any(item.action in {"fill", "select", "check", "upload"} for item in initial_actions):
            pauses = any(item.action == "pause" for item in initial_actions)
            return SessionResult("needs_review" if pauses else "completed", (), (), (), target.target_id)
        self.begin_execution(target.target_id)
        completed: set[tuple[str, str | None, str | None, str | None]] = set()
        verified: list[str] = []
        executed: list[str] = []
        while True:
            self._assert_origin(target)
            actions = plan(state)
            candidate = next((item for item in actions if item.action in {"fill", "select", "check", "upload", "verify"}
                              and action_identity(item) not in completed), None)
            if candidate is None:
                pauses = any(item.action == "pause" for item in actions)
                return SessionResult("needs_review" if pauses else "completed", tuple(verified), (), tuple(executed), target.target_id)
            if candidate.action == "verify":
                if not self._verified(candidate, state):
                    return SessionResult("partial", tuple(verified), (candidate.ref,), tuple(executed), target.target_id)
                completed.add(action_identity(candidate))
                verified.append(candidate.ref)
                continue
            journal_id = self.before_action(candidate, target.target_id)
            self._assert_origin(target)
            self.transport.execute(target.target_id, {"action": candidate.action, "target": candidate.ref, "value": candidate.value or ""})
            executed.append(candidate.ref)
            self._assert_origin(target)
            fresh = self.snapshot_state(target.target_id)
            if not self._verified(candidate, fresh):
                self.after_failed(candidate, target.target_id, journal_id)
                return SessionResult("partial", tuple(verified), (candidate.ref,), tuple(executed), target.target_id)
            self.after_verified(candidate, target.target_id, journal_id)
            completed.add(action_identity(candidate))
            verified.append(candidate.ref)
            # The next iteration deliberately uses this post-action snapshot
            # to rematch every remaining field; React/ATS rerenders make old
            # accessibility refs unsafe after a write.
            state = fresh
