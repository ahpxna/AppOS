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
import subprocess
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


def _host_is_allowed(cur, url: str, *, application_id: str | None = None, purpose: str | None = None) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    if not host:
        return False
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled=true;")
    if any(host == str(row[0]).casefold() or host.endswith("." + str(row[0]).casefold())
           for row in cur.fetchall()):
        return True
    if application_id:
        if purpose:
            cur.execute(
                """SELECT 1 FROM application_scoped_domain_trusts
                     WHERE application_id=%s AND domain=%s AND purpose=%s
                       AND enabled=true AND expires_at>now() LIMIT 1;""",
                (application_id, host, purpose),
            )
        else:
            cur.execute(
                """SELECT 1 FROM application_scoped_domain_trusts
                     WHERE application_id=%s AND domain=%s
                       AND enabled=true AND expires_at>now() LIMIT 1;""",
                (application_id, host),
            )
        return cur.fetchone() is not None
    return False


def _require_trusted_target(cur, url: str, *, application_id: str | None = None, purpose: str | None = None) -> None:
    if not _host_is_allowed(cur, url, application_id=application_id, purpose=purpose):
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


def _node_text(nodes: list[dict[str, Any]]) -> str:
    return " ".join(
        str(node.get("label") or node.get("value") or "")
        for node in nodes
    ).casefold()


def _application_form_evidence(nodes: list[dict[str, Any]]) -> bool:
    """Return whether controls look like an actual job-application form.

    Header/footer copy such as ``Sign in`` or ``protected by reCAPTCHA`` is
    common on ATS pages and must not outweigh the controls the user is really
    filling.  This deliberately uses only visible/control evidence and does not
    guess answers.
    """
    labels = _node_text(nodes)
    signals = (
        "first name", "last name", "phone", "resume", "résumé", "cover letter",
        "work authorization", "sponsorship", "linkedin", "portfolio", "experience",
        "education", "current company", "salary", "address", "submit application",
        "apply now", "apply for",
    )
    score = sum(1 for signal in signals if signal in labels)
    click_labels = {str(item.get("label") or "").strip().casefold() for item in _clickables(nodes)}
    if any(label in SUBMIT_LABELS or "submit application" in label for label in click_labels):
        return True
    return score >= 2


def _active_checkpoint_evidence(text: str, nodes: list[dict[str, Any]]) -> bool:
    node_text = _node_text(nodes)
    challenge_controls = (
        "i'm not a robot", "i am not a robot", "captcha challenge", "hcaptcha challenge",
        "recaptcha challenge", "verify you are human", "bot challenge", "arkose",
    )
    if any(marker in node_text for marker in challenge_controls):
        return True
    # Prose such as "background security check" on a real application form is
    # not a checkpoint. Raw challenge copy is considered only on a dedicated
    # non-form page.
    if _application_form_evidence(nodes):
        return False
    strong_page = ("verify you are human", "risk checkpoint", "bot challenge", "arkose")
    if any(marker in text for marker in strong_page):
        return True
    weak = any(marker in text for marker in ("recaptcha", "hcaptcha", "captcha"))
    footer_only = "protected by recaptcha" in text or "protected by hcaptcha" in text
    return weak and not footer_only

def detect_page_state(url: str, snapshot: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    text = f"{url}\n{snapshot.get('snapshot') or ''}".casefold()
    inputs = [n for n in nodes if n.get("ref") and str(n.get("role") or "").casefold() in INPUT_ROLES]
    app_form = _application_form_evidence(nodes)

    # =====================================================================
    # !!! HUMAN CHECKPOINT BOUNDARY — DO NOT MERGE WITH OTP/MFA APPROVAL !!!
    # Route only an *active* challenge, not a generic reCAPTCHA legal footer.
    # =====================================================================
    if _active_checkpoint_evidence(text, nodes):
        return "needs_human_checkpoint", {"reason": "active human checkpoint detected"}

    # Prose alone must not override an active application form. Verification
    # gates need a code/control signal or a dedicated non-form verification page.
    code_field = _find_input(nodes, ("code", "verification", "otp", "one-time"))
    email_verify = ("verify your email", "email verification", "code sent to your email",
                    "code sent to email", "check your email")
    email_path = any(token in (urlsplit(url).path or "").casefold() for token in ("verify", "verification", "confirm-email"))
    if any(marker in text for marker in email_verify) and (code_field is not None or not app_form):
        return "needs_email_verification", {"field_ref": str(code_field.get("ref")) if code_field else "NaN"}

    strong_mfa = ("authenticator", "security key", "passkey", "push notification", "approve the sign-in")
    strong_control_mfa = any(marker in _node_text(nodes) for marker in strong_mfa)
    sms_mfa = any(marker in text for marker in ("sms code", "texted you", "sent you a text")) and code_field is not None
    dedicated_mfa_page = (not app_form) and any(marker in text for marker in strong_mfa)
    if strong_control_mfa or sms_mfa or dedicated_mfa_page:
        return "needs_mfa", {"reason": "non-email MFA detected"}

    password = _find_input(nodes, ("password",))
    path = (urlsplit(url).path or "/").casefold()
    auth_path = any(token in path for token in ("/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up", "/account"))
    auth_copy = any(marker in text for marker in ("create account", "sign in", "log in", "register", "sign up"))
    if password or (auth_copy and (auth_path or not app_form)):
        if any(marker in text for marker in ("sign in with google", "continue with google", "sign in with microsoft", "continue with microsoft")):
            return "needs_manual_sso", {"reason": "browser SSO requires manual identity-provider session"}
        return "needs_account_auth", {"reason": "employer account authentication required"}

    if inputs or app_form:
        return "application_form_ready", {"input_count": len(inputs), "form_evidence": bool(app_form)}
    return "unknown", {"reason": "page state could not be classified"}


def _application_step(cur, application_id: str, *, for_update: bool = False) -> str:
    cur.execute(f"SELECT current_step FROM applications WHERE id=%s{' FOR UPDATE' if for_update else ''};", (application_id,))
    row = cur.fetchone()
    if not row:
        raise PrivilegedActionError("application not found")
    return str(row[0] or "")


def _require_application_step(cur, application_id: str, required: str) -> None:
    current = _application_step(cur, application_id)
    if current != required:
        raise PrivilegedActionError(f"application must be at {required!r}; current_step is {current!r}")


def _transition_application_step(cur, *, application_id: str, to_step: str, actor: str,
                                 reason: str, detail: dict[str, Any] | None = None,
                                 status: str | None = None) -> bool:
    current = _application_step(cur, application_id, for_update=True)
    if current == to_step:
        return False
    cur.execute("SELECT 1 FROM pipeline_transitions WHERE from_step=%s AND to_step=%s;", (current, to_step))
    if not cur.fetchone():
        raise PrivilegedActionError(f"illegal pipeline transition {current!r} -> {to_step!r}")
    if status is None:
        cur.execute("UPDATE applications SET current_step=%s, updated_at=now() WHERE id=%s AND current_step=%s;",
                    (to_step, application_id, current))
    else:
        cur.execute("UPDATE applications SET current_step=%s, status=%s, updated_at=now() WHERE id=%s AND current_step=%s;",
                    (to_step, status, application_id, current))
    if cur.rowcount != 1:
        raise PrivilegedActionError("application pipeline state changed concurrently")
    cur.execute(
        """INSERT INTO pipeline_events(application_id, from_step, to_step, actor, reason, detail_json)
           VALUES (%s,%s,%s,%s,%s,%s);""",
        (application_id, current, to_step, actor, reason, Jsonb(detail or {})),
    )
    return True


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
        target_step = step_map[state]
        current = _application_step(cur, application_id)
        if current != target_step:
            _transition_application_step(
                cur, application_id=application_id, to_step=target_step, actor="privileged-auth-state",
                reason=f"Observed employer auth/browser state: {state}.",
                detail={"url": url, "page_fingerprint": fingerprint, "platform": platform, "state": state},
            )
    if platform != "custom":
        cur.execute("SELECT 1 FROM ats_capabilities WHERE ats_type=%s;", (platform,))
        if cur.fetchone():
            cur.execute("UPDATE applications SET ats_type=%s, updated_at=now() WHERE id=%s;", (platform, application_id))


def _document_bindings(cur, application_id: str) -> dict[str, Any]:
    """Return current pointer-bound documents, including their JD provenance."""
    cur.execute(
        """SELECT gd.doc_type, gd.id::text, gda.id::text, gda.file_path, gda.filename, gda.sha256,
                  gd.source_jd_hash, a.jd_hash
             FROM applications a
             JOIN generated_document_artifacts gda
               ON gda.id IN (a.approved_resume_artifact_id, a.approved_cover_letter_artifact_id)
             JOIN generated_documents gd ON gd.id = gda.generated_document_id
            WHERE a.id=%s AND gda.application_id=a.id AND gd.application_id=a.id
              AND ((gd.doc_type='resume' AND gd.id=a.approved_resume_id AND gda.id=a.approved_resume_artifact_id)
                OR (gd.doc_type='cover_letter' AND gd.id=a.approved_cover_letter_id AND gda.id=a.approved_cover_letter_artifact_id));""",
        (application_id,),
    )
    return {str(kind): {"generated_document_id": doc_id, "artifact_id": artifact_id,
                        "file_path": path, "filename": filename, "sha256": sha,
                        "source_jd_hash": str(source_jd or ""), "application_jd_hash": str(app_jd or "")}
            for kind, doc_id, artifact_id, path, filename, sha, source_jd, app_jd in cur.fetchall()}


def _document_bindings_still_current(cur, application_id: str, approved: dict[str, Any]) -> bool:
    current = _document_bindings(cur, application_id)
    if current != approved:
        return False
    for item in current.values():
        if not item.get("source_jd_hash") or item.get("source_jd_hash") != item.get("application_jd_hash"):
            return False
    return _document_hashes_still_match(current)


def _consent_items(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only controls that can actually express legal consent.

    Links/buttons such as ``Privacy Policy`` are informational navigation, not
    consent. Optional marketing/newsletter/SMS permissions are also excluded
    from the legal blocker gate.
    """
    items: list[dict[str, Any]] = []
    marketing_words = ("marketing", "newsletter", "promotional", "offers", "product updates",
                       "receive text", "text message updates", "sms updates", "email updates")
    button_positive = ("accept", "i agree", "agree and", "agree &", "continue and agree",
                       "continue & agree", "accept and continue", "accept & continue")
    for n in nodes:
        ref = str(n.get("ref") or "")
        role = str(n.get("role") or "").casefold()
        label = str(n.get("label") or "").strip()
        low = " ".join(label.casefold().split())
        if not ref or role not in {"checkbox", "radio", "button"}:
            continue
        if any(word in low for word in marketing_words):
            continue
        if role in {"checkbox", "radio"}:
            if not any(word in low for word in CONSENT_WORDS):
                continue
            items.append({"ref": ref, "label": label, "role": role,
                          "selected": n.get("selected"), "required": bool(n.get("required"))})
            continue
        # Buttons have no selected state. Only an explicit affirmative action is
        # consent; informational buttons such as "Privacy Policy" are excluded.
        if any(phrase in low for phrase in button_positive):
            items.append({"ref": ref, "label": label, "role": role,
                          "selected": n.get("selected"), "required": bool(n.get("required"))})
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
        path = dest / f"{action}-{__import__('uuid').uuid4().hex}.png"
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
        _require_application_step(cur, application_id, "docs_verified")
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
        summary = f"Trust employer application domain {_origin(url)} for this application for a bounded time."
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
        _require_application_step(cur, application_id, "application_ready")
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
        _require_application_step(cur, application_id, "application_ready")
        control = _find_exact_control(nodes, SUBMIT_LABELS)
        field_digest, fields, blockers = _field_state(nodes)
        document_bindings = _document_bindings(cur, application_id)
        if not isinstance(document_bindings.get("resume"), dict):
            raise PrivilegedActionError("final Submit requires the current approved resume artifact pointer")
        if not _document_bindings_still_current(cur, application_id, document_bindings):
            raise PrivilegedActionError("approved resume/cover letter is stale against current pointers or JD")
        payload.update({"control_ref": control["ref"], "control_label": control["label"],
                        "field_state_sha256": field_digest, "required_blockers": blockers,
                        "review_context": {"screenshot_path": screenshot or "NaN", "write_actions": fields,
                                           "will_pause": blockers},
                        "document_bindings": document_bindings})
        atype = "privileged_submit_application"; summary = f"FINAL SUBMIT: click exact {control['label']!r} only after fresh revalidation."
    elif action == "use_email_verification":
        if not candidate_id:
            raise PrivilegedActionError("--candidate-id is required")
        cur.execute("""SELECT gmail_account, gmail_message_id, sender, subject, received_at, verification_kind, secret_sha256, secret_context_json
                       FROM email_verification_candidates WHERE id=%s AND application_id=%s AND status IN ('discovered','approved');""",
                    (candidate_id, application_id))
        row = cur.fetchone()
        if not row:
            raise PrivilegedActionError("verification candidate unavailable")
        field = _find_input(nodes, ("code", "verification", "otp", "one-time")) if row[5] == "numeric_code" else None
        button = None
        if row[5] == "numeric_code":
            try: button = _find_exact_control(nodes, VERIFY_LABELS)
            except PrivilegedActionError: button = None
        payload.update({"candidate_id": candidate_id, "gmail_account": row[0], "gmail_message_id": row[1], "sender": row[2] or "NaN",
                        "subject": row[3] or "NaN", "received_at": row[4].isoformat() if row[4] else "NaN",
                        "verification_kind": row[5], "secret_sha256": row[6], "secret_context": row[7] or {},
                        "field_ref": str(field.get('ref')) if field else "NaN",
                        "control_ref": button["ref"] if button else "NaN", "control_label": button["label"] if button else "NaN"})
        atype = "privileged_use_email_verification"; summary = f"Use exact Gmail verification {row[5]} from mailbox {row[0]} after Telegram approval; secret stays out of DB/Telegram."
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
    has_create = any(k in text for k in ("create account", "create an account", "register", "sign up"))
    has_login = any(k in text for k in ("sign in", "log in", "login"))
    actions = (["login_employer_account"] if has_login else []) + (["create_employer_account"] if has_create else [])
    if not actions:
        create_privileged_request(cur, application_id=application_id, action_type="privileged_auth_manual_retry",
                                  payload=base, summary="Employer auth page could not be classified safely. Complete login/register manually, then approve AUTH RETRY.",
                                  requested_by="auth-state-router")
        return

    materialized = 0
    for action in actions:
        try:
            account_payload, _ = _account_action_payload(cur, nodes, url, action=action)
        except Exception:
            continue
        if action == "create_employer_account" and account_payload.get("consent_blockers"):
            # Do not make terms a peer choice with LOGIN. First capture the
            # user's explicit CREATE ACCOUNT path selection with no browser I/O;
            # only after that decision do we materialize the separate legal gate.
            choose_payload = dict(base)
            choose_payload.update({
                "consent_items": account_payload.get("consent_items") or [],
                "create_path_choice": True,
            })
            create_privileged_request(
                cur, application_id=application_id,
                action_type="privileged_choose_create_employer_account_path",
                payload=choose_payload,
                summary="Choose the CREATE NEW EMPLOYER ACCOUNT path. This choice performs no browser write; any required terms/consent will be a separate approval next.",
                requested_by="auth-state-router",
            )
            materialized += 1
            continue
        atype = "privileged_create_employer_account" if action == "create_employer_account" else "privileged_login_employer_account"
        action_payload = dict(base); action_payload.update(account_payload)
        create_privileged_request(
            cur, application_id=application_id, action_type=atype, payload=action_payload,
            summary=("Use the existing employer-account LOGIN path." if action == "login_employer_account"
                     else "Use the CREATE NEW EMPLOYER ACCOUNT path."),
            requested_by="auth-state-router",
        )
        materialized += 1
    if materialized == 0:
        create_privileged_request(cur, application_id=application_id, action_type="privileged_auth_manual_retry",
                                  payload=base, summary="Employer auth controls are ambiguous. Complete this auth step manually, then approve AUTH RETRY.",
                                  requested_by="auth-state-router")


def _snapshot_text_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(str(snapshot.get("snapshot") or "").encode("utf-8")).hexdigest()


def _observable_page_change(*, before_target: str, before_url: str, before_snapshot: dict[str, Any],
                            after: dict[str, Any]) -> bool:
    """Return whether a post-I/O read proves a visible navigation/modal/page change."""
    if str(after.get("target_id") or "") != str(before_target):
        return True
    if canonical_page_url(str(after.get("url") or "")) != canonical_page_url(str(before_url)):
        return True
    return str(after.get("snapshot_sha256") or "") != _snapshot_text_sha256(before_snapshot)


def _consent_effect_verified(approved: list[dict[str, Any]], observed: list[dict[str, Any]],
                             *, page_changed: bool, observed_state: str = "") -> bool:
    """Verify legal consent from control state, never from navigation alone."""
    for item in approved:
        role = str(item.get("role") or ("checkbox" if "selected" in item else "")).casefold()
        if item.get("selected") is True:
            continue
        ref = str(item.get("ref") or "")
        label = " ".join(str(item.get("label") or "").casefold().split())
        match = next((candidate for candidate in observed
                      if str(candidate.get("ref") or "") == ref or
                         (label and " ".join(str(candidate.get("label") or "").casefold().split()) == label)), None)
        if role in {"checkbox", "radio"}:
            if match is None or match.get("selected") is not True:
                return False
            continue
        if role == "button":
            # A navigation to a Privacy Policy/help page must not count as
            # consent. Require the exact affirmative button to be gone, an
            # observable page change, and a recognizable next application/auth
            # state rather than arbitrary navigation.
            if match is not None or not page_changed or observed_state in {"", "unknown"}:
                return False
            continue
        return False
    return True

def _select_after_navigation_target(transport: OpenClawTransport, source_target: str,
                                    before_tabs: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, str]]]:
    """Choose the resulting page by opener/evidence; never silently fall back when several new tabs exist."""
    after_tabs = transport._tabs()
    before_ids = {transport._stable_id(t) for t in before_tabs}
    new_tabs = [t for t in after_tabs if transport._stable_id(t) and transport._stable_id(t) not in before_ids]
    if not new_tabs:
        return source_target, []
    if len(new_tabs) == 1:
        return transport._stable_id(new_tabs[0]), []

    opener_matches = [t for t in new_tabs if str(t.get("openerId") or "") == str(source_target)]
    if len(opener_matches) == 1:
        return transport._stable_id(opener_matches[0]), []

    candidates: list[dict[str, str]] = []
    plausible: list[str] = []
    for tab in new_tabs:
        tid = transport._stable_id(tab)
        if not tid:
            continue
        try:
            url, snap, nodes, fp = _snapshot(transport, tid)
            state, _detail = detect_page_state(url, snap, nodes)
        except Exception:
            continue
        candidates.append({"target_id": tid, "url": canonical_page_url(url), "page_fingerprint": fp, "state": state})
        if state != "unknown":
            plausible.append(tid)
    if len(plausible) == 1:
        return plausible[0], []
    return None, candidates


def _after_navigation(cur, transport: OpenClawTransport, application_id: str, source_target: str,
                      before_tabs: list[dict[str, Any]]) -> dict[str, Any]:
    time.sleep(1.5)
    target_id, ambiguous_candidates = _select_after_navigation_target(transport, source_target, before_tabs)
    if not target_id:
        return {
            "target_id": source_target, "state": "needs_human_checkpoint",
            "followup": "navigation_target_ambiguity", "browser_io": True,
            "navigation_candidates": ambiguous_candidates,
            "detail": {"reason": "multiple new browser tabs are plausible application targets"},
        }
    url, snap, nodes, fp = _snapshot(transport, target_id)
    platform = detect_platform(url, snap)
    state, detail = detect_page_state(url, snap, nodes)
    trusted = _host_is_allowed(cur, url, application_id=application_id)
    # Read-only observation of an untrusted redirect is allowed so the human
    # can see what needs trust. It must not mutate authoritative auth/pipeline
    # state until the separate trust capability is approved.
    if trusted:
        _update_auth_session(cur, application_id=application_id, url=url, fingerprint=fp,
                             state=state, platform=platform, detail={**detail, "target_id": target_id})

    # Do not package the next approval in the same transaction as an external
    # browser effect.  Return enough evidence for a post-commit best-effort
    # materializer instead.
    followup = "state_gate" if trusted else "trust_domain_required"
    return {"target_id": target_id, "url": url, "platform": platform, "state": state, "detail": detail,
            "followup": followup, "snapshot_sha256": _snapshot_text_sha256(snap),
            "page_fingerprint": fp, "consent_items": _consent_items(nodes)}


def _reconciliation_target_snapshot(transport: OpenClawTransport, payload: dict[str, Any], *,
                                    require_exact_page: bool = False) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], str]:
    """Resolve reconciliation evidence from the approval-bound target, never browser focus."""
    target_id = str(payload.get("target_id") or "")
    if target_id:
        try:
            url, snap, nodes, fp = _snapshot(transport, target_id)
            if require_exact_page:
                if canonical_page_url(url) != canonical_page_url(str(payload.get("expected_url") or "")):
                    raise PrivilegedActionError("reconciliation target left the exact approved page")
                if _origin(url) != str(payload.get("expected_origin") or ""):
                    raise PrivilegedActionError("reconciliation target origin changed")
                expected_fp = str(payload.get("expected_page_fingerprint") or "")
                if expected_fp and fp != expected_fp:
                    raise PrivilegedActionError("reconciliation target page fingerprint changed")
            return target_id, url, snap, nodes, fp
        except Exception as exc:
            exact_error = exc
    else:
        exact_error = PrivilegedActionError("approval has no exact target id")

    # Never fall back to the currently focused tab.  Recovery is allowed only
    # when one unique live page still matches the approval's bound URL+origin.
    expected_url = str(payload.get("expected_url") or "")
    expected_origin = str(payload.get("expected_origin") or "")
    candidates: list[tuple[str, str, dict[str, Any], list[dict[str, Any]], str]] = []
    try:
        for tab in transport._tabs():
            tid = transport._stable_id(tab)
            raw_url = str(tab.get("url") or "")
            if not tid or not raw_url:
                continue
            try:
                if canonical_page_url(raw_url) != canonical_page_url(expected_url) or _origin(raw_url) != expected_origin:
                    continue
                url, snap, nodes, fp = _snapshot(transport, tid)
                candidates.append((tid, url, snap, nodes, fp))
            except Exception:
                continue
    except Exception:
        candidates = []
    if len(candidates) == 1:
        return candidates[0]
    raise PrivilegedActionError(
        f"cannot safely recover the approval-bound browser target; exact target error={exact_error}; "
        f"unique bound-page candidates={len(candidates)}"
    )


def reconcile_observed_privileged_effect(cur, *, application_id: str, approval_request_id: str,
                                         action_type: str) -> dict[str, Any]:
    """Reconstruct state from exact approval-bound browser evidence without replay."""
    cur.execute(
        """SELECT payload_json FROM approval_requests
             WHERE id=%s AND application_id=%s;""",
        (approval_request_id, application_id),
    )
    row = cur.fetchone()
    if not row:
        raise PrivilegedActionError("reconciliation approval payload is unavailable")
    payload = dict(row[0] or {})

    if action_type == "privileged_submit_application":
        _transition_application_step(
            cur, application_id=application_id, to_step="submitted", actor="privileged-reconciliation",
            reason="Human confirmed the uncertain final Submit browser effect occurred.",
            detail={"approval_request_id": approval_request_id}, status="submitted",
        )
        cur.execute("UPDATE applications SET submitted_at=coalesce(submitted_at,now()), updated_at=now() WHERE id=%s;",
                    (application_id,))
        return {"submitted": True, "state": "submitted", "followup": "none"}

    transport = _transport()
    if action_type == "privileged_upload_document":
        target_id, url, snap, _nodes, fp = _reconciliation_target_snapshot(
            transport, payload, require_exact_page=True,
        )
        if not _upload_effect_verified(
            before_snapshot={"snapshot": ""}, after_snapshot=snap,
            field_ref=str(payload.get("field_ref") or ""), filename=str(payload.get("filename") or ""),
            allow_text_fallback=False,
        ):
            raise PrivilegedActionError(
                "human reported upload occurred, but the exact approved filename is not observable in the exact upload field"
            )
        return {"uploaded": True, "target_id": target_id, "url": url, "page_fingerprint": fp,
                "document_type": payload.get("document_type"), "followup": "none"}

    recoverable = {
        "privileged_begin_application", "privileged_advance_application_step",
        "privileged_create_employer_account", "privileged_login_employer_account",
        "privileged_accept_terms", "privileged_use_email_verification",
    }
    if action_type not in recoverable:
        return {"followup": "none", "note": "No browser state reconstruction is required for this action type."}

    observed_target, url, snap, nodes, fp = _reconciliation_target_snapshot(transport, payload)
    # For a navigation handoff, an unchanged source page is not evidence that
    # the external effect occurred. Leave reconciliation open rather than using
    # an arbitrary focused employer tab.
    if action_type == "privileged_begin_application" and canonical_page_url(url) == canonical_page_url(str(payload.get("expected_url") or "")):
        raise PrivilegedActionError("Apply handoff is not observable on the exact bound target; choose/refocus the resulting application tab")

    trusted = _host_is_allowed(cur, url, application_id=application_id)
    if action_type not in {"privileged_begin_application", "privileged_use_email_verification"} and not trusted:
        raise PrivilegedActionError("reconciliation target is not trusted for this application")

    if action_type == "privileged_begin_application" and _application_step(cur, application_id) == "docs_verified":
        _transition_application_step(
            cur, application_id=application_id, to_step="application_entrypoint_ready",
            actor="privileged-reconciliation",
            reason="Human confirmed the uncertain Apply/application-entrypoint effect occurred.",
            detail={"approval_request_id": approval_request_id, "target_id": observed_target},
        )

    platform = detect_platform(url, snap)
    state, detail = detect_page_state(url, snap, nodes)
    if trusted:
        _update_auth_session(
            cur, application_id=application_id, url=url, fingerprint=fp, state=state, platform=platform,
            detail={**detail, "target_id": observed_target, "reconciled_from": action_type},
        )
    followup = "state_gate" if trusted else "trust_domain_required"
    return {"target_id": observed_target, "url": url, "page_fingerprint": fp,
            "state": state, "platform": platform, "followup": followup}

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


def _confirmation(*, before_snapshot: dict[str, Any], before_url: str,
                  after_snapshot: dict[str, Any], after_url: str, submit_ref: str) -> bool:
    """Require multiple independent post-submit signals.

    Static instructional text containing words such as "application submitted"
    is not confirmation.  A marker must newly appear (or a known confirmation
    route must be reached) and the exact Submit control must disappear.
    """
    markers = ("application submitted", "thank you for applying", "thank you for your application",
               "we received your application", "application has been submitted", "submission successful")
    before_text = f"{before_url}\n{before_snapshot.get('snapshot') or ''}".casefold()
    after_text = f"{after_url}\n{after_snapshot.get('snapshot') or ''}".casefold()
    newly_appeared = any(marker in after_text and marker not in before_text for marker in markers)
    route_changed = canonical_page_url(after_url) != canonical_page_url(before_url)
    route_hint = any(token in urlsplit(after_url).path.casefold() for token in ("confirm", "thank", "submitted", "success", "complete"))
    after_nodes = parse_snapshot(after_snapshot)
    submit_still_present = any(str(node.get("ref") or "") == str(submit_ref) for node in after_nodes)
    if submit_still_present:
        return False
    snapshot_changed = _snapshot_text_sha256(after_snapshot) != _snapshot_text_sha256(before_snapshot)
    return (newly_appeared and (route_changed or snapshot_changed)) or (route_changed and route_hint)


def _upload_effect_verified(*, before_snapshot: dict[str, Any], after_snapshot: dict[str, Any],
                            field_ref: str, filename: str, allow_text_fallback: bool = True) -> bool:
    before_text = str(before_snapshot.get("snapshot") or "")
    after_text = str(after_snapshot.get("snapshot") or "")
    node = next((n for n in parse_snapshot(after_snapshot) if str(n.get("ref") or "") == field_ref), None)
    expected_name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if node:
        observed = str(node.get("value") or "").strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if observed:
            return bool(expected_name and observed == expected_name)
    if not allow_text_fallback:
        return False
    return bool(expected_name and expected_name in after_text.casefold() and expected_name not in before_text.casefold())

def _prepare_exact_application_ready_control(cur, *, application_id: str, action: str,
                                             control: dict[str, Any]) -> str:
    """Create one exact-bound application_ready capability for a human-selected control."""
    _require_application_step(cur, application_id, "application_ready")
    transport = _transport()
    target_id, url, _snapshot_payload, nodes, fingerprint = _base_binding(transport)
    _require_trusted_target(cur, url, application_id=application_id)
    live = next((candidate for candidate in _clickables(nodes)
                 if str(candidate.get("ref") or "") == str(control.get("ref") or "")
                 and str(candidate.get("label") or "") == str(control.get("label") or "")), None)
    if not live:
        raise PrivilegedActionError("candidate control changed while preparing ambiguity review")
    screenshot = _capture_review_screenshot(transport, application_id, action, target_id)
    field_digest, fields, blockers = _field_state(nodes)
    payload: dict[str, Any] = {
        "target_id": target_id, "expected_url": url,
        "expected_page_fingerprint": fingerprint, "expected_origin": _origin(url),
        "control_ref": live["ref"], "control_label": live["label"],
        "field_state_sha256": field_digest, "required_blockers": blockers,
        "review_context": {"screenshot_path": screenshot or "NaN",
                           "write_actions": fields, "will_pause": blockers,
                           "ambiguity_choice": {"action": action, "ref": live["ref"], "label": live["label"]}},
    }
    if action == "submit_application":
        document_bindings = _document_bindings(cur, application_id)
        if not isinstance(document_bindings.get("resume"), dict):
            raise PrivilegedActionError("final Submit requires the current approved resume artifact pointer")
        if not _document_bindings_still_current(cur, application_id, document_bindings):
            raise PrivilegedActionError("approved resume/cover letter is stale against current pointers or JD")
        payload["document_bindings"] = document_bindings
        atype = "privileged_submit_application"
        summary = f"AMBIGUITY CHOICE — FINAL SUBMIT: exact control {live['label']!r}."
    elif action == "advance_application_step":
        atype = "privileged_advance_application_step"
        summary = f"AMBIGUITY CHOICE — advance/review using exact control {live['label']!r}; final Submit remains separate."
    else:
        raise PrivilegedActionError(f"unsupported application-ready candidate action: {action}")
    return create_privileged_request(cur, application_id=application_id, action_type=atype,
                                     payload=payload, summary=summary, requested_by="application-ready-ambiguity")


def materialize_application_ready_gate(cur, application_id: str) -> list[str]:
    """Freshly inspect application_ready and materialize exact candidate gates.

    The function never clicks. If several Review/Next/Submit controls coexist,
    each exact control becomes its own Telegram approval so the human can choose.
    """
    _require_application_step(cur, application_id, "application_ready")
    transport = _transport()
    _target_id, url, _snapshot_payload, nodes, _fingerprint = _base_binding(transport)
    _require_trusted_target(cur, url, application_id=application_id)
    pending_consents = [item for item in _consent_items(nodes) if item.get("selected") is not True]
    if pending_consents:
        return [prepare(cur, application_id=application_id, action="accept_terms")]
    submit_matches = [c for c in _clickables(nodes) if c["label"].strip().casefold() in SUBMIT_LABELS]
    advance_matches = [c for c in _clickables(nodes) if c["label"].strip().casefold() in ADVANCE_LABELS]
    candidates = [("advance_application_step", item) for item in advance_matches] + [
        ("submit_application", item) for item in submit_matches
    ]
    if not candidates:
        return []
    if len(candidates) > 6:
        # Too many candidate controls is itself ambiguous; keep the human gate
        # open rather than flooding Telegram or guessing.
        return []
    if len(candidates) == 1:
        action, _control = candidates[0]
        return [prepare(cur, application_id=application_id, action=action)]
    approvals: list[str] = []
    cur.execute("SAVEPOINT application_ready_candidate_set")
    try:
        for action, control in candidates:
            approvals.append(_prepare_exact_application_ready_control(
                cur, application_id=application_id, action=action, control=control
            ))
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT application_ready_candidate_set")
        raise
    finally:
        cur.execute("RELEASE SAVEPOINT application_ready_candidate_set")
    return approvals


def _post_commit_followup(conn, application_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort packaging after the external effect is already durable."""
    state = str(result.get("state") or "")
    target_id = str(result.get("target_id") or "")
    transport = _transport()
    if result.get("followup") == "navigation_target_ambiguity":
        with conn.cursor() as cur:
            created = []
            for candidate in result.get("navigation_candidates") or []:
                if not isinstance(candidate, dict) or not candidate.get("target_id") or not candidate.get("url"):
                    continue
                rid = create_privileged_request(
                    cur, application_id=application_id, action_type="privileged_choose_navigation_target",
                    payload={
                        "target_id": candidate["target_id"], "expected_url": candidate["url"],
                        "expected_page_fingerprint": candidate.get("page_fingerprint") or "",
                        "expected_origin": _origin(candidate["url"]), "navigation_target_choice": True,
                        "review_context": {"candidate_state": candidate.get("state") or "unknown"},
                    },
                    summary=f"Choose this exact resulting application tab: {candidate['url']}",
                    requested_by="navigation-target-ambiguity",
                )
                created.append(str(rid))
            conn.commit()
            return {"kind": "navigation_target_ambiguity", "approval_request_ids": created}
    if not target_id:
        return None
    url, _snap, nodes, fp = _snapshot(transport, target_id)
    with conn.cursor() as cur:
        if result.get("followup") == "create_path_terms_required":
            items = result.get("consent_items") if isinstance(result.get("consent_items"), list) else []
            if not items:
                return {"kind": "create_path_choice", "ok": False, "detail": "No live consent controls remained; resync auth state."}
            rid = create_privileged_request(
                cur, application_id=application_id, action_type="privileged_accept_terms",
                payload={"target_id": target_id, "expected_url": canonical_page_url(url),
                         "expected_page_fingerprint": fp, "expected_origin": _origin(url),
                         "consent_items": items,
                         "review_context": {"screenshot_path": _capture_review_screenshot(transport, application_id, "create-path-terms", target_id) or "NaN"}},
                summary="CREATE ACCOUNT path selected. Approve the exact required terms/consent controls before a fresh create-account action is offered.",
                requested_by="create-account-path-choice",
            )
            conn.commit()
            return {"kind": "create_path_terms", "approval_request_id": rid}
        # Trust is the first follow-up gate. Never try to prepare autofill or
        # commit observed auth state on an untrusted employer redirect.
        if result.get("followup") == "trust_domain_required" or not _host_is_allowed(cur, url, application_id=application_id):
            host = (urlsplit(url).hostname or "").casefold()
            if not host:
                return None
            rid = create_privileged_request(
                cur, application_id=application_id, action_type="privileged_trust_external_domain",
                payload={"target_id": target_id, "expected_url": canonical_page_url(url),
                         "expected_page_fingerprint": fp, "expected_origin": _origin(url), "domain": host,
                         "review_context": {"screenshot_path": _capture_review_screenshot(transport, application_id, "trust-domain", target_id) or "NaN"}},
                summary=f"Trust newly discovered employer application domain {host} for this application for a bounded time.",
                requested_by="application-handoff",
            )
            conn.commit()
            return {"kind": "trust_domain", "approval_request_id": rid}
        if state == "application_form_ready":
            # The trusted state was already committed by the privileged
            # executor. Materialize the next human autofill package only now.
            command = [sys.executable, str(ROOT / "scripts" / "jobos.py"), "autofill", "prepare",
                       "--application-id", application_id, "--create", "--yes"]
            proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=150)
            return {"kind": "autofill_prepare", "ok": proc.returncode == 0,
                    "detail": (proc.stdout or proc.stderr or "")[-1200:]}
        before_count = 0
        cur.execute("SELECT count(*) FROM approval_requests WHERE application_id=%s AND status='pending';", (application_id,))
        before_count = int(cur.fetchone()[0])
        _enqueue_state_followup(cur, transport, application_id=application_id, target_id=target_id,
                                url=url, fingerprint=fp, nodes=nodes, state=state)
        cur.execute("SELECT count(*) FROM approval_requests WHERE application_id=%s AND status='pending';", (application_id,))
        after_count = int(cur.fetchone()[0])
        conn.commit()
        return {"kind": "state_gate", "created": after_count > before_count, "state": state}


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
        if atype == "privileged_upload_document" and isinstance(payload, dict) and payload.get("delegated_to_autofill") is True:
            raise PrivilegedActionError("delegated upload approvals execute only inside their exact parent autofill session")
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
            cur.execute("SELECT coalesce(job_url,''), coalesce(jd_hash,''), current_step FROM applications WHERE id=%s FOR UPDATE;", (app_id,))
            current_app = cur.fetchone()
            if not current_app:
                raise PrivilegedActionError("application no longer exists")
            if str(payload.get("application_id") or "") != str(app_id):
                raise PrivilegedActionError("approval application binding changed")
            if str(payload.get("job_url") or "") != str(current_app[0] or ""):
                raise PrivilegedActionError("application job URL changed after approval")
            if str(payload.get("jd_hash") or "") != str(current_app[1] or ""):
                raise PrivilegedActionError("application JD changed after approval")
            if str(payload.get("expected_application_step") or "") != str(current_app[2] or ""):
                raise PrivilegedActionError("application pipeline step changed after approval")
            if atype == "privileged_choose_navigation_target":
                live_url, live_snap, live_nodes, live_fp = _revalidate(transport, dict(payload or {}))
                platform = detect_platform(live_url, live_snap)
                state, detail = detect_page_state(live_url, live_snap, live_nodes)
                trusted = _host_is_allowed(cur, live_url, application_id=app_id)
                if trusted:
                    _update_auth_session(
                        cur, application_id=app_id, url=live_url, fingerprint=live_fp,
                        state=state, platform=platform,
                        detail={**detail, "target_id": str(payload.get("target_id") or ""),
                                "selected_from_navigation_ambiguity": True},
                    )
                result = {
                    "target_id": str(payload.get("target_id") or ""), "url": live_url,
                    "page_fingerprint": live_fp, "state": state, "platform": platform,
                    "followup": "state_gate" if trusted else "trust_domain_required",
                    "browser_io": False,
                }
            elif atype == "privileged_choose_create_employer_account_path":
                live_url, live_snap, live_nodes, live_fp = _revalidate(transport, dict(payload or {}))
                _require_trusted_target(cur, live_url, application_id=app_id)
                live_consents = [item for item in _consent_items(live_nodes) if item.get("selected") is not True]
                if not live_consents:
                    raise PrivilegedActionError("create-account path no longer has the reviewed consent gate; resync auth state")
                result = {
                    "target_id": str(payload.get("target_id") or ""), "url": live_url,
                    "page_fingerprint": live_fp, "state": "needs_account_auth",
                    "followup": "create_path_terms_required", "consent_items": live_consents,
                    "browser_io": False,
                }
            elif atype == "privileged_trust_external_domain":
                domain = str(payload.get("domain") or "").casefold()
                if payload.get("trust_source") == "gmail_magic_link":
                    cur.execute(
                        """SELECT gmail_account, gmail_message_id, verification_kind, secret_sha256, secret_context_json
                             FROM email_verification_candidates
                            WHERE id=%s AND application_id=%s AND status IN ('discovered','approved');""",
                        (payload.get("candidate_id"), app_id),
                    )
                    cand = cur.fetchone()
                    if not cand or cand[2] != "magic_link":
                        raise PrivilegedActionError("email magic-link trust candidate is unavailable")
                    if str(cand[3]) != str(payload.get("secret_sha256") or "") or str(cand[0]) != str(payload.get("gmail_account") or cand[0]):
                        raise PrivilegedActionError("email magic-link trust hash changed")
                    if json.dumps(cand[4] or {}, sort_keys=True, separators=(",", ":"), default=str) != json.dumps(payload.get("secret_context") or {}, sort_keys=True, separators=(",", ":"), default=str):
                        raise PrivilegedActionError("email magic-link trust context changed")
                    secret = refetch_secret({
                        "gmail_account": cand[0], "gmail_message_id": cand[1], "verification_kind": cand[2],
                        "secret_sha256": cand[3], "secret_context": cand[4] or {},
                    })
                    observed_domain = (urlsplit(secret).hostname or "").casefold()
                    if not domain or domain != observed_domain:
                        raise PrivilegedActionError("email magic-link domain does not match the approved trust gate")
                    ttl_minutes = max(15, min(60, int(os.getenv("JOBOS_MAGIC_LINK_TRUST_TTL_MINUTES", "30"))))
                    cur.execute(
                        """UPDATE application_scoped_domain_trusts
                              SET enabled=true, expires_at=now()+make_interval(mins => %s),
                                  approval_request_id=%s, updated_at=now()
                            WHERE application_id=%s AND domain=%s AND purpose='gmail_magic_link';""",
                        (ttl_minutes, request_id, app_id, domain),
                    )
                    if cur.rowcount == 0:
                        cur.execute(
                            """INSERT INTO application_scoped_domain_trusts(
                                   application_id, domain, purpose, expires_at, approval_request_id, enabled)
                               VALUES (%s,%s,'gmail_magic_link',now()+make_interval(mins => %s),%s,true);""",
                            (app_id, domain, ttl_minutes, request_id),
                        )
                    secret = ""
                    result = {"trusted_domain": domain, "trust_source": "gmail_magic_link",
                              "scope": "application", "purpose": "gmail_magic_link", "ttl_minutes": ttl_minutes}
                else:
                    live_url, live_snap, live_nodes, live_fp = _revalidate(transport, dict(payload or {}))
                    if not domain or domain != (urlsplit(str(payload.get("expected_url") or "")).hostname or "").casefold():
                        raise PrivilegedActionError("domain binding invalid")
                    if domain != (urlsplit(live_url).hostname or "").casefold():
                        raise PrivilegedActionError("browser target no longer belongs to the approved employer domain")
                    ttl_minutes = max(15, min(10080, int(os.getenv("JOBOS_EMPLOYER_TRUST_TTL_MINUTES", "1440"))))
                    cur.execute(
                        """UPDATE application_scoped_domain_trusts
                              SET enabled=true, expires_at=now()+make_interval(mins => %s),
                                  approval_request_id=%s, updated_at=now()
                            WHERE application_id=%s AND domain=%s AND purpose='employer_handoff';""",
                        (ttl_minutes, request_id, app_id, domain),
                    )
                    if cur.rowcount == 0:
                        cur.execute(
                            """INSERT INTO application_scoped_domain_trusts(
                                   application_id, domain, purpose, expires_at, approval_request_id, enabled)
                               VALUES (%s,%s,'employer_handoff',now()+make_interval(mins => %s),%s,true);""",
                            (app_id, domain, ttl_minutes, request_id),
                        )
                    platform = detect_platform(live_url, live_snap)
                    state, detail = detect_page_state(live_url, live_snap, live_nodes)
                    _update_auth_session(cur, application_id=app_id, url=live_url, fingerprint=live_fp,
                                         state=state, platform=platform, detail={**detail, "target_id": str(payload.get("target_id") or "")})
                    result = {"trusted_domain": domain, "target_id": str(payload.get("target_id") or ""),
                              "url": live_url, "page_fingerprint": live_fp, "state": state,
                              "platform": platform, "followup": "state_gate",
                              "scope": "application", "purpose": "employer_handoff", "ttl_minutes": ttl_minutes}
            elif atype in {"privileged_auth_manual_retry", "privileged_mfa_retry", "privileged_checkpoint_retry"}:
                # Retry capabilities are intentionally read-only: after the human
                # handles MFA/checkpoint, JobOS only takes a fresh snapshot and
                # classifies what is next.
                target_id = str(payload.get("target_id")); url = transport.current_url(target_id)
                _require_trusted_target(cur, url, application_id=app_id)
                snap = transport.snapshot(target_id); nodes = parse_snapshot(snap); fp = page_fingerprint(snap, page_url=url)
                platform = detect_platform(url, snap); state, detail = detect_page_state(url, snap, nodes)
                _update_auth_session(cur, application_id=app_id, url=url, fingerprint=fp, state=state, platform=platform, detail={**detail, "target_id": target_id})
                result = {"target_id": target_id, "url": url, "page_fingerprint": fp,
                          "state": state, "platform": platform, "followup": "state_gate"}
            else:
                url, snap, nodes, fp = _revalidate(transport, payload)
                target_id = str(payload["target_id"])
                if atype != "privileged_begin_application":
                    _require_trusted_target(cur, url, application_id=app_id)
                before_tabs = transport._tabs()
                if atype == "privileged_begin_application":
                    _require_application_step(cur, app_id, "docs_verified")
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    _transition_application_step(
                        cur, application_id=app_id, to_step="application_entrypoint_ready",
                        actor="privileged-action-executor",
                        reason="Human-approved application entrypoint was opened.",
                        detail={"approval_request_id": request_id, "target_id": target_id},
                    )
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    if not _observable_page_change(before_target=target_id, before_url=url, before_snapshot=snap, after=result):
                        raise PrivilegedActionError("Apply handoff click produced no observable navigation or modal change")
                elif atype in {"privileged_create_employer_account", "privileged_login_employer_account"}:
                    if atype == "privileged_create_employer_account" and payload.get("consent_blockers"):
                        raise PrivilegedActionError("account registration has unapproved terms/consent controls; approve those separately first")
                    resolved_plan = [(item, _resolve_plan_value(cur, item)) for item in (payload.get("field_plan") or [])]
                    for item, value in resolved_plan:
                        io_started = True; _fill(transport, target_id, str(item["ref"]), value)
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    if not _observable_page_change(before_target=target_id, before_url=url, before_snapshot=snap, after=result):
                        raise PrivilegedActionError("employer account action produced no observable browser change")
                    if payload.get("account_email") and payload.get("account_email") != "NaN":
                        cur.execute("UPDATE application_auth_sessions SET account_email=%s, updated_at=now() WHERE application_id=%s;",
                                    (payload.get("account_email"), app_id))
                elif atype == "privileged_accept_terms":
                    for item in payload.get("consent_items") or []:
                        if item.get("selected") is not True:
                            io_started = True; _click(transport, target_id, str(item["ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    changed = _observable_page_change(before_target=target_id, before_url=url, before_snapshot=snap, after=result)
                    if not _consent_effect_verified(payload.get("consent_items") or [], result.get("consent_items") or [],
                                                    page_changed=changed, observed_state=str(result.get("state") or "")):
                        raise PrivilegedActionError("approved consent controls were not observably accepted after browser I/O")
                    result["consent_items"] = payload.get("consent_items") or []
                elif atype == "privileged_advance_application_step":
                    _require_application_step(cur, app_id, "application_ready")
                    if payload.get("required_blockers"):
                        raise PrivilegedActionError("required form blockers existed in the approved wizard-step package")
                    current_digest, _fields, blockers = _field_state(nodes)
                    if blockers or current_digest != payload.get("field_state_sha256"):
                        raise PrivilegedActionError("form fields changed after application-step approval")
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    if not _observable_page_change(before_target=target_id, before_url=url, before_snapshot=snap, after=result):
                        raise PrivilegedActionError("application wizard step click produced no observable page change")
                elif atype == "privileged_use_email_verification":
                    cur.execute("""SELECT gmail_account, gmail_message_id, verification_kind, secret_sha256, secret_context_json
                                   FROM email_verification_candidates WHERE id=%s AND application_id=%s AND status IN ('approved','discovered');""",
                                (payload.get("candidate_id"), app_id))
                    cand = cur.fetchone()
                    if not cand:
                        raise PrivilegedActionError("verification candidate unavailable")
                    if str(cand[0]) != str(payload.get("gmail_account") or "") or str(cand[1]) != str(payload.get("gmail_message_id") or ""):
                        raise PrivilegedActionError("verification candidate mailbox/message binding changed after approval")
                    if str(cand[3]) != str(payload.get("secret_sha256") or "") or str(cand[2]) != str(payload.get("verification_kind") or ""):
                        raise PrivilegedActionError("verification candidate kind/hash changed after approval")
                    if json.dumps(cand[4] or {}, sort_keys=True, separators=(",", ":"), default=str) != json.dumps(payload.get("secret_context") or {}, sort_keys=True, separators=(",", ":"), default=str):
                        raise PrivilegedActionError("verification candidate context changed after approval")
                    secret = refetch_secret({"gmail_account": cand[0], "gmail_message_id": cand[1], "verification_kind": cand[2],
                                              "secret_sha256": cand[3], "secret_context": cand[4] or {}})
                    if cand[2] == "numeric_code":
                        ref = str(payload.get("field_ref") or "")
                        if not ref or ref == "NaN": raise PrivilegedActionError("verification code field was not bound")
                        io_started = True; _fill(transport, target_id, ref, secret)
                        control = str(payload.get("control_ref") or "")
                        if control and control != "NaN": io_started = True; _click(transport, target_id, control)
                        result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    else:
                        _require_trusted_target(cur, secret, application_id=app_id, purpose="gmail_magic_link")
                        io_started = True; transport._run(["open", secret])
                        result = _after_navigation(cur, transport, app_id, target_id, before_tabs)
                    if not _observable_page_change(before_target=target_id, before_url=url, before_snapshot=snap, after=result):
                        raise PrivilegedActionError("email verification browser I/O produced no observable page change")
                    cur.execute("UPDATE email_verification_candidates SET status='consumed', consumed_at=now() WHERE id=%s;", (payload.get("candidate_id"),))
                elif atype == "privileged_upload_document":
                    doc_type = str(payload.get("document_type") or "")
                    current = _document_bindings(cur, app_id).get(doc_type)
                    approved = {key: payload.get(key) for key in (
                        "generated_document_id", "artifact_id", "file_path", "filename", "sha256",
                        "source_jd_hash", "application_jd_hash"
                    )}
                    if not current or current != approved:
                        raise PrivilegedActionError("approved upload document pointer/JD binding changed")
                    if approved.get("source_jd_hash") != approved.get("application_jd_hash"):
                        raise PrivilegedActionError("approved upload document is stale against the current JD")
                    path = Path(str(approved.get("file_path") or "")).expanduser().resolve()
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != str(approved.get("sha256") or ""):
                        raise PrivilegedActionError("approved upload artifact bytes changed or are missing")
                    field_ref = str(payload.get("field_ref") or "")
                    field_label = " ".join(str(payload.get("field_label") or "").casefold().split())
                    live_field = next((n for n in nodes if str(n.get("ref") or "") == field_ref), None)
                    if not live_field or (field_label and " ".join(str(live_field.get("label") or "").casefold().split()) != field_label):
                        raise PrivilegedActionError("approved upload field identity changed")
                    upload_transport = OpenClawTransport(
                        binary=resolve_openclaw_binary(required=True),
                        profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"), timeout=90,
                        approved_upload_hashes={str(path): str(approved["sha256"])},
                    )
                    io_started = True
                    upload_transport.execute(target_id, {"action": "upload", "target": field_ref, "value": str(path)})
                    time.sleep(0.8)
                    observed = upload_transport.snapshot(target_id)
                    if not _upload_effect_verified(before_snapshot=snap, after_snapshot=observed,
                                                   field_ref=field_ref, filename=str(approved.get("filename") or "")):
                        raise PrivilegedActionError("document upload occurred but the exact field effect could not be verified")
                    result = {"uploaded": True, "document_type": doc_type, "artifact_id": approved.get("artifact_id"),
                              "filename": approved.get("filename"), "target_id": target_id, "field_ref": field_ref}
                elif atype == "privileged_submit_application":
                    _require_application_step(cur, app_id, "application_ready")
                    if payload.get("required_blockers"):
                        raise PrivilegedActionError("required form blockers existed in the approved review package")
                    current_digest, _fields, blockers = _field_state(nodes)
                    if blockers or current_digest != payload.get("field_state_sha256"):
                        raise PrivilegedActionError("form fields changed after final-submit approval")
                    bindings = payload.get("document_bindings") or {}
                    if not isinstance(bindings.get("resume"), dict):
                        raise PrivilegedActionError("final Submit requires an exact approved resume artifact binding")
                    if not _document_bindings_still_current(cur, app_id, bindings):
                        raise PrivilegedActionError("approved resume/cover artifact pointer, JD provenance, or bytes changed")
                    io_started = True; _click(transport, target_id, str(payload["control_ref"]))
                    time.sleep(2.0)
                    observed_url = transport.current_url(target_id)
                    observed = transport.snapshot(target_id)
                    if not _confirmation(before_snapshot=snap, before_url=url, after_snapshot=observed,
                                         after_url=observed_url, submit_ref=str(payload["control_ref"])):
                        raise PrivilegedActionError("Submit was clicked but confirmation is uncertain; reconcile manually before any retry")
                    _transition_application_step(
                        cur, application_id=app_id, to_step="submitted", actor="privileged-action-executor",
                        reason="Final Submit produced multi-signal confirmation.",
                        detail={"approval_request_id": request_id, "confirmation_url": observed_url}, status="submitted",
                    )
                    cur.execute("UPDATE applications SET submitted_at=coalesce(submitted_at, now()), updated_at=now() WHERE id=%s;", (app_id,))
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
        conn.commit()
        followup = None
        try:
            if isinstance(result, dict) and result.get("state"):
                followup = _post_commit_followup(conn, app_id, result)
        except Exception as followup_exc:
            # External effect + execution record are already durable. Follow-up
            # packaging may fail softly and must never turn success into replay.
            followup = {"ok": False, "error": str(followup_exc)[:1000]}
        return {"ok": True, "request_id": request_id, "action_type": atype,
                "result": result, "followup": followup}
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
                         AND NOT (ar.type='privileged_upload_document' AND COALESCE(ar.payload_json->>'delegated_to_autofill','false')='true')
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
