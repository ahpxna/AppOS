#!/usr/bin/env python3
"""Human-approved privileged application actions.

Normal autofill never gains a submit action. This service is a separate
one-shot executor for actions whose consequences require explicit Telegram
approval: opening Apply, trusting a discovered employer domain, account
registration/login, employer email verification, consent, MFA/checkpoint retry,
and final Submit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.application_actions.action_request_v1 import PRIVILEGED_TYPES, create_privileged_request
from services.auth.gmail_verification_v1 import refetch_secret
from services.autofill.autofill_agent_v1 import INPUT_ROLES, parse_snapshot
from services.autofill.autofill_executor_v1 import OpenClawTransport, TransportError
from services.common.autofill_identity import canonical_page_url, page_fingerprint
from services.common.config import database_dsn, load_repo_env
from services.common.openclaw_runtime import resolve_openclaw_binary
from services.security.credential_vault_v1 import (
    VaultError, generate_password, mask_entry, read_secret, store_secret,
)

load_repo_env()

APPLY_LABELS = {"apply", "easy apply", "apply now", "apply on company website", "apply on company site"}
SUBMIT_LABELS = {"submit", "submit application", "submit your application", "send application", "complete application"}
CREATE_LABELS = {"create account", "create an account", "sign up", "register", "continue"}
LOGIN_LABELS = {"sign in", "log in", "login", "continue"}
VERIFY_LABELS = {"verify", "verify email", "confirm", "confirm email", "continue"}
CONSENT_WORDS = ("privacy", "terms", "consent", "certify", "agree", "acknowledge", "declaration")
ADVANCE_LABELS = {"next", "continue", "review", "review application", "save and continue"}


class PrivilegedActionError(RuntimeError):
    pass


def _transport() -> OpenClawTransport:
    return OpenClawTransport(binary=resolve_openclaw_binary(required=True),
                             profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"), timeout=90)


def _origin(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        raise PrivilegedActionError("browser URL is not HTTP(S)")
    return f"{p.scheme.casefold()}://{p.netloc.casefold()}"


def _host_is_allowed(cur, url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    if not host:
        return False
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled=true;")
    return any(host == str(row[0]).casefold() or host.endswith("." + str(row[0]).casefold())
               for row in cur.fetchall())


def _require_trusted_target(cur, url: str) -> None:
    if not _host_is_allowed(cur, url):
        host = (urlsplit(url).hostname or "unknown").casefold()
        raise PrivilegedActionError(
            f"target domain {host!r} is not human-trusted yet; approve the separate trust-domain gate first"
        )


def _snapshot(transport: OpenClawTransport, target_id: str) -> tuple[str, dict[str, Any], list[dict[str, Any]], str]:
    url = transport.current_url(target_id)
    payload = transport.snapshot(target_id)
    if payload.get("truncated"):
        raise PrivilegedActionError("browser snapshot is truncated; privileged action requires a complete snapshot")
    return url, payload, parse_snapshot(payload), page_fingerprint(payload, page_url=url)


def _clickables(nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for n in nodes:
        if n.get("ref") and str(n.get("role") or "").casefold() in {"button", "link"}:
            label = str(n.get("label") or n.get("value") or "").strip()
            if label:
                out.append({"ref": str(n["ref"]), "label": label, "role": str(n.get("role") or "")})
    return out


def _find_exact_control(nodes: list[dict[str, Any]], labels: set[str]) -> dict[str, str]:
    matches = [c for c in _clickables(nodes) if c["label"].strip().casefold() in labels]
    if len(matches) != 1:
        raise PrivilegedActionError(f"expected exactly one control in {sorted(labels)}, found {len(matches)}")
    return matches[0]


def _find_input(nodes: list[dict[str, Any]], words: tuple[str, ...], *, role: str | None = None) -> dict[str, Any] | None:
    candidates = []
    for n in nodes:
        if not n.get("ref") or str(n.get("role") or "").casefold() not in INPUT_ROLES:
            continue
        if role and str(n.get("role") or "").casefold() != role:
            continue
        label = str(n.get("label") or "").casefold()
        if any(word in label for word in words):
            candidates.append(n)
    return candidates[0] if len(candidates) == 1 else None


def _profile_values(cur) -> dict[str, str]:
    # Profile context is optional for Telegram/account planning, but a failed
    # optional query must not poison the caller's PostgreSQL transaction.
    cur.execute("SAVEPOINT jobos_profile_values")
    try:
        cur.execute("SELECT field_name, field_value FROM v_autofill_ready_values;")
        values = {str(k): str(v) for k, v in cur.fetchall() if v is not None}
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT jobos_profile_values")
        values = {}
    finally:
        cur.execute("RELEASE SAVEPOINT jobos_profile_values")
    first, last = values.get("legal_first_name", ""), values.get("legal_last_name", "")
    if first and last:
        values.setdefault("full_name", f"{first} {last}")
    return values


def _account_action_payload(cur, nodes: list[dict[str, Any]], url: str, *, action: str) -> tuple[dict[str, Any], str]:
    values = _profile_values(cur)
    email = values.get("email") or values.get("email_address") or ""
    first = values.get("legal_first_name", ""); last = values.get("legal_last_name", "")
    full = values.get("full_name") or f"{first} {last}".strip()
    plan: list[dict[str, Any]] = []
    for label_words, value, source in ((("email", "e-mail"), email, "profile"),
                                       (("first name", "given name"), first, "profile"),
                                       (("last name", "family name", "surname"), last, "profile"),
                                       (("full name", "name"), full, "profile")):
        node = _find_input(nodes, label_words)
        if node and value:
            plan.append({"ref": str(node["ref"]), "label": str(node.get("label") or ""), "source": source,
                         "profile_value": value, "value_sha256": _value_hash(value)})
    password_nodes = [n for n in nodes if n.get("ref") and str(n.get("role") or "").casefold() in INPUT_ROLES
                      and "password" in str(n.get("label") or "").casefold()]
    primary_password = next((n for n in password_nodes if not any(word in str(n.get("label") or "").casefold()
                            for word in ("confirm", "repeat", "re-enter", "reenter"))), None)
    confirm_passwords = [n for n in password_nodes if n is not primary_password]
    if primary_password:
        vault_origin = _origin(url)
        vault_account = email or "unknown"
        vault = mask_entry(cur, origin=vault_origin, account_key=vault_account, secret_kind="password")
        if action == "create_employer_account" and vault.get("status") != "active":
            # Generate once into the encrypted vault before asking for account-creation
            # approval. The plaintext is never returned in Telegram/context/logs.
            generated = generate_password()
            store_secret(cur, origin=vault_origin, account_key=vault_account, secret_kind="password",
                         secret=generated, metadata={"generated_by": "jobos-account-registration"})
            generated = ""  # drop the only local plaintext reference as early as possible
            vault = mask_entry(cur, origin=vault_origin, account_key=vault_account, secret_kind="password")
        if action == "login_employer_account" and vault.get("status") != "active":
            raise PrivilegedActionError(
                "employer password is not available in the encrypted vault; use the manual-auth gate or store it first"
            )
        plan.append({"ref": str(primary_password["ref"]), "label": str(primary_password.get("label") or "Password"),
                     "source": "vault", "vault_origin": vault_origin, "vault_account": vault_account,
                     "vault_kind": "password", "value_sha256": vault.get("sha256_prefix", "NaN")})
        for confirm in confirm_passwords:
            plan.append({"ref": str(confirm["ref"]), "label": str(confirm.get("label") or "Confirm password"),
                         "source": "vault", "vault_origin": vault_origin, "vault_account": vault_account,
                         "vault_kind": "password", "value_sha256": vault.get("sha256_prefix", "NaN")})
    control = _find_exact_control(nodes, CREATE_LABELS if action == "create_employer_account" else LOGIN_LABELS)
    consent_items = _consent_items(nodes) if action == "create_employer_account" else []
    payload = {"field_plan": plan, "control_ref": control["ref"], "control_label": control["label"],
               "account_email": email or "NaN", "consent_items": consent_items,
               "consent_blockers": [item["label"] or item["ref"] for item in consent_items if item.get("selected") is not True]}
    return payload, email


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def detect_platform(url: str, snapshot: dict[str, Any]) -> str:
    text = f"{url}\n{snapshot.get('snapshot') or ''}".casefold()
    host = (urlsplit(url).hostname or "").casefold()
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and any(
            marker in text for marker in ("easy apply", "submit application", "contact info", "additional questions")):
        return "linkedin_easy_apply"
    for platform, markers in (
        ("workday", ("myworkdayjobs", "workday")),
        ("greenhouse", ("greenhouse.io", "boards.greenhouse")),
        ("lever", ("jobs.lever.co", "lever")),
        ("ashby", ("jobs.ashbyhq.com", "ashby")),
        ("smartrecruiters", ("smartrecruiters",)),
        ("oracle", ("oraclecloud", "oracle recruiting")),
        ("successfactors", ("successfactors",)),
        ("icims", ("icims",)),
    ):
        if any(marker in text for marker in markers):
            return platform
    return "custom"


def detect_page_state(url: str, snapshot: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    text = f"{url}\n{snapshot.get('snapshot') or ''}".casefold()

    # =====================================================================
    # !!! HUMAN CHECKPOINT BOUNDARY — DO NOT MERGE WITH OTP/MFA APPROVAL !!!
    # CAPTCHA / bot challenge / risk checkpoint always becomes
    # needs_human_checkpoint. Telegram approval here means only "I handled it;
    # re-snapshot now". It never authorizes CAPTCHA solving, OTP reuse, login,
    # consent, autofill, or final Submit.
    # =====================================================================
    checkpoint_markers = ("captcha", "verify you are human", "security check", "risk checkpoint", "bot challenge", "recaptcha", "hcaptcha", "arkose")
    if any(marker in text for marker in checkpoint_markers):
        return "needs_human_checkpoint", {"reason": "human checkpoint detected"}

    mfa_markers = ("authenticator", "security key", "passkey", "text message", "sms code", "push notification", "approve the sign-in")
    if any(marker in text for marker in mfa_markers):
        return "needs_mfa", {"reason": "non-email MFA detected"}
    email_verify = ("verify your email", "verification code", "email verification", "code sent to", "check your email")
    if any(marker in text for marker in email_verify):
        field = _find_input(nodes, ("code", "verification", "otp", "one-time"))
        return "needs_email_verification", {"field_ref": str(field.get("ref")) if field else "NaN"}
    password = _find_input(nodes, ("password",))
    if password or any(marker in text for marker in ("create account", "sign in", "log in", "register")):
        if any(marker in text for marker in ("sign in with google", "continue with google", "sign in with microsoft", "continue with microsoft")):
            return "needs_manual_sso", {"reason": "browser SSO requires manual identity-provider session"}
        return "needs_account_auth", {"reason": "employer account authentication required"}
    inputs = [n for n in nodes if n.get("ref") and str(n.get("role") or "").casefold() in INPUT_ROLES]
    if inputs:
        return "application_form_ready", {"input_count": len(inputs)}
    return "unknown", {"reason": "page state could not be classified"}


def _update_auth_session(cur, *, application_id: str, url: str, fingerprint: str,
                         state: str, platform: str, detail: dict[str, Any]) -> None:
    cur.execute(
        """INSERT INTO application_auth_sessions(
               application_id, employer_origin, platform_hint, auth_state, current_url,
               page_fingerprint, last_event, detail_json, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
           ON CONFLICT (application_id) DO UPDATE SET
               employer_origin=EXCLUDED.employer_origin, platform_hint=EXCLUDED.platform_hint,
               auth_state=EXCLUDED.auth_state, current_url=EXCLUDED.current_url,
               page_fingerprint=EXCLUDED.page_fingerprint, last_event=EXCLUDED.last_event,
               detail_json=EXCLUDED.detail_json, updated_at=now();""",
        (application_id, _origin(url), platform, state, url, fingerprint, state, Jsonb(detail)),
    )
    step_map = {
        "needs_account_auth": "needs_account_auth", "needs_email_verification": "needs_email_verification",
        "needs_mfa": "needs_mfa", "needs_human_checkpoint": "needs_human_checkpoint",
        "needs_manual_sso": "needs_account_auth", "application_form_ready": "application_form_ready",
        "authenticated": "application_form_ready",
    }
    if state in step_map:
        cur.execute("UPDATE applications SET current_step=%s, updated_at=now() WHERE id=%s;", (step_map[state], application_id))
    if platform != "custom":
        cur.execute("SELECT 1 FROM ats_capabilities WHERE ats_type=%s;", (platform,))
        if cur.fetchone():
            cur.execute("UPDATE applications SET ats_type=%s, updated_at=now() WHERE id=%s;", (platform, application_id))


def _document_bindings(cur, application_id: str) -> dict[str, Any]:
    cur.execute(
        """SELECT gda.artifact_type, gda.file_path, gda.filename, gda.sha256
             FROM applications a
             JOIN generated_document_artifacts gda
               ON gda.id IN (a.approved_resume_artifact_id, a.approved_cover_letter_artifact_id)
            WHERE a.id=%s;""",
        (application_id,),
    )
    return {str(kind): {"file_path": path, "filename": filename, "sha256": sha}
            for kind, path, filename, sha in cur.fetchall()}


def _consent_items(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for n in nodes:
        if not n.get("ref") or str(n.get("role") or "").casefold() not in {"checkbox", "radio", "button"}:
            continue
        label = str(n.get("label") or "")
        if any(word in label.casefold() for word in CONSENT_WORDS):
            items.append({"ref": str(n["ref"]), "label": label, "selected": n.get("selected")})
    return items


def _field_state(nodes: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[str]]:
    fields = []
    blockers = []
    for n in nodes:
        if not n.get("ref") or str(n.get("role") or "").casefold() not in INPUT_ROLES:
            continue
        item = {"ref": str(n["ref"]), "label": str(n.get("label") or ""), "role": str(n.get("role") or ""),
                "required": bool(n.get("required")), "value": str(n.get("value") or ""), "selected": n.get("selected")}
        fields.append(item)
        if item["required"] and not item["value"] and item["selected"] is not True:
            blockers.append(item["label"] or item["ref"])
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return digest, fields, blockers


def _capture_review_screenshot(transport: OpenClawTransport, application_id: str, action: str, target_id: str) -> str | None:
    try:
        source = transport.screenshot(target_id, full_page=True)
        dest = ROOT / "data" / "review-artifacts" / application_id / "privileged-actions"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{action}-{int(time.time())}.png"
        path.write_bytes(source.read_bytes()); path.chmod(0o600)
        return str(path.resolve())
    except Exception:
        return None


def _base_binding(transport: OpenClawTransport) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], str]:
    target = transport.resolve_target()
    url, snapshot, nodes, fingerprint = _snapshot(transport, target.target_id)
    return target.target_id, canonical_page_url(url), snapshot, nodes, fingerprint


def prepare(cur, *, application_id: str, action: str, candidate_id: str | None = None) -> str:
    transport = _transport()
    target_id, url, snapshot, nodes, fingerprint = _base_binding(transport)
    screenshot = _capture_review_screenshot(transport, application_id, action, target_id)
    payload: dict[str, Any] = {"target_id": target_id, "expected_url": url,
                               "expected_page_fingerprint": fingerprint,
                               "expected_origin": _origin(url),
                               "review_context": {"screenshot_path": screenshot or "NaN"}}
    summary = action
    if action == "begin_application":
        cur.execute("SELECT job_url FROM applications WHERE id=%s;", (application_id,))
        source_row = cur.fetchone()
        source_job_url = str(source_row[0] or "") if source_row else ""
        if not source_job_url or canonical_page_url(source_job_url) != canonical_page_url(url):
            raise PrivilegedActionError("focused browser page is not the exact stored job URL for this application")
        control = _find_exact_control(nodes, APPLY_LABELS)
        payload.update({"control_ref": control["ref"], "control_label": control["label"],
                        "source_job_url": canonical_page_url(source_job_url)})
        summary = f"Open the employer application entry point by clicking {control['label']!r}."
        atype = "privileged_begin_application"
    elif action == "trust_external_domain":
        payload.update({"domain": (urlsplit(url).hostname or "").casefold()})
        summary = f"Trust employer application domain {_origin(url)} for this application."
        atype = "privileged_trust_external_domain"
    elif action in {"create_employer_account", "login_employer_account"}:
        account_payload, _email = _account_action_payload(cur, nodes, url, action=action)
        payload.update(account_payload)
        atype = "privileged_create_employer_account" if action == "create_employer_account" else "privileged_login_employer_account"
        summary = f"{action.replace('_',' ').title()} on {_origin(url)} using exact approved fields; password comes from encrypted vault only."
    elif action == "accept_terms":
        items = _consent_items(nodes)
        if not items:
            raise PrivilegedActionError("no explicit terms/privacy/consent controls found")
        payload["consent_items"] = items
        atype = "privileged_accept_terms"; summary = f"Accept {len(items)} exact employer consent/terms control(s)."
    elif action == "advance_application_step":
        control = _find_exact_control(nodes, ADVANCE_LABELS)
        field_digest, fields, blockers = _field_state(nodes)
        payload.update({"control_ref": control["ref"], "control_label": control["label"],
                        "field_state_sha256": field_digest, "required_blockers": blockers,
                        "review_context": {"screenshot_path": screenshot or "NaN", "write_actions": fields,
                                           "will_pause": blockers}})
        atype = "privileged_advance_application_step"
        summary = f"Advance this exact application wizard by clicking {control['label']!r}; final Submit remains separate."
    elif action == "auth_manual_retry":
        atype = "privileged_auth_manual_retry"; summary = "Re-snapshot after you manually complete an ambiguous/SSO employer login step."
    elif action == "mfa_retry":
        atype = "privileged_mfa_retry"; summary = "Re-snapshot after you manually complete SMS/TOTP/push/passkey MFA."
    elif action == "checkpoint_retry":
        atype = "privileged_checkpoint_retry"; summary = "Re-snapshot after you manually complete the CAPTCHA/bot/risk checkpoint."
    elif action == "submit_application":
        control = _find_exact_control(nodes, SUBMIT_LABELS)
        field_digest, fields, blockers = _field_state(nodes)
        payload.update({"control_ref": control["ref"], "control_label": control["label"],
                        "field_state_sha256": field_digest, "required_blockers": blockers,
                        "review_context": {"screenshot_path": screenshot or "NaN", "write_actions": fields,
                                           "will_pause": blockers},
                        "document_bindings": _document_bindings(cur, application_id)})
        atype = "privileged_submit_application"; summary = f"FINAL SUBMIT: click exact {control['label']!r} only after fresh revalidation."
    elif action == "use_email_verification":
        if not candidate_id:
            raise PrivilegedActionError("--candidate-id is required")
        cur.execute("""SELECT gmail_message_id, sender, subject, received_at, verification_kind, secret_sha256, secret_context_json
                       FROM email_verification_candidates WHERE id=%s AND application_id=%s AND status IN ('discovered','approved');""",
                    (candidate_id, application_id))
        row = cur.fetchone()
        if not row:
            raise PrivilegedActionError("verification candidate unavailable")
        field = _find_input(nodes, ("code", "verification", "otp", "one-time")) if row[4] == "numeric_code" else None
        button = None
        if row[4] == "numeric_code":
            try: button = _find_exact_control(nodes, VERIFY_LABELS)
            except PrivilegedActionError: button = None
        payload.update({"candidate_id": candidate_id, "gmail_message_id": row[0], "sender": row[1] or "NaN",
                        "subject": row[2] or "NaN", "received_at": row[3].isoformat() if row[3] else "NaN",
                        "verification_kind": row[4], "secret_sha256": row[5], "secret_context": row[6] or {},
                        "field_ref": str(field.get('ref')) if field else "NaN",
                        "control_ref": button["ref"] if button else "NaN", "control_label": button["label"] if button else "NaN"})
        atype = "privileged_use_email_verification"; summary = f"Use exact Gmail verification {row[4]} after Telegram approval; secret stays out of DB/Telegram."
    else:
        raise PrivilegedActionError(f"unsupported prepare action: {action}")
    return create_privileged_request(cur, application_id=application_id, action_type=atype,
                                     payload=payload, summary=summary, requested_by="jobos-action")


def _revalidate(transport: OpenClawTransport, payload: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]], str]:
    target_id = str(payload.get("target_id") or "")
    if not target_id:
        raise PrivilegedActionError("approval has no pinned target id")
    url, snap, nodes, fp = _snapshot(transport, target_id)
    if canonical_page_url(url) != canonical_page_url(str(payload.get("expected_url") or "")):
        raise PrivilegedActionError("page URL changed after approval")
    if fp != payload.get("expected_page_fingerprint"):
        raise PrivilegedActionError("page fingerprint changed after approval")
    return url, snap, nodes, fp


def _click(transport: OpenClawTransport, target_id: str, ref: str) -> None:
    transport._run(["click", ref, "--target-id", target_id])


def _fill(transport: OpenClawTransport, target_id: str, ref: str, value: str) -> None:
    payload = json.dumps([{"ref": ref, "value": value}], separators=(",", ":"))
    transport._run(["fill", "--fields", payload, "--target-id", target_id])


def _resolve_plan_value(cur, item: dict[str, Any]) -> str:
    if item.get("source") == "profile":
        value = str(item.get("profile_value") or "")
        if not value or _value_hash(value) != item.get("value_sha256"):
            raise PrivilegedActionError("approved profile value binding is missing or changed")
        return value
    if item.get("source") == "vault":
        value = read_secret(cur, origin=str(item.get("vault_origin")), account_key=str(item.get("vault_account")),
                            secret_kind=str(item.get("vault_kind") or "password"))
        prefix = str(item.get("value_sha256") or "")
        if prefix != "NaN" and not hashlib.sha256(value.encode()).hexdigest().startswith(prefix):
            raise PrivilegedActionError("vault secret rotated after approval; prepare a fresh action")
        return value
    raise PrivilegedActionError("unknown field value source")


def _enqueue_state_followup(cur, transport: OpenClawTransport, *, application_id: str, target_id: str,
                            url: str, fingerprint: str, nodes: list[dict[str, Any]], state: str) -> None:
    base = {"target_id": target_id, "expected_url": canonical_page_url(url),
            "expected_page_fingerprint": fingerprint, "expected_origin": _origin(url),
            "review_context": {"screenshot_path": _capture_review_screenshot(transport, application_id, state, target_id) or "NaN"}}
    if state == "needs_human_checkpoint":
        create_privileged_request(cur, application_id=application_id, action_type="privileged_checkpoint_retry",
                                  payload=base, summary="Human checkpoint detected. Handle it manually in the pinned browser, then approve RETRY AFTER I FINISH.",
                                  requested_by="auth-state-router")
        return
    if state == "needs_mfa":
        create_privileged_request(cur, application_id=application_id, action_type="privileged_mfa_retry",
                                  payload=base, summary="Non-email MFA detected. Complete it manually, then approve RETRY AFTER MFA.",
                                  requested_by="auth-state-router")
        return
    if state == "needs_manual_sso":
        create_privileged_request(cur, application_id=application_id, action_type="privileged_auth_manual_retry",
                                  payload=base, summary="Employer SSO/ambiguous auth requires manual login. Complete it in the pinned browser, then approve AUTH RETRY.",
                                  requested_by="auth-state-router")
        return
    if state != "needs_account_auth":
        return
    text = " ".join(str(n.get("label") or n.get("value") or "") for n in nodes).casefold()
    action = "create_employer_account" if any(k in text for k in ("create account", "create an account", "register", "sign up")) else (
             "login_employer_account" if any(k in text for k in ("sign in", "log in", "login")) else "")
    if not action:
        create_privileged_request(cur, application_id=application_id, action_type="privileged_auth_manual_retry",
                                  payload=base, summary="Employer auth page could not be classified safely. Complete login/register manually, then approve AUTH RETRY.",
                                  requested_by="auth-state-router")
        return
    try:
        account_payload, _ = _account_action_payload(cur, nodes, url, action=action)
    except Exception:
        create_privileged_request(cur, application_id=application_id, action_type="privileged_auth_manual_retry",
                                  payload=base, summary="Employer auth controls are ambiguous. Complete this auth step manually, then approve AUTH RETRY.",
                                  requested_by="auth-state-router")
        return
    if action == "create_employer_account" and account_payload.get("consent_blockers"):
        consent_payload = dict(base); consent_payload["consent_items"] = account_payload.get("consent_items") or []
        create_privileged_request(cur, application_id=application_id, action_type="privileged_accept_terms",
                                  payload=consent_payload, summary="Employer account registration requires explicit terms/consent approval before account creation.",
                                  requested_by="auth-state-router")
        return
    atype = "privileged_create_employer_account" if action == "create_employer_account" else "privileged_login_employer_account"
    action_payload = dict(base); action_payload.update(account_payload)
    create_privileged_request(cur, application_id=application_id, action_type=atype, payload=action_payload,
                              summary=f"{action.replace('_',' ').title()} using exact profile fields and encrypted-vault password if present.",
                              requested_by="auth-state-router")


def _after_navigation(cur, transport: OpenClawTransport, application_id: str, source_target: str,
                      before_tabs: list[dict[str, Any]]) -> dict[str, Any]:
    time.sleep(1.5)
    after_tabs = transport._tabs()
    before_ids = {transport._stable_id(t) for t in before_tabs}
    new_tabs = [t for t in after_tabs if transport._stable_id(t) and transport._stable_id(t) not in before_ids]
    if len(new_tabs) == 1:
        target_id = transport._stable_id(new_tabs[0])
    else:
        target_id = source_target
    url, snap, nodes, fp = _snapshot(transport, target_id)
    platform = detect_platform(url, snap)
    state, detail = detect_page_state(url, snap, nodes)
    _update_auth_session(cur, application_id=application_id, url=url, fingerprint=fp,
                         state=state, platform=platform, detail={**detail, "target_id": target_id})

    # A human must trust a newly discovered employer origin before JobOS may
    # create any follow-up capability that can write/authenticate on it. The
    # approved Apply/email navigation may *observe* the destination, but trust
    # is a separate one-shot gate. After trust is approved, that executor takes
    # a fresh snapshot and enqueues the next auth/form gate.
    if not _host_is_allowed(cur, url):
        host = (urlsplit(url).hostname or "").casefold()
        if host:
            create_privileged_request(
                cur, application_id=application_id, action_type="privileged_trust_external_domain",
                payload={"target_id": target_id, "expected_url": canonical_page_url(url),
                         "expected_page_fingerprint": fp, "expected_origin": _origin(url), "domain": host,
                         "review_context": {"screenshot_path": _capture_review_screenshot(transport, application_id, "trust-domain", target_id) or "NaN"}},
                summary=f"Trust newly discovered employer application domain {host}.",
                requested_by="application-handoff",
            )
        return {"target_id": target_id, "url": url, "platform": platform, "state": state,
                "detail": detail, "followup": "trust_domain_required"}

    _enqueue_state_followup(cur, transport, application_id=application_id, target_id=target_id,
                            url=url, fingerprint=fp, nodes=nodes, state=state)
    return {"target_id": target_id, "url": url, "platform": platform, "state": state, "detail": detail}


def _document_hashes_still_match(bindings: dict[str, Any]) -> bool:
    for item in bindings.values():
        if not isinstance(item, dict):
            continue
        path, expected = item.get("file_path"), item.get("sha256")
        if not path or not expected or not Path(path).is_file():
            return False
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if digest != expected:
            return False
    return True


def _confirmation(snapshot: dict[str, Any], url: str) -> bool:
    text = f"{url}\n{snapshot.get('snapshot') or ''}".casefold()
    return any(marker in text for marker in ("application submitted", "thank you for applying", "thank you for your application",
                                              "we received your application", "application has been submitted", "submission successful"))


def execute_one(conn, request_id: str) -> dict[str, Any]:
    transport = _transport()
    io_started = False
    execution_id = None
    with conn.cursor() as cur:
        cur.execute("""SELECT id::text, type, application_id::text, status, token_expires_at, payload_json
                       FROM approval_requests WHERE id=%s FOR UPDATE;""", (request_id,))
        row = cur.fetchone()
        if not row or row[1] not in PRIVILEGED_TYPES:
            raise PrivilegedActionError("privileged approval not found")
        _rid, atype, app_id, status, expires, payload = row
        if status != "approved" or not expires or expires <= __import__('datetime').datetime.now(expires.tzinfo):
            raise PrivilegedActionError(f"approval is not live/approved: {status}")
        cur.execute("""INSERT INTO privileged_action_executions(
                           approval_request_id, application_id, action_type, status, expected_url, expected_page_fingerprint)
                       VALUES (%s,%s,%s,'running',%s,%s)
                       ON CONFLICT (approval_request_id) DO NOTHING RETURNING id::text;""",
                    (request_id, app_id, atype, payload.get("expected_url"), payload.get("expected_page_fingerprint")))
        created = cur.fetchone()
        if not created:
            raise PrivilegedActionError("this approval already has an execution record; never replay it")
        execution_id = str(created[0]); conn.commit()
    try:
        with conn.cursor() as cur:
            payload = dict(payload or {})
            if atype == "privileged_trust_external_domain":
                domain = str(payload.get("domain") or "").casefold()
                if payload.get("trust_source") == "gmail_magic_link":
                    cur.execute(
                        """SELECT gmail_message_id, verification_kind, secret_sha256, secret_context_json
                             FROM email_verification_candidates
                            WHERE id=%s AND application_id=%s AND status IN ('discovered','approved');""",
                        (payload.get("candidate_id"), app_id),
                    )
                    cand = cur.fetchone()
                    if not cand or cand[1] != "magic_link":
                        raise PrivilegedActionError("email magic-link trust candidate is unavailable")
                    if str(cand[2]) != str(payload.get("secret_sha256") or ""):
                        raise PrivilegedActionError("email magic-link trust hash changed")
                    secret = refetch_secret({
                        "gmail_message_id": cand[0], "verification_kind": cand[1],
                        "secret_sha256": cand[2], "secret_context": cand[3] or {},
                    })
                    observed_domain = (urlsplit(secret).hostname or "").casefold()
                    if not domain or domain != observed_domain:
                        raise PrivilegedActionError("email magic-link domain does not match the approved trust gate")
                    cur.execute(
                        """INSERT INTO allowed_domains(domain, category, enabled)
                           VALUES (%s,'email_verification',true)
                           ON CONFLICT (domain) DO UPDATE SET enabled=true;""",
                        (domain,),
                    )
                    secret = ""
                    result = {"trusted_domain": domain, "trust_source": "gmail_magic_link"}
                else:
                    live_url, live_snap, live_nodes, live_fp = _revalidate(transport, dict(payload or {}))
                    if not domain or domain != (urlsplit(str(payload.get("expected_url") or "")).hostname or "").casefold():
                        raise PrivilegedActionError("domain binding invalid")
                    if domain != (urlsplit(live_url).hostname or "").casefold():
                        raise PrivilegedActionError("browser target no longer belongs to the approved employer domain")
                    cur.execute("""INSERT INTO allowed_domains(domain, category, enabled)
                                   VALUES (%s,'employer_ats',true)
                                   ON CONFLICT (domain) DO UPDATE SET enabled=true;""", (domain,))
                    platform = detect_platform(live_url, live_snap)
                    state, detail = detect_page_state(live_url, live_snap, live_nodes)
                    _update_auth_session(cur, application_id=app_id, url=live_url, fingerprint=live_fp,
                                         state=state, platform=platform, detail={**detail, "target_id": str(payload.get("target_id") or "")})
                    _enqueue_state_followup(cur, transport, application_id=app_id, target_id=str(payload.get("target_id") or ""),
                                            url=live_url, fingerprint=live_fp, nodes=live_nodes, state=state)
                    result = {"trusted_domain": domain, "state": state, "platform": platform}
            elif atype in {"privileged_auth_manual_retry", "privileged_mfa_retry", "privileged_checkpoint_retry"}:
                # Retry capabilities are intentionally read-only: after the human
                # handles MFA/checkpoint, JobOS only takes a fresh snapshot and
                # classifies what is next.
                target_id = str(payload.get("target_id")); url = transport.current_url(target_id)
                _require_trusted_target(cur, url)
                snap = transport.snapshot(target_id); nodes = parse_snapshot(snap); fp = page_fingerprint(snap, page_url=url)
                platform = detect_platform(url, snap); state, detail = detect_page_state(url, snap, nodes)
                _update_auth_session(cur, application_id=app_id, url=url, fingerprint=fp, state=state, platform=platform, detail={**detail, "target_id": target_id})
                _enqueue_state_followup(cur, transport, application_id=app_id, target_id=target_id,
                                        url=url, fingerprint=fp, nodes=nodes, state=state)
                result = {"target_id": target_id, "url": url, "state": state, "platform": platform}
            else:
                url, snap, nodes, fp = _revalidate(transport, payload)
                target_id = str(payload["target_id"])
                if atype != "privileged_begin_application":
                    _require_trusted_target(cur, url)
                before_tabs = transport._tabs()
                if atype == "privileged_begin_application":
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                elif atype in {"privileged_create_employer_account", "privileged_login_employer_account"}:
                    if atype == "privileged_create_employer_account" and payload.get("consent_blockers"):
                        raise PrivilegedActionError("account registration has unapproved terms/consent controls; approve those separately first")
                    resolved_plan = [(item, _resolve_plan_value(cur, item)) for item in (payload.get("field_plan") or [])]
                    for item, value in resolved_plan:
                        io_started = True; _fill(transport, target_id, str(item["ref"]), value)
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    if payload.get("account_email") and payload.get("account_email") != "NaN":
                        cur.execute("UPDATE application_auth_sessions SET account_email=%s, updated_at=now() WHERE application_id=%s;",
                                    (payload.get("account_email"), app_id))
                elif atype == "privileged_accept_terms":
                    for item in payload.get("consent_items") or []:
                        if item.get("selected") is not True:
                            io_started = True; _click(transport, target_id, str(item["ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    result["consent_items"] = payload.get("consent_items") or []
                elif atype == "privileged_advance_application_step":
                    if payload.get("required_blockers"):
                        raise PrivilegedActionError("required form blockers existed in the approved wizard-step package")
                    current_digest, _fields, blockers = _field_state(nodes)
                    if blockers or current_digest != payload.get("field_state_sha256"):
                        raise PrivilegedActionError("form fields changed after application-step approval")
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                elif atype == "privileged_use_email_verification":
                    cur.execute("""SELECT gmail_message_id, verification_kind, secret_sha256, secret_context_json
                                   FROM email_verification_candidates WHERE id=%s AND application_id=%s AND status IN ('approved','discovered');""",
                                (payload.get("candidate_id"), app_id))
                    cand = cur.fetchone()
                    if not cand:
                        raise PrivilegedActionError("verification candidate unavailable")
                    secret = refetch_secret({"gmail_message_id": cand[0], "verification_kind": cand[1],
                                              "secret_sha256": cand[2], "secret_context": cand[3] or {}})
                    if cand[1] == "numeric_code":
                        ref = str(payload.get("field_ref") or "")
                        if not ref or ref == "NaN": raise PrivilegedActionError("verification code field was not bound")
                        io_started = True; _fill(transport, target_id, ref, secret)
                        control = str(payload.get("control_ref") or "")
                        if control and control != "NaN": io_started = True; _click(transport, target_id, control)
                        result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    else:
                        _require_trusted_target(cur, secret)
                        io_started = True; transport._run(["open", secret])
                        result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    cur.execute("UPDATE email_verification_candidates SET status='consumed', consumed_at=now() WHERE id=%s;", (payload.get("candidate_id"),))
                elif atype == "privileged_submit_application":
                    if payload.get("required_blockers"):
                        raise PrivilegedActionError("required form blockers existed in the approved review package")
                    current_digest, _fields, blockers = _field_state(nodes)
                    if blockers or current_digest != payload.get("field_state_sha256"):
                        raise PrivilegedActionError("form fields changed after final-submit approval")
                    bindings = payload.get("document_bindings") or {}
                    if not isinstance(bindings.get("resume"), dict):
                        raise PrivilegedActionError("final Submit requires an exact approved resume artifact binding")
                    if not _document_hashes_still_match(bindings):
                        raise PrivilegedActionError("approved resume/cover artifact changed or is missing")
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    time.sleep(2.0)
                    observed_url = transport.current_url(target_id)
                    observed = transport.snapshot(target_id)
                    if not _confirmation(observed, observed_url):
                        raise PrivilegedActionError("Submit was clicked but confirmation is uncertain; reconcile manually before any retry")
                    cur.execute("UPDATE applications SET current_step='submitted', status='submitted', submitted_at=now(), updated_at=now() WHERE id=%s;", (app_id,))
                    result = {"submitted": True, "confirmation_url": observed_url}
                else:
                    raise PrivilegedActionError(f"unimplemented privileged type: {atype}")
            cur.execute("""UPDATE privileged_action_executions SET status='completed', result_json=%s, finished_at=now()
                           WHERE id=%s;""", (Jsonb(result), execution_id))
            cur.execute("""UPDATE approval_requests SET status='consumed', consumed_at=now(), consumed_by='privileged-action-executor'
                           WHERE id=%s AND status='approved';""", (request_id,))
            cur.execute("""INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                           VALUES (%s,'privileged_action_completed','privileged-action-executor',%s);""",
                        (app_id, Jsonb({"approval_request_id": request_id, "action_type": atype, "result": result})))
        conn.commit(); return {"ok": True, "request_id": request_id, "action_type": atype, "result": result}
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            state = "needs_reconciliation" if io_started else "failed"
            cur.execute("""UPDATE privileged_action_executions SET status=%s, error_message=%s, finished_at=now()
                           WHERE id=%s;""", (state, str(exc)[:2000], execution_id))
            if io_started:
                cur.execute("""UPDATE approval_requests SET status='consumed', consumed_at=now(), consumed_by='privileged-action-executor', action_note=%s
                               WHERE id=%s AND status='approved';""", (f"Uncertain side effect: {exc}"[:500], request_id))
            else:
                cur.execute("UPDATE approval_requests SET status='expired', action_note=%s WHERE id=%s AND status='approved';",
                            (f"Pre-I/O binding refused: {exc}"[:500], request_id))
            cur.execute("""INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                           VALUES (%s,%s,'privileged-action-executor',%s);""",
                        (app_id, "privileged_action_needs_reconciliation" if io_started else "privileged_action_refused",
                         Jsonb({"approval_request_id": request_id, "action_type": atype, "error": str(exc)[:1000]})))
        conn.commit()
        raise PrivilegedActionError(str(exc)) from exc


def recover_stale_executions(conn) -> int:
    """Never replay a privileged approval after an executor process disappears.

    The process may have crashed before or after browser I/O and the current
    schema deliberately does not guess which. A stale ``running`` record is
    therefore fenced into manual reconciliation and its one-shot approval is
    consumed. This can create a conservative false-positive review, but cannot
    duplicate an external side effect.
    """
    ttl = max(60, int(os.getenv("JOBOS_PRIVILEGED_ACTION_STALE_SECONDS", "600")))
    with conn.cursor() as cur:
        cur.execute(
            """WITH stale AS (
                   UPDATE privileged_action_executions e
                      SET status='needs_reconciliation', finished_at=now(),
                          error_message=coalesce(error_message, 'Executor lease/staleness timeout; reconcile before any new approval.')
                    WHERE e.status='running'
                      AND e.started_at < now() - make_interval(secs => %s)
                  RETURNING e.approval_request_id, e.application_id, e.action_type
               ), consumed AS (
                   UPDATE approval_requests ar
                      SET status='consumed', consumed_at=coalesce(consumed_at, now()),
                          consumed_by=coalesce(consumed_by, 'privileged-action-stale-reaper'),
                          action_note=coalesce(action_note, 'Stale privileged execution requires reconciliation; never replay this approval.')
                     FROM stale s
                    WHERE ar.id=s.approval_request_id AND ar.status='approved'
                  RETURNING ar.id
               )
               SELECT application_id, action_type, approval_request_id FROM stale;""",
            (ttl,),
        )
        rows = cur.fetchall()
        for application_id, action_type, approval_request_id in rows:
            cur.execute(
                """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                   VALUES (%s,'privileged_action_needs_reconciliation','privileged-action-stale-reaper',%s);""",
                (application_id, Jsonb({"approval_request_id": str(approval_request_id),
                                        "action_type": action_type, "reason": "stale_running_execution"})),
            )
    conn.commit()
    return len(rows)


def execute_next(conn) -> dict[str, Any] | None:
    recover_stale_executions(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT ar.id::text FROM approval_requests ar
                       WHERE ar.status='approved' AND ar.type = ANY(%s)
                         AND ar.token_expires_at > now()
                         AND NOT EXISTS (SELECT 1 FROM privileged_action_executions e WHERE e.approval_request_id=ar.id)
                       ORDER BY ar.responded_at NULLS LAST, ar.created_at LIMIT 1;""", (list(PRIVILEGED_TYPES),))
        row = cur.fetchone()
    return execute_one(conn, str(row[0])) if row else None


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS privileged human-approved application actions")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--application-id", required=True)
    prep.add_argument("--action", required=True, choices=["begin_application","trust_external_domain","create_employer_account","login_employer_account","use_email_verification","accept_terms","advance_application_step","auth_manual_retry","mfa_retry","checkpoint_retry","submit_application"])
    prep.add_argument("--candidate-id")
    run = sub.add_parser("execute"); run.add_argument("--request-id")
    sub.add_parser("once")
    worker = sub.add_parser("worker"); worker.add_argument("--poll-seconds", type=int, default=5)
    args = p.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        if args.command == "prepare":
            with conn.cursor() as cur:
                rid = prepare(cur, application_id=args.application_id, action=args.action, candidate_id=args.candidate_id)
            conn.commit(); print(json.dumps({"approval_request_id": rid, "action": args.action, "telegram": "pending"}, indent=2)); return 0
        if args.command == "execute":
            if not args.request_id: raise SystemExit("--request-id is required")
            print(json.dumps(execute_one(conn, args.request_id), indent=2, default=str)); return 0
        if args.command == "once":
            result = execute_next(conn)
            print(json.dumps(result or {"queue": "empty"}, indent=2, default=str)); return 0
        while True:
            try:
                result = execute_next(conn)
                if result:
                    print(json.dumps(result, default=str))
                else:
                    time.sleep(max(1, args.poll_seconds))
            except PrivilegedActionError as exc:
                print(f"privileged action refused: {exc}", file=sys.stderr)
                time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PrivilegedActionError, TransportError, VaultError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); raise SystemExit(1)
