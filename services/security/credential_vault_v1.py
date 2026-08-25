#!/usr/bin/env python3
"""Local AES-256-GCM credential vault for employer-site accounts.

The master key lives outside PostgreSQL. Database rows contain ciphertext,
nonce, AAD and a SHA-256 audit digest only. Secrets are never printed by this
module and Telegram context renders only masked vault references.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env

load_repo_env()
DEFAULT_KEY_FILE = ROOT / "data" / "secrets" / "jobos-vault.key"


class VaultError(RuntimeError):
    pass


def normalize_origin(value: str) -> str:
    p = urlsplit((value or "").strip())
    if p.scheme not in {"http", "https"} or not p.netloc:
        raise VaultError("origin must be an http(s) origin")
    return f"{p.scheme.casefold()}://{p.netloc.casefold()}"


def key_file() -> Path:
    return Path(os.getenv("JOBOS_VAULT_KEY_FILE", str(DEFAULT_KEY_FILE))).expanduser().resolve()


def init_master_key(*, force: bool = False) -> Path:
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
        return path
    raw = secrets.token_bytes(32)
    path.write_text(base64.urlsafe_b64encode(raw).decode("ascii"), encoding="ascii")
    path.chmod(0o600)
    return path


def load_master_key() -> bytes:
    env = (os.getenv("JOBOS_VAULT_MASTER_KEY") or "").strip()
    if env:
        try:
            raw = base64.urlsafe_b64decode(env + "=" * (-len(env) % 4))
        except Exception as exc:
            raise VaultError("JOBOS_VAULT_MASTER_KEY is not valid base64") from exc
    else:
        path = key_file()
        if not path.is_file():
            raise VaultError(f"vault master key missing: {path}; run vault init")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise VaultError(f"vault key permissions must be 0600: {path}")
        try:
            raw = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip())
        except Exception as exc:
            raise VaultError("vault key file is invalid") from exc
    if len(raw) != 32:
        raise VaultError("vault master key must decode to exactly 32 bytes")
    return raw


def _aad(origin: str, account_key: str, secret_kind: str, key_version: int) -> str:
    return f"jobos-vault-v1|{origin}|{account_key.casefold()}|{secret_kind}|k{key_version}"


def store_secret(cur, *, origin: str, account_key: str, secret_kind: str,
                 secret: str, metadata: dict | None = None, key_version: int = 1) -> str:
    if not secret:
        raise VaultError("secret must not be empty")
    origin = normalize_origin(origin)
    account_key = account_key.strip().casefold()
    secret_kind = secret_kind.strip().casefold()
    if not account_key or not secret_kind:
        raise VaultError("account_key and secret_kind are required")
    key = load_master_key()
    nonce = secrets.token_bytes(12)
    aad = _aad(origin, account_key, secret_kind, key_version)
    plaintext = secret.encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad.encode("utf-8"))
    digest = hashlib.sha256(plaintext).hexdigest()
    cur.execute(
        """UPDATE credential_vault_entries
              SET status = 'rotated', rotated_at = now()
            WHERE origin = %s AND account_key = %s AND secret_kind = %s AND status = 'active';""",
        (origin, account_key, secret_kind),
    )
    cur.execute(
        """INSERT INTO credential_vault_entries(
               origin, account_key, secret_kind, ciphertext, nonce, aad, secret_sha256,
               key_version, status, metadata_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)
           RETURNING id::text;""",
        (origin, account_key, secret_kind, ciphertext, nonce, aad, digest, key_version, Jsonb(metadata or {})),
    )
    return str(cur.fetchone()[0])


def read_secret(cur, *, origin: str, account_key: str, secret_kind: str) -> str:
    origin = normalize_origin(origin)
    account_key = account_key.strip().casefold()
    secret_kind = secret_kind.strip().casefold()
    cur.execute(
        """SELECT ciphertext, nonce, aad, secret_sha256
             FROM credential_vault_entries
            WHERE origin = %s AND account_key = %s AND secret_kind = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1;""",
        (origin, account_key, secret_kind),
    )
    row = cur.fetchone()
    if not row:
        raise VaultError("no active vault secret for this origin/account/kind")
    plaintext = AESGCM(load_master_key()).decrypt(bytes(row[1]), bytes(row[0]), str(row[2]).encode("utf-8"))
    if hashlib.sha256(plaintext).hexdigest() != row[3]:
        raise VaultError("vault integrity check failed")
    return plaintext.decode("utf-8")


def generate_password(length: int = 28) -> str:
    if length < 20:
        raise VaultError("generated passwords must be at least 20 characters")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*_-+="
    return "".join(secrets.choice(alphabet) for _ in range(length))


def mask_entry(cur, *, origin: str, account_key: str, secret_kind: str) -> dict[str, str]:
    origin = normalize_origin(origin)
    cur.execute(
        """SELECT id::text, secret_sha256, created_at
             FROM credential_vault_entries
            WHERE origin = %s AND account_key = %s AND secret_kind = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1;""",
        (origin, account_key.casefold(), secret_kind.casefold()),
    )
    row = cur.fetchone()
    if not row:
        return {"status": "NaN", "origin": origin, "account": account_key, "kind": secret_kind}
    return {"status": "active", "origin": origin, "account": account_key,
            "kind": secret_kind, "vault_id": row[0], "sha256_prefix": str(row[1])[:12],
            "created_at": row[2].isoformat() if row[2] else "NaN"}


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS encrypted credential vault")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ps = sub.add_parser("set")
    ps.add_argument("--origin", required=True); ps.add_argument("--account", required=True); ps.add_argument("--kind", default="password")
    pg = sub.add_parser("generate")
    pg.add_argument("--origin", required=True); pg.add_argument("--account", required=True); pg.add_argument("--kind", default="password"); pg.add_argument("--length", type=int, default=28)
    pst = sub.add_parser("status")
    pst.add_argument("--origin", required=True); pst.add_argument("--account", required=True); pst.add_argument("--kind", default="password")
    pr = sub.add_parser("revoke")
    pr.add_argument("--origin", required=True); pr.add_argument("--account", required=True); pr.add_argument("--kind", default="password")
    args = p.parse_args()
    if args.command == "init":
        path = init_master_key()
        print(f"Vault key ready: {path} (0600)")
        return 0
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if args.command == "set":
            import getpass
            secret = getpass.getpass("Secret (not echoed): ")
            store_secret(cur, origin=args.origin, account_key=args.account, secret_kind=args.kind, secret=secret)
            conn.commit(); print("Stored encrypted secret.")
        elif args.command == "generate":
            secret = generate_password(args.length)
            store_secret(cur, origin=args.origin, account_key=args.account, secret_kind=args.kind, secret=secret,
                         metadata={"generated_by": "jobos"})
            conn.commit(); print("Generated and stored encrypted secret. Plaintext was not printed.")
        elif args.command == "status":
            print(mask_entry(cur, origin=args.origin, account_key=args.account, secret_kind=args.kind))
        else:
            cur.execute("""UPDATE credential_vault_entries SET status='revoked', rotated_at=now()
                            WHERE origin=%s AND account_key=%s AND secret_kind=%s AND status='active';""",
                        (normalize_origin(args.origin), args.account.casefold(), args.kind.casefold()))
            conn.commit(); print(f"Revoked {cur.rowcount} active secret(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
