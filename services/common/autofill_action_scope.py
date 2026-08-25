"""Exact action identity for one human-reviewed deterministic autofill plan.

An approval is not a permission to write a profile key anywhere on a dynamic
page.  It authorizes only the exact action/ref/semantic/value tuple the human
reviewed.  Newly-rendered React controls therefore require a fresh approval.
Document uploads are intentionally excluded: they use a separate privileged
capability.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from services.common.question_memory import normalize_question


def _value_sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _semantic_label(action: Any) -> str:
    return normalize_question(str(getattr(action, "question_label", "") or ""))


def exact_action_identity(action: Any) -> dict[str, str | None]:
    return {
        "action": str(getattr(action, "action", "") or ""),
        "ref": str(getattr(action, "ref", "") or ""),
        "label": _semantic_label(action),
        "profile_key": str(getattr(action, "profile_key", "") or "") or None,
        "value_sha256": _value_sha256(getattr(action, "value", None)),
    }


def build_exact_action_scope(actions: Iterable[Any]) -> dict[str, Any]:
    exact = [exact_action_identity(action) for action in actions
             if str(getattr(action, "action", "")) in {"fill", "select", "check"}]
    # Keep the legacy summary keys for review/context compatibility, but the
    # executor authorizes only ``actions`` below.
    return {
        "version": 2,
        "actions": exact,
        "profile_keys": sorted({str(item["profile_key"]) for item in exact if item.get("profile_key")}),
        "document_types": [],
        "sensitive_classes": [],
        "remembered_questions": sorted({str(item["label"]) for item in exact if item.get("label") and not item.get("profile_key")}),
    }


def action_is_exactly_approved(action: Any, scope: dict[str, Any]) -> bool:
    if str(getattr(action, "action", "")) not in {"fill", "select", "check", "upload"}:
        return True
    # Upload must never inherit autofill approval authority.
    if str(getattr(action, "action", "")) == "upload":
        return False
    if int(scope.get("version") or 0) != 2 or not isinstance(scope.get("actions"), list):
        return False
    wanted = exact_action_identity(action)
    return any(isinstance(item, dict) and {
        "action": item.get("action"),
        "ref": item.get("ref"),
        "label": item.get("label") or "",
        "profile_key": item.get("profile_key") or None,
        "value_sha256": item.get("value_sha256"),
    } == wanted for item in scope["actions"])
