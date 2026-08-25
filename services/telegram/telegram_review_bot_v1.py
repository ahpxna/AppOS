#!/usr/bin/env python3
"""Telegram long-polling adapter for the JobOS Human Review Hub.

No webhook/public server is required. Callback buttons contain only short,
opaque single-use tokens; every decision is revalidated by Review Hub and the
canonical approval service before any state change.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env
from services.review.review_service_v1 import ReviewError, answer_question, decide_item, review_artifacts, sync_inbox
from services.review.approval_context_v1 import NAN, build_envelope, context_files, snapshot_context

load_repo_env()
DSN = database_dsn()
BOT_KEY = "review_bot"


class TelegramError(RuntimeError):
    pass


def _bot_token() -> str:
    token = (os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise TelegramError("Set JOBOS_TELEGRAM_BOT_TOKEN in .env.")
    return token


def _required_env() -> tuple[str, int, int]:
    token = _bot_token()
    user = (os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip()
    chat = (os.getenv("JOBOS_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or user).strip()
    if not user or not chat:
        raise TelegramError("Set JOBOS_TELEGRAM_ALLOWED_USER_ID and JOBOS_TELEGRAM_CHAT_ID in .env.")
    try:
        return token, int(user), int(chat)
    except ValueError as exc:
        raise TelegramError("Telegram user/chat ids must be integers.") from exc


def api(token: str, method: str, *, data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None, timeout: int = 70) -> dict[str, Any]:
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                                 data=data, files=files, timeout=timeout)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(f"Telegram {method} request failed: {exc}") from exc
    if not response.ok or not payload.get("ok"):
        raise TelegramError(f"Telegram {method} failed: {payload.get('description') or response.text[:300]}")
    return payload


def discover_ids(token: str) -> list[dict[str, int | str]]:
    payload = api(token, "getUpdates", data={"timeout": "0", "allowed_updates": json.dumps(["message"])}, timeout=15)
    seen: set[tuple[int, int]] = set()
    result: list[dict[str, int | str]] = []
    for update in payload.get("result") or []:
        message = update.get("message") or {}
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        user_id, chat_id = int(sender.get("id") or 0), int(chat.get("id") or 0)
        if not user_id or not chat_id or (user_id, chat_id) in seen:
            continue
        seen.add((user_id, chat_id))
        result.append({"user_id": user_id, "chat_id": chat_id,
                       "username": str(sender.get("username") or ""),
                       "chat_type": str(chat.get("type") or "")})
    return result


def _callback_token(cur, item_id: str, action: str, allowed_user_id: int, ttl_hours: int = 24) -> str:
    raw = secrets.token_urlsafe(18)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO telegram_callback_tokens(
               review_item_id, token_sha256, action, allowed_user_id, expires_at)
           VALUES (%s, %s, %s, %s, now() + make_interval(hours => %s));""",
        (item_id, digest, action, allowed_user_id, ttl_hours),
    )
    return raw


def _keyboard(cur, item_id: str, allowed_user_id: int, item_type: str,
              payload: dict[str, Any]) -> str | None:
    if item_type in {"question_required", "application_ready"}:
        return None
    if item_type == "autofill_review" and (
            payload.get("execution_state") != "completed" or not payload.get("screenshot_sha256")):
        revise = _callback_token(cur, item_id, "revise", allowed_user_id)
        reject = _callback_token(cur, item_id, "reject", allowed_user_id)
        return json.dumps({"inline_keyboard": [[
            {"text": "📝 Review / prepare again", "callback_data": f"rv:{revise}"},
            {"text": "❌ Reject", "callback_data": f"rv:{reject}"},
        ]]}, separators=(",", ":"))
    if item_type == "document_review" and payload.get("qa_status") != "pass":
        revise = _callback_token(cur, item_id, "revise", allowed_user_id)
        reject = _callback_token(cur, item_id, "reject", allowed_user_id)
        return json.dumps({"inline_keyboard": [[
            {"text": "📝 Revise / regenerate", "callback_data": f"rv:{revise}"},
            {"text": "❌ Reject", "callback_data": f"rv:{reject}"},
        ]]}, separators=(",", ":"))
    approval_type = str(payload.get("approval_type") or "")
    approve_labels = {
        "privileged_begin_application": "✅ OPEN APPLY",
        "privileged_trust_external_domain": "✅ TRUST DOMAIN",
        "privileged_create_employer_account": "✅ CREATE ACCOUNT",
        "privileged_login_employer_account": "✅ LOGIN",
        "privileged_use_email_verification": "✅ USE EMAIL VERIFICATION",
        "privileged_accept_terms": "✅ ACCEPT TERMS",
        "privileged_advance_application_step": "✅ NEXT / CONTINUE",
        "privileged_auth_manual_retry": "🔁 AUTH RETRY",
        "privileged_mfa_retry": "🔁 RETRY AFTER MFA",
        "privileged_checkpoint_retry": "🔁 RETRY AFTER I FINISH",
        "privileged_submit_application": "✅ APPROVE SUBMIT",
    }
    approve = _callback_token(cur, item_id, "approve", allowed_user_id)
    reject = _callback_token(cur, item_id, "reject", allowed_user_id)
    if approval_type in {"privileged_auth_manual_retry", "privileged_mfa_retry", "privileged_checkpoint_retry"}:
        return json.dumps({"inline_keyboard": [[
            {"text": approve_labels[approval_type], "callback_data": f"rv:{approve}"},
            {"text": "❌ CANCEL", "callback_data": f"rv:{reject}"},
        ]]}, separators=(",", ":"))
    revise = _callback_token(cur, item_id, "revise", allowed_user_id)
    return json.dumps({"inline_keyboard": [
        [{"text": approve_labels.get(approval_type, "✅ Approve"), "callback_data": f"rv:{approve}"}],
        [{"text": "📝 Revise", "callback_data": f"rv:{revise}"},
         {"text": "❌ Reject", "callback_data": f"rv:{reject}"}],
    ]}, separators=(",", ":"))


def _compact(value: Any, limit: int = 120) -> str:
    if value is None:
        return NAN
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _message_text(row: tuple[Any, ...], envelope: dict[str, Any] | None = None,
                  diff: dict[str, Any] | None = None) -> str:
    item_id, item_type, priority, title, summary, company, role, payload = row
    payload = payload or {}
    envelope = envelope if isinstance(envelope, dict) else {}
    job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    approval = envelope.get("approval") if isinstance(envelope.get("approval"), dict) else {}
    browser = envelope.get("browser") if isinstance(envelope.get("browser"), dict) else {}
    fit = envelope.get("fit") if isinstance(envelope.get("fit"), dict) else {}
    documents = envelope.get("documents") if isinstance(envelope.get("documents"), dict) else {}
    form = envelope.get("form") if isinstance(envelope.get("form"), dict) else {}
    auth = envelope.get("auth") if isinstance(envelope.get("auth"), dict) else {}
    action_type = approval.get("action_type") or payload.get("approval_type") or item_type
    lines = ["🧭 JobOS Human Approval", f"{company or NAN} — {role or NAN}", f"Action: {action_type}", "", str(title)]
    if summary:
        lines.extend(["", str(summary)])
    lines.extend([
        "", "JOB",
        f"Location: {_compact(job.get('location', NAN))} | Work mode: {_compact(job.get('work_mode', NAN))}",
        f"Fit: {_compact(fit.get('fit_score', job.get('fit_score', NAN)))} / {_compact(fit.get('fit_decision', job.get('fit_decision', NAN)))}",
        f"Fit reason: {_compact(fit.get('decision_reason', NAN), 260)}",
        f"Matched: {_compact(fit.get('matched_requirements', NAN), 260)}",
        f"Missing/weak: {_compact(fit.get('missing_or_weak_requirements', NAN), 260)}",
        f"Hard blockers: {_compact(fit.get('hard_blockers', NAN), 180)}",
        f"JD: {_compact(job.get('job_url', NAN), 180)}",
        "", "BROWSER",
        f"URL: {_compact(browser.get('target_url', auth.get('current_url', NAN)), 180)}",
        f"Auth state: {_compact(auth.get('state', NAN))} | Platform: {_compact(auth.get('platform_hint', NAN))}",
    ])
    resume = documents.get("resume") if isinstance(documents, dict) else NAN
    cover = documents.get("cover_letter") if isinstance(documents, dict) else NAN
    if isinstance(resume, dict):
        resume_text = f"{resume.get('filename', NAN)} [{str(resume.get('sha256', NAN))[:12]}]"
    else:
        resume_text = NAN
    if isinstance(cover, dict):
        cover_text = f"{cover.get('filename', NAN)} [{str(cover.get('sha256', NAN))[:12]}]"
    else:
        cover_text = NAN
    lines.extend(["", "DOCUMENTS", f"Resume: {resume_text}", f"Cover letter: {cover_text}"])
    proposed = form.get("proposed_fields", NAN) if isinstance(form, dict) else NAN
    paused = form.get("paused_fields", NAN) if isinstance(form, dict) else NAN
    blockers = form.get("required_blockers", NAN) if isinstance(form, dict) else NAN
    lines.extend(["", "FORM", f"Fields: {_compact(proposed, 500)}", f"Paused: {_compact(paused, 240)}", f"Required blockers: {_compact(blockers, 240)}"])
    if diff:
        changes = diff.get("changed") if isinstance(diff.get("changed"), list) else []
        lines.extend(["", "DIFF VS PREVIOUS APPROVAL MESSAGE"])
        if diff.get("baseline"):
            lines.append("Baseline: no previous same application/action package.")
        elif not changes:
            lines.append("No material context changes.")
        else:
            for change in changes[:10]:
                lines.append(f"• {change.get('path')}: {_compact(change.get('before'), 55)} → {_compact(change.get('after'), 55)}")
            if len(changes) > 10:
                lines.append(f"• … {len(changes) - 10} more change(s) in attached context JSON")
    if item_type == "question_required":
        lines.extend(["", f"Question: {payload.get('question') or NAN}", f"Reply: /answer {item_id} <your answer>"])
    elif item_type == "reconciliation_required":
        lines.extend(["", "⚠️ Do not retry until the uncertain browser execution is reconciled."])
    elif item_type == "application_ready":
        lines.extend(["", "Final Submit is not part of normal autofill. Prepare a separate privileged Submit approval when ready."])
    lines.extend(["", "Context delivery is soft-fail: missing sections show NaN and do not remove approval controls.",
                  f"Review ID: {item_id}", f"Priority: {priority}"])
    return "\n".join(lines)[:3900]


def _record_delivery(cur, item_id: str, chat_id: int, message_id: int | None,
                     kind: str, *, status: str = "sent", error: str | None = None,
                     artifact_sha256: str | None = None, context_sha256: str | None = None) -> None:
    cur.execute(
        """INSERT INTO telegram_review_deliveries(
               review_item_id, chat_id, message_id, delivery_kind, status, error_message, artifact_sha256, context_sha256)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s);""",
        (item_id, chat_id, message_id, kind, status, error, artifact_sha256, context_sha256),
    )


def _deliver_artifact(cur, token: str, *, item_id: str, chat_id: int,
                      artifact: dict[str, Any]) -> bool:
    """Best-effort exact artifact delivery. Failure never suppresses summary/approval."""
    path = Path(artifact["file_path"]).expanduser()
    if not path.is_file():
        return False
    cur.execute(
        """SELECT 1 FROM telegram_review_deliveries
             WHERE review_item_id = %s AND chat_id = %s AND delivery_kind = 'artifact'
               AND artifact_sha256 = %s AND status = 'sent' LIMIT 1;""",
        (item_id, chat_id, artifact["sha256"]),
    )
    if cur.fetchone():
        return False
    method = "sendPhoto" if artifact["mime_type"].startswith("image/") else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    try:
        with path.open("rb") as stream:
            sent = api(token, method, data={"chat_id": str(chat_id), "caption": artifact["filename"]},
                       files={field: (artifact["filename"], stream, artifact["mime_type"])})
        _record_delivery(cur, item_id, chat_id, int(sent["result"]["message_id"]), "artifact",
                         artifact_sha256=artifact["sha256"])
        return True
    except Exception as exc:
        _record_delivery(cur, item_id, chat_id, None, "artifact", status="failed",
                         error=str(exc)[:1000], artifact_sha256=artifact.get("sha256"))
        return False


def _deliver_memory_artifact(cur, token: str, *, item_id: str, chat_id: int,
                             filename: str, payload: bytes, mime_type: str) -> bool:
    digest = hashlib.sha256(payload).hexdigest()
    cur.execute("""SELECT 1 FROM telegram_review_deliveries
                    WHERE review_item_id=%s AND chat_id=%s AND delivery_kind='artifact'
                      AND artifact_sha256=%s AND status='sent' LIMIT 1;""", (item_id, chat_id, digest))
    if cur.fetchone():
        return False
    try:
        sent = api(token, "sendDocument", data={"chat_id": str(chat_id), "caption": filename},
                   files={"document": (filename, io.BytesIO(payload), mime_type)})
        _record_delivery(cur, item_id, chat_id, int(sent["result"]["message_id"]), "artifact",
                         artifact_sha256=digest)
        return True
    except Exception as exc:
        _record_delivery(cur, item_id, chat_id, None, "artifact", status="failed",
                         error=str(exc)[:1000], artifact_sha256=digest)
        return False


def dispatch_pending(conn, token: str, allowed_user_id: int, chat_id: int, *, limit: int = 20) -> int:
    with conn.cursor() as cur:
        sync_inbox(cur)
        cur.execute(
            """SELECT v.review_item_id::text, v.item_type, v.priority, v.title,
                      v.summary_text, v.company, v.job_title, v.payload_json,
                      v.application_id::text
                 FROM v_human_review_inbox v
                LIMIT %s;""",
            (limit,),
        )
        rows = cur.fetchall()
        delivered = 0
        for raw in rows:
            row, application_id = raw[:8], raw[8]
            item_id = row[0]
            try:
                envelope = build_envelope(cur, item_id, application_id)
            except Exception:
                envelope = {"schema": "jobos-human-approval-envelope-v1", "job": NAN, "fit": NAN,
                            "approval": NAN, "browser": NAN, "documents": NAN, "form": NAN, "auth": NAN}
            approval = envelope.get("approval") if isinstance(envelope.get("approval"), dict) else {}
            action_scope = str(approval.get("action_type") or row[1])
            try:
                context_sha, diff = snapshot_context(cur, review_item_id=item_id, application_id=application_id,
                                                     action_scope=action_scope, envelope=envelope)
            except Exception:
                context_sha = hashlib.sha256(json.dumps(envelope, sort_keys=True, default=str).encode()).hexdigest()
                diff = {"baseline": True, "changed": []}
            cur.execute("""SELECT 1 FROM telegram_review_deliveries
                            WHERE review_item_id=%s AND chat_id=%s AND delivery_kind='summary'
                              AND context_sha256=%s AND status='sent' LIMIT 1;""",
                        (item_id, chat_id, context_sha))
            if cur.fetchone():
                continue
            for artifact in review_artifacts(cur, item_id):
                _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id, artifact=artifact)
            for extra in context_files(envelope):
                _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                                  artifact={"file_path": extra["path"], "filename": extra["filename"],
                                            "mime_type": extra["mime_type"], "sha256": extra["sha256"]})
            job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
            jd_text = job.get("jd_text") if isinstance(job, dict) else None
            if isinstance(jd_text, str) and jd_text != NAN and jd_text.strip():
                _deliver_memory_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                                         filename=f"{application_id}-job-description.txt",
                                         payload=jd_text.encode("utf-8"), mime_type="text/plain")
            _deliver_memory_artifact(cur, token, item_id=item_id, chat_id=chat_id,
                                     filename=f"{application_id}-approval-context.json",
                                     payload=json.dumps({"context": envelope, "diff": diff}, ensure_ascii=False,
                                                        indent=2, default=str).encode("utf-8"),
                                     mime_type="application/json")
            keyboard = _keyboard(cur, item_id, allowed_user_id, row[1], row[7] or {})
            data: dict[str, Any] = {"chat_id": str(chat_id), "text": _message_text(row, envelope, diff)}
            if keyboard:
                data["reply_markup"] = keyboard
            sent = api(token, "sendMessage", data=data)
            _record_delivery(cur, item_id, chat_id, int(sent["result"]["message_id"]), "summary",
                             context_sha256=context_sha)
            delivered += 1
        conn.commit()
        return delivered

def _load_offset(cur) -> int:
    cur.execute("SELECT update_offset FROM telegram_bot_state WHERE bot_key = %s;", (BOT_KEY,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _save_offset(cur, offset: int) -> None:
    cur.execute(
        """INSERT INTO telegram_bot_state(bot_key, update_offset, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (bot_key) DO UPDATE SET update_offset = EXCLUDED.update_offset, updated_at = now();""",
        (BOT_KEY, offset),
    )


def handle_callback(conn, token: str, allowed_user_id: int, callback: dict[str, Any]) -> None:
    callback_id = str(callback.get("id") or "")
    sender_id = int((callback.get("from") or {}).get("id") or 0)
    data = str(callback.get("data") or "")
    if sender_id != allowed_user_id:
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                "text": "Not authorized", "show_alert": "true"})
        return
    if not data.startswith("rv:"):
        return
    digest = hashlib.sha256(data[3:].encode("utf-8")).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text, review_item_id::text, action, allowed_user_id,
                      expires_at, used_at
                 FROM telegram_callback_tokens WHERE token_sha256 = %s FOR UPDATE;""",
            (digest,),
        )
        row = cur.fetchone()
        if not row or row[3] != sender_id or row[5] is not None:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                     "text": "Button expired or already used", "show_alert": "true"})
            return
        cur.execute("SELECT %s < now();", (row[4],))
        if cur.fetchone()[0]:
            conn.rollback()
            api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                     "text": "Button expired", "show_alert": "true"})
            return
        item_id, action = row[1], row[2]
    try:
        result = decide_item(conn, item_id, decision=action,
                             actor=f"telegram:{sender_id}", note="Telegram review decision")
        with conn.cursor() as cur:
            cur.execute("UPDATE telegram_callback_tokens SET used_at = now() WHERE review_item_id = %s AND used_at IS NULL;", (item_id,))
        conn.commit()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": str(result["status"])})
        chat_id = int((callback.get("message") or {}).get("chat", {}).get("id") or 0)
        if chat_id:
            api(token, "sendMessage", data={"chat_id": str(chat_id),
                                             "text": f"✅ Review {item_id}: {result['status']}"})
    except ReviewError as exc:
        conn.rollback()
        api(token, "answerCallbackQuery", data={"callback_query_id": callback_id,
                                                 "text": str(exc)[:180], "show_alert": "true"})


def handle_message(conn, token: str, allowed_user_id: int, message: dict[str, Any]) -> None:
    sender_id = int((message.get("from") or {}).get("id") or 0)
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    text = str(message.get("text") or "").strip()
    if sender_id != allowed_user_id:
        return
    if text in {"/inbox", "/sync"}:
        with conn.cursor() as cur:
            counts = sync_inbox(cur)
        conn.commit()
        api(token, "sendMessage", data={"chat_id": str(chat_id),
                                         "text": f"Review inbox synced: {json.dumps(counts)}"})
        return
    if not text.startswith("/answer "):
        return
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        api(token, "sendMessage", data={"chat_id": str(chat_id),
                                         "text": "Usage: /answer <review_id> <answer>"})
        return
    try:
        answer_question(conn, parts[1], answer=parts[2], actor=f"telegram:{sender_id}", scope="company")
        conn.commit()
        api(token, "sendMessage", data={"chat_id": str(chat_id),
                                         "text": f"✅ Answer confirmed for {parts[1]} (company scoped)."})
    except ReviewError as exc:
        conn.rollback()
        api(token, "sendMessage", data={"chat_id": str(chat_id), "text": f"❌ {exc}"})


def poll_once(conn, token: str, allowed_user_id: int, *, timeout_seconds: int = 50) -> int:
    with conn.cursor() as cur:
        offset = _load_offset(cur)
    payload = api(token, "getUpdates", data={"offset": str(offset),
                  "timeout": str(timeout_seconds),
                  "allowed_updates": json.dumps(["callback_query", "message"])},
                  timeout=timeout_seconds + 15)
    next_offset = offset
    updates = payload.get("result") or []
    for update in updates:
        update_id = int(update.get("update_id") or 0)
        if update.get("callback_query"):
            handle_callback(conn, token, allowed_user_id, update["callback_query"])
        if update.get("message"):
            handle_message(conn, token, allowed_user_id, update["message"])
        next_offset = max(next_offset, update_id + 1)
    if next_offset != offset:
        with conn.cursor() as cur:
            _save_offset(cur, next_offset)
        conn.commit()
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS Telegram review adapter")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dispatch-only", action="store_true")
    parser.add_argument("--discover-id", action="store_true")
    parser.add_argument("--poll-timeout", type=int, default=50)
    args = parser.parse_args()
    if args.discover_id:
        token = _bot_token()
        me = api(token, "getMe", data={})
        print(f"Telegram bot: @{me['result'].get('username', 'unknown')}")
        rows = discover_ids(token)
        print(json.dumps(rows, indent=2))
        if not rows:
            print("No recent messages. Send /start to the bot, then run again.")
        return 0
    token, allowed_user_id, chat_id = _required_env()
    me = api(token, "getMe", data={})
    print(f"Telegram bot: @{me['result'].get('username', 'unknown')} | chat={chat_id} | allowed_user={allowed_user_id}")
    with psycopg.connect(DSN, autocommit=False) as conn:
        while True:
            sent = dispatch_pending(conn, token, allowed_user_id, chat_id)
            if sent:
                print(f"Delivered {sent} new review item(s).")
            if args.dispatch_only:
                return 0
            updates = poll_once(conn, token, allowed_user_id,
                                timeout_seconds=max(1, min(args.poll_timeout, 50)))
            if args.once:
                return 0
            if not sent and not updates:
                time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TelegramError, ReviewError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
