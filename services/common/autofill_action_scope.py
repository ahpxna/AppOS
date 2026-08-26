"""Exact action identity for one human-reviewed deterministic autofill plan.

An approval is not a permission to write a profile key anywhere on a dynamic
page.  It authorizes only the exact action/ref/semantic/value tuple the human
reviewed.  Newly-rendered React controls therefore require a fresh approval.
Document uploads remain separately privileged, but their exact reviewed action
identity is included so a parent autofill session may execute the upload only
after redeeming the matching one-shot child capability.
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
             if str(getattr(action, "action", "")) in {"fill", "select", "check", "upload"}]
    # Keep the legacy summary keys for review/context compatibility, but the
    # executor authorizes only ``actions`` below.
    return {
        "version": 3,
        "actions": exact,
        "profile_keys": sorted({str(item["profile_key"]) for item in exact if item.get("profile_key")}),
        "document_types": sorted({str(item["profile_key"]).removeprefix("documents.") for item in exact
                                  if item.get("action") == "upload" and str(item.get("profile_key") or "").startswith("documents.")}),
        "sensitive_classes": [],
        "remembered_questions": sorted({str(item["label"]) for item in exact if item.get("label") and not item.get("profile_key")}),
    }


def autofill_plan_key(*, application_id: str, page_url: str, page_fingerprint: str,
                      input_hash: str, action_scope: dict[str, Any]) -> str:
    import json
    payload = {
        "application_id": str(application_id),
        "page_url": str(page_url),
        "page_fingerprint": str(page_fingerprint),
        "input_hash": str(input_hash),
        "action_scope": action_scope,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def action_is_exactly_approved(action: Any, scope: dict[str, Any]) -> bool:
    if str(getattr(action, "action", "")) not in {"fill", "select", "check", "upload"}:
        return True
    # An upload identity in this scope is necessary but never sufficient: the
    # browser worker additionally requires a separately approved delegated
    # ``privileged_upload_document`` child capability.
    if int(scope.get("version") or 0) != 3 or not isinstance(scope.get("actions"), list):
        return False
    wanted = exact_action_identity(action)
    return any(isinstance(item, dict) and {
        "action": item.get("action"),
        "ref": item.get("ref"),
        "label": item.get("label") or "",
        "profile_key": item.get("profile_key") or None,
        "value_sha256": item.get("value_sha256"),
    } == wanted for item in scope["actions"])
