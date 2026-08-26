"""Canonical browser-domain authority checks.

Global allowlist entries are administrator policy. Per-application grants are
human-approved, purpose-bound, and expire; neither substitutes for the other.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def host_is_authorized(cur, url: str, *, application_id: str | None = None,
                       purpose: str = "employer_handoff") -> bool:
    host = (urlsplit(str(url)).hostname or "").casefold()
    if not host:
        return False
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled=true;")
    if any(host == str(row[0]).casefold() or host.endswith("." + str(row[0]).casefold())
           for row in cur.fetchall()):
        return True
    if not application_id:
        return False
    cur.execute(
        """SELECT 1 FROM application_scoped_domain_trusts
             WHERE application_id=%s AND domain=%s AND purpose=%s
               AND enabled=true AND expires_at>now() LIMIT 1;""",
        (application_id, host, purpose),
    )
    return cur.fetchone() is not None
