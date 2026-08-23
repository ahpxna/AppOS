"""Pinned, journaled session that rematches after each external side effect."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable
from urllib.parse import urlsplit

from services.autofill.autofill_executor_v1 import AutofillTransport, BrowserTarget
from services.autofill.autofill_planner_v1 import PlannedAction
from services.autofill.form_inspector_v1 import FormField, QuestionGroup
from services.common.autofill_identity import canonical_page_url


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotState:
    fields: tuple[FormField, ...]
    groups: tuple[QuestionGroup, ...]
    page_fingerprint: str = ""
    truncated: bool = False


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


def _normal_text(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _equivalent_value(action: PlannedAction, observed: str, role: str) -> bool:
    if action.value is None:
        return False
    if action.action == "upload":
        return _filename(observed) == _filename(action.value)
    if action.profile_key and action.profile_key.endswith("phone"):
        return re.sub(r"\D", "", observed) == re.sub(r"\D", "", action.value)
    # Select controls sometimes expose the state abbreviation as a value and
    # the full state name as accessible text; accepting only exact text would
    # be a safe but unnecessarily common false-negative. Preserve exactness
    # for all other values.
    aliases = {"nj": "new jersey", "ny": "new york", "ca": "california", "tx": "texas"}
    actual, expected = _normal_text(observed), _normal_text(action.value)
    return actual == expected or role in {"select", "combobox", "listbox"} and aliases.get(actual) == expected or aliases.get(expected) == actual


def action_identity(action: PlannedAction) -> tuple[str, str | None, str | None, str | None]:
    return action.action, action.profile_key, action.question_label, action.value


class AutofillSession:
    """Replans from a fresh snapshot; old refs never survive an action."""
    def __init__(self, *, transport: AutofillTransport, expected_origin: str,
                 expected_initial_url: str, expected_page_fingerprint: str,
                 snapshot_state: Callable[[str], SnapshotState], origin_allowed: Callable[[str], None],
                 begin_execution: Callable[[str], None], before_action: Callable[[PlannedAction, str], str],
                 after_verified: Callable[[PlannedAction, str, str], None],
                 after_failed: Callable[[PlannedAction, str, str], None]):
        self.transport = transport
        self.expected_origin = _origin(expected_origin)
        self.expected_initial_url = canonical_page_url(expected_initial_url)
        self.expected_page_fingerprint = expected_page_fingerprint
        self.snapshot_state = snapshot_state
        self.origin_allowed = origin_allowed
        self.begin_execution, self.before_action = begin_execution, before_action
        self.after_verified, self.after_failed = after_verified, after_failed

    def _assert_origin(self, target: BrowserTarget) -> None:
        current = self.transport.current_url(target.target_id)
        self.origin_allowed(current)
        if _origin(current) != self.expected_origin:
            raise SessionError(f"Pinned browser target moved to {_origin(current)}; expected {self.expected_origin}.")

    def _assert_initial_page(self, target: BrowserTarget, state: SnapshotState) -> None:
        if state.truncated:
            raise SessionError("Browser snapshot is truncated; refusing to authorize an incomplete form view.")
        if canonical_page_url(self.transport.current_url(target.target_id)) != self.expected_initial_url:
            raise SessionError("Pinned browser target is not the approval-bound application page.")
        if not state.page_fingerprint or state.page_fingerprint != self.expected_page_fingerprint:
            raise SessionError("Current application page fingerprint differs from the approved page; refusing writes.")

    @staticmethod
    def _verified(action: PlannedAction, state: SnapshotState) -> bool:
        if action.action in {"check", "verify"}:
            # The option ref may change on an ATS rerender: recover by the
            # question and desired option label, never by a stale old ref.
            return any(
                option.selected is True and (
                    option.ref == action.ref or
                    _normal_text(option.label) == _normal_text(action.value or "")
                ) and (not action.question_label or _normal_text(group.label) == _normal_text(action.question_label))
                for group in state.groups for option in group.options
            )
        field = next((item for item in state.fields if item.ref == action.ref), None)
        if field is None:
            # React/Workday often replaces every accessibility ref. Recover
            # the field semantically by its approved question label.
            field = next((item for item in state.fields
                          if action.question_label and _normal_text(item.label) == _normal_text(action.question_label)), None)
        if field is None:
            return False
        return _equivalent_value(action, field.value, field.role)

    def execute(self, plan: Callable[[SnapshotState], list[PlannedAction]]) -> SessionResult:
        target = self.transport.resolve_target()
        self._assert_origin(target)
        # Do not consume a capability merely to inspect a form with no
        # deterministic write.  The preflight snapshot is read-only.
        state = self.snapshot_state(target.target_id)
        self._assert_initial_page(target, state)
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
            # V1 is intentionally single-page. A new wizard step needs its
            # own approval rather than silently continuing on same-origin URL.
            self._assert_initial_page(target, state)
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
            self._assert_initial_page(target, fresh)
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
