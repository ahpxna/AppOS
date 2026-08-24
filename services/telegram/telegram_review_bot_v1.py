#!/usr/bin/env python3
"""Telegram long-polling adapter for the JobOS Human Review Hub.

No webhook/public server is required. Callback buttons contain only short,
opaque single-use tokens; every decision is revalidated by Review Hub and the
canonical approval service before any state change.
"""
from __future__ import annotations

import argparse
import hashlib
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
    if item_type == "autofill_review" and payload.get("execution_state") not in {"completed", "partial"}:
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
    approve = _callback_token(cur, item_id, "approve", allowed_user_id)
    revise = _callback_token(cur, item_id, "revise", allowed_user_id)
    reject = _callback_token(cur, item_id, "reject", allowed_user_id)
    return json.dumps({"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": f"rv:{approve}"}],
        [{"text": "📝 Revise", "callback_data": f"rv:{revise}"},
         {"text": "❌ Reject", "callback_data": f"rv:{reject}"}],
    ]}, separators=(",", ":"))


def _message_text(row: tuple[Any, ...]) -> str:
    item_id, item_type, priority, title, summary, company, role, payload = row
    payload = payload or {}
    lines = ["🧭 JobOS Review", f"{company} — {role}", "", str(title)]
    if summary:
        lines.extend(["", str(summary)])
    if item_type == "autofill_review":
        lines.extend(["", f"Verified: {len(payload.get('verified_refs') or [])}",
                      f"Failed: {len(payload.get('failed_refs') or [])}",
                      f"Paused: {len(payload.get('paused') or [])}", "Submit: HUMAN ONLY"])
    elif item_type == "question_required":
        lines.extend(["", f"Question: {payload.get('question') or ''}",
                      f"Reply: /answer {item_id} <your answer>",
                      "Default memory scope: this company only."])
    elif item_type == "reconciliation_required":
        lines.extend(["", "⚠️ Do not retry autofill until the underlying browser execution is reconciled."])
    elif item_type == "application_ready":
        lines.extend(["", "🚫 Telegram cannot submit this application.",
                      "Open the pinned browser tab, inspect the final form, and click Submit yourself."])
    lines.extend(["", f"Review ID: {item_id}", f"Priority: {priority}"])
    return "\n".join(lines)[:3900]


def _record_delivery(cur, item_id: str, chat_id: int, message_id: int | None,
                     kind: str, *, status: str = "sent", error: str | None = None,
                     artifact_sha256: str | None = None) -> None:
    cur.execute(
        """INSERT INTO telegram_review_deliveries(
               review_item_id, chat_id, message_id, delivery_kind, status, error_message, artifact_sha256)
           VALUES (%s, %s, %s, %s, %s, %s, %s);""",
        (item_id, chat_id, message_id, kind, status, error, artifact_sha256),
    )


def _deliver_artifact(cur, token: str, *, item_id: str, chat_id: int,
                      artifact: dict[str, Any]) -> bool:
    """Deliver one exact artifact once, independently of summary delivery."""
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
    except TelegramError as exc:
        _record_delivery(cur, item_id, chat_id, None, "artifact", status="failed",
                         error=str(exc)[:1000], artifact_sha256=artifact["sha256"])
        raise


def dispatch_pending(conn, token: str, allowed_user_id: int, chat_id: int, *, limit: int = 20) -> int:
    with conn.cursor() as cur:
        sync_inbox(cur)
        # Artifacts can be rendered/captured after their review summary was
        # already delivered. Send them from their own ledger, never gated by
        # the summary delivery row.
        cur.execute(
            """SELECT DISTINCT v.review_item_id::text
                 FROM v_human_review_inbox v
                 JOIN human_review_artifacts a ON a.review_item_id = v.review_item_id
                WHERE NOT EXISTS (
                      SELECT 1 FROM telegram_review_deliveries d
                       WHERE d.review_item_id = v.review_item_id AND d.chat_id = %s
                         AND d.delivery_kind = 'artifact' AND d.artifact_sha256 = a.sha256
                         AND d.status = 'sent')
                LIMIT %s;""",
            (chat_id, limit),
        )
        for (item_id,) in cur.fetchall():
            for artifact in review_artifacts(cur, item_id):
                _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id, artifact=artifact)
        cur.execute(
            """SELECT v.review_item_id::text, v.item_type, v.priority, v.title,
                      v.summary_text, v.company, v.job_title, v.payload_json
                 FROM v_human_review_inbox v
                WHERE NOT EXISTS (
                      SELECT 1 FROM telegram_review_deliveries d
                       WHERE d.review_item_id = v.review_item_id AND d.chat_id = %s
                         AND d.delivery_kind = 'summary' AND d.status = 'sent')
                LIMIT %s;""",
            (chat_id, limit),
        )
        rows = cur.fetchall()
        for row in rows:
            item_id = row[0]
            for artifact in review_artifacts(cur, item_id):
                _deliver_artifact(cur, token, item_id=item_id, chat_id=chat_id, artifact=artifact)
            keyboard = _keyboard(cur, item_id, allowed_user_id, row[1], row[7] or {})
            data: dict[str, Any] = {"chat_id": str(chat_id), "text": _message_text(row)}
            if keyboard:
                data["reply_markup"] = keyboard
            sent = api(token, "sendMessage", data=data)
            _record_delivery(cur, item_id, chat_id, int(sent["result"]["message_id"]), "summary")
        conn.commit()
        return len(rows)


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
