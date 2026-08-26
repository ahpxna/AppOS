#!/usr/bin/env python3
"""Bounded Gmail verification reader using OpenClaw's gog Google tooling.

Searches only a small post-request window for one recipient and explicitly
checks Spam. OTP/magic-link plaintext is never persisted; only message metadata
and a SHA-256 of the exact secret are stored. The executor re-fetches the same
message after human approval and compares the hash before using it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env
from services.application_actions.action_request_v1 import create_privileged_request

load_repo_env()

OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
URL_RE = re.compile(r"https://[^\s<>'\"\])]+", re.I)
VERIFY_WORDS = ("verify", "verification", "confirm", "security code", "one-time", "one time", "otp", "candidate account")


class GmailVerificationError(RuntimeError):
    pass


def gog_bin() -> str:
    return (os.getenv("JOBOS_GOG_BIN") or "gog").strip()


def gmail_account() -> str:
    account = (os.getenv("JOBOS_GMAIL_ACCOUNT") or os.getenv("GMAIL_ACCOUNT") or "").strip()
    if not account:
        raise GmailVerificationError("Set JOBOS_GMAIL_ACCOUNT or GMAIL_ACCOUNT.")
    return account


def _run_gog(args: list[str], *, timeout: int = 45, account: str | None = None) -> Any:
    binary = gog_bin()
    if shutil.which(binary) is None:
        raise GmailVerificationError(f"gog binary not found: {binary}")
    selected_account = (account or gmail_account()).strip()
    if not selected_account:
        raise GmailVerificationError("Gmail account binding is empty.")
    command = [binary, "--readonly", "--gmail-no-send", "--no-input", "--wrap-untrusted",
               "--account", selected_account, "--json", *args]
    env = dict(os.environ)
    env["GOG_GMAIL_NO_SEND"] = "1"
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise GmailVerificationError("gog Gmail command timed out") from exc
    if proc.returncode not in (0, 3):
        raise GmailVerificationError((proc.stderr or proc.stdout or "gog Gmail failure").strip()[:700])
    if proc.returncode == 3 or not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GmailVerificationError("gog Gmail returned malformed JSON") from exc


def _collect_message_objects(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ident = node.get("id") or node.get("messageId") or node.get("message_id")
            if ident:
                found.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    dedup: dict[str, dict[str, Any]] = {}
    for item in found:
        ident = str(item.get("id") or item.get("messageId") or item.get("message_id") or "")
        if ident:
            dedup[ident] = item
    return list(dedup.values())


def _message_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("messageId") or item.get("message_id") or "")


def _extract_headers(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        headers = value.get("headers")
        if isinstance(headers, dict):
            out.update({str(k).casefold(): str(v) for k, v in headers.items()})
        elif isinstance(headers, list):
            for h in headers:
                if isinstance(h, dict) and h.get("name"):
                    out[str(h["name"]).casefold()] = str(h.get("value") or "")
        payload = value.get("payload")
        if payload is not None:
            out.update(_extract_headers(payload))
        message = value.get("message")
        if message is not None:
            out.update(_extract_headers(message))
    return out


def _all_text(value: Any) -> str:
    pieces: list[str] = []
    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).casefold() in {"body", "text", "snippet", "subject", "from", "sender", "content", "html"}:
                    if isinstance(v, str):
                        pieces.append(v)
                    else:
                        walk(v, str(k))
                elif isinstance(v, (dict, list)):
                    walk(v, str(k))
        elif isinstance(node, list):
            for child in node:
                walk(child, key)
        elif isinstance(node, str):
            pieces.append(node)
    walk(value)
    return "\n".join(pieces)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, dict):
        candidates = [value.get("internalDate"), value.get("internal_date"), value.get("date")]
        headers = _extract_headers(value)
        candidates.append(headers.get("date"))
        for raw in candidates:
            if raw is None:
                continue
            try:
                if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
                    num = float(raw)
                    if num > 10_000_000_000:
                        num /= 1000.0
                    return datetime.fromtimestamp(num, tz=timezone.utc)
                parsed = parsedate_to_datetime(str(raw))
                return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def search_candidate_ids(*, recipient: str, requested_at: datetime, max_results: int = 10) -> list[str]:
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    date_token = requested_at.astimezone(timezone.utc).strftime("%Y/%m/%d")
    queries = [
        f"after:{date_token} to:{recipient} -in:spam",
        f"after:{date_token} to:{recipient} in:spam",
    ]
    ids: list[str] = []
    for query in queries:
        payload = _run_gog(["gmail", "messages", "search", query, "--max", str(max_results)])
        for item in _collect_message_objects(payload):
            ident = _message_id(item)
            if ident and ident not in ids:
                ids.append(ident)
            if len(ids) >= max_results * 2:
                break
    return ids


def read_message(message_id: str, *, sanitized: bool, account: str | None = None) -> Any:
    args = ["gmail", "get", message_id]
    if sanitized:
        args.append("--sanitize-content")
    else:
        args.extend(["--format", "full"])
    return _run_gog(args, account=account)


def _sender_subject(message: Any) -> tuple[str, str]:
    headers = _extract_headers(message)
    text = message if isinstance(message, dict) else {}
    sender = headers.get("from") or str(text.get("from") or text.get("sender") or "")
    subject = headers.get("subject") or str(text.get("subject") or "")
    return sender, subject


def _relevance_score(message: Any, *, employer_domain: str | None) -> int:
    sender, subject = _sender_subject(message)
    text = f"{sender}\n{subject}\n{_all_text(message)}".casefold()
    if not any(word in text for word in VERIFY_WORDS):
        return -1
    score = 2
    if any(word in f"{sender} {subject}".casefold() for word in ("verify", "verification", "code", "candidate")):
        score += 2
    if employer_domain:
        host_token = (urlsplit(employer_domain).hostname or employer_domain).casefold().removeprefix("www.")
        company_token = host_token.split(".")[0]
        if host_token and host_token in text:
            score += 8
        elif len(company_token) >= 4 and company_token in text:
            # Short host prefixes (for example "hr" or "id") are too weak to
            # establish employer-bound verification evidence. Keep those mails
            # in the generic ambiguity lane instead of making them executable.
            score += 5
        elif any(word in text for word in ("candidate account", "verify your email", "verification code")):
            # Soft fallback for branded ATS mail, but rank it below employer-matched mail.
            score += 1
        else:
            return -1
    return score


def _relevance_tier(message: Any, *, employer_domain: str | None) -> str:
    """Separate employer-bound evidence from generic verification-looking mail."""
    score = _relevance_score(message, employer_domain=employer_domain)
    if score < 0:
        return "irrelevant"
    if employer_domain and score >= 7:
        return "employer_match"
    return "generic_verification"


def _relevant(message: Any, *, employer_domain: str | None) -> bool:
    return _relevance_score(message, employer_domain=employer_domain) >= 0


def _extract_numeric_code(message: Any) -> str | None:
    text = _all_text(message)
    lower = text.casefold()
    matches = list(OTP_RE.finditer(text))
    if not matches:
        return None
    scored: list[tuple[int, str]] = []
    for match in matches:
        start, end = max(0, match.start() - 80), min(len(text), match.end() + 80)
        context = lower[start:end]
        score = sum(1 for word in ("code", "verify", "verification", "otp", "one-time", "security") if word in context)
        scored.append((score, match.group(1)))
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def _extract_magic_link(message: Any) -> str | None:
    text = _all_text(message)
    candidates = URL_RE.findall(text)
    ranked: list[tuple[int, str]] = []
    for url in candidates:
        low = url.casefold()
        score = sum(2 for token in ("verify", "verification", "confirm", "activate", "token") if token in low)
        if url.startswith("https://"):
            score += 1
        ranked.append((score, url))
    ranked.sort(reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] > 1 else None


def discover_verification(*, recipient: str, requested_at: datetime,
                          employer_origin: str | None, max_results: int = 10,
                          exclude_message_ids: set[str] | None = None) -> dict[str, Any] | None:
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    requested_utc = requested_at.astimezone(timezone.utc)
    ranked: list[tuple[int, datetime, str, Any, str]] = []
    excluded = {str(item) for item in (exclude_message_ids or set())}
    for message_id in search_candidate_ids(recipient=recipient, requested_at=requested_utc, max_results=max_results):
        if str(message_id) in excluded:
            continue
        sanitized = read_message(message_id, sanitized=True)
        msg_time = _timestamp(sanitized)
        # Missing/ambiguous time is not enough evidence for automatic selection.
        # Keep the pipeline waiting instead of risking an old same-day OTP.
        if msg_time is None or msg_time < requested_utc:
            continue
        score = _relevance_score(sanitized, employer_domain=employer_origin)
        tier = _relevance_tier(sanitized, employer_domain=employer_origin)
        if score < 0 or tier == "irrelevant":
            continue
        ranked.append((score, msg_time, message_id, sanitized, tier))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _score, msg_time, message_id, sanitized, relevance_tier in ranked:
        sender, subject = _sender_subject(sanitized)
        code = _extract_numeric_code(sanitized)
        if code:
            return {"message_id": message_id, "sender": sender, "subject": subject,
                    "received_at": msg_time, "kind": "numeric_code", "relevance": relevance_tier,
                    "relevance_score": _score,
                    "secret_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    "secret_context": {"kind": "numeric_code", "digits": len(code),
                                       "relevance_tier": relevance_tier}}
        # Only full-read a sanitized candidate that already passed recipient,
        # time and relevance ranking. Plaintext link is never persisted.
        full = read_message(message_id, sanitized=False)
        full_time = _timestamp(full)
        if full_time is None or full_time < requested_utc:
            continue
        link = _extract_magic_link(full)
        if link:
            parsed = urlsplit(link)
            return {"message_id": message_id, "sender": sender, "subject": subject,
                    "received_at": full_time, "kind": "magic_link", "relevance": relevance_tier,
                    "relevance_score": _score,
                    "secret_sha256": hashlib.sha256(link.encode()).hexdigest(),
                    "secret_context": {"kind": "magic_link",
                                       "link_origin": f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}",
                                       "link_path": parsed.path or "/",
                                       "relevance_tier": relevance_tier}}
    return None


def persist_candidate(cur, *, application_id: str, candidate: dict[str, Any]) -> str:
    cur.execute(
        """INSERT INTO email_verification_candidates(
               application_id, gmail_account, gmail_message_id, sender, subject,
               received_at, verification_kind, secret_sha256, secret_context_json, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'discovered')
           ON CONFLICT (application_id, gmail_message_id, verification_kind, secret_sha256)
           DO UPDATE SET sender=EXCLUDED.sender, subject=EXCLUDED.subject,
                         received_at=EXCLUDED.received_at, secret_context_json=EXCLUDED.secret_context_json
           RETURNING id::text;""",
        (application_id, gmail_account(), candidate["message_id"], candidate.get("sender"),
         candidate.get("subject"), candidate.get("received_at"), candidate["kind"], candidate["secret_sha256"],
         Jsonb(candidate.get("secret_context") or {})),
    )
    return str(cur.fetchone()[0])


def create_verification_approval(cur, *, application_id: str, candidate_id: str,
                                 expected_url: str, expected_fingerprint: str,
                                 target_id: str, field_ref: str | None = None) -> str:
    cur.execute(
        """SELECT gmail_account, gmail_message_id, sender, subject, received_at, verification_kind, secret_sha256, secret_context_json
             FROM email_verification_candidates WHERE id=%s AND application_id=%s AND status='discovered';""",
        (candidate_id, application_id),
    )
    row = cur.fetchone()
    if not row:
        raise GmailVerificationError("verification candidate is unavailable")
    payload = {
        "candidate_id": candidate_id, "gmail_account": row[0], "gmail_message_id": row[1], "sender": row[2] or "NaN",
        "subject": row[3] or "NaN", "received_at": row[4].isoformat() if row[4] else "NaN",
        "verification_kind": row[5], "secret_sha256": row[6], "secret_context": row[7] or {},
        "expected_url": expected_url, "expected_page_fingerprint": expected_fingerprint,
        "target_id": target_id, "field_ref": field_ref or "NaN",
    }
    request_id = create_privileged_request(
        cur, application_id=application_id, action_type="privileged_use_email_verification",
        payload=payload, summary=f"Use employer email verification ({row[5]}) from mailbox {row[0]} after Telegram approval.",
        requested_by="gmail-verification",
    )
    return request_id


def refetch_secret(candidate: dict[str, Any]) -> str:
    account = str(candidate.get("gmail_account") or "").strip()
    if not account:
        raise GmailVerificationError("verification approval is missing its exact Gmail account binding")
    message = read_message(str(candidate["gmail_message_id"]), sanitized=candidate["verification_kind"] == "numeric_code", account=account)
    secret = _extract_numeric_code(message) if candidate["verification_kind"] == "numeric_code" else _extract_magic_link(message)
    if not secret or hashlib.sha256(secret.encode()).hexdigest() != candidate["secret_sha256"]:
        raise GmailVerificationError("verification email secret changed or cannot be revalidated")
    context = candidate.get("secret_context") if isinstance(candidate.get("secret_context"), dict) else {}
    if candidate["verification_kind"] == "magic_link" and context:
        parsed = urlsplit(secret)
        observed_origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
        if parsed.scheme.casefold() != "https" or observed_origin != context.get("link_origin") or (parsed.path or "/") != context.get("link_path"):
            raise GmailVerificationError("verification link destination changed after approval")
    return secret


def main() -> int:
    p = argparse.ArgumentParser(description="Bounded Gmail verification lookup")
    p.add_argument("--application-id", required=True)
    p.add_argument("--recipient", required=True)
    p.add_argument("--employer-origin")
    p.add_argument("--since-unix", type=float, default=time.time() - 300)
    p.add_argument("--max-results", type=int, default=10)
    args = p.parse_args()
    try:
        candidate = discover_verification(recipient=args.recipient,
                                          requested_at=datetime.fromtimestamp(args.since_unix, tz=timezone.utc),
                                          employer_origin=args.employer_origin,
                                          max_results=max(1, min(args.max_results, 20)))
        if not candidate:
            print("No bounded verification email found (Inbox/other labels + Spam checked).")
            return 3
        with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
            candidate_id = persist_candidate(cur, application_id=args.application_id, candidate=candidate)
            conn.commit()
    except GmailVerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"candidate_id": candidate_id, "message_id": candidate["message_id"],
                      "kind": candidate["kind"], "sender": candidate.get("sender") or "NaN",
                      "subject": candidate.get("subject") or "NaN",
                      "secret": "NOT_PERSISTED"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
