"""Conservative ATS source enrollment from grounded external application URLs.

This module never guesses a company tenant from a company name.  It only
projects an ATS polling source when another discovery path returned an exact
HTTP(S) application/careers URL whose vendor identity and locator can be
derived deterministically from the URL itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from services.ats.contracts import canonical_job_url
from services.ats.registry import DiscoveryStrategy, detect_ats_platform, get_definition


@dataclass(frozen=True)
class ATSSourceEvidence:
    platform: str
    slug: str | None
    source_url: str | None
    evidence_url: str


def _path_parts(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def _native_slug(platform: str, url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    parts = _path_parts(url)
    candidate = ""
    if platform == "greenhouse":
        # Ground only the stable board form: /<tenant>/jobs/<id>.
        if len(parts) >= 3 and parts[1].casefold() == "jobs":
            candidate = parts[0]
    elif platform == "lever":
        if host.endswith("lever.co") and len(parts) >= 2:
            candidate = parts[0]
    elif platform == "ashby":
        if host.endswith("ashbyhq.com") and len(parts) >= 2:
            candidate = parts[0]
    elif platform == "smartrecruiters":
        if host.endswith("smartrecruiters.com") and len(parts) >= 2:
            candidate = parts[0]
    elif platform == "workable":
        # Common stable form: apply.workable.com/<tenant>/j/<job-id>/.
        if len(parts) >= 3 and parts[1].casefold() == "j":
            candidate = parts[0]
    elif platform == "recruitee":
        suffix = ".recruitee.com"
        candidate = host[: -len(suffix)] if host.endswith(suffix) else ""
        if candidate in {"", "www", "careers"}:
            candidate = ""
    elif platform == "breezy":
        suffix = ".breezy.hr"
        candidate = host[: -len(suffix)] if host.endswith(suffix) else ""
        if candidate in {"", "www", "app"}:
            candidate = ""
    else:
        return None
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "", candidate).strip("._-")
    return candidate[:160] or None


def derive_ats_source(url: str | None) -> ATSSourceEvidence | None:
    """Return deterministic ATS source evidence or ``None`` for unknown URLs."""
    try:
        canonical = canonical_job_url(url)
    except (TypeError, ValueError):
        return None
    if not canonical:
        return None
    platform = detect_ats_platform(canonical)
    if platform == "custom":
        return None
    definition = get_definition(platform)
    if definition.discovery_strategy == DiscoveryStrategy.EXTERNAL_SOURCE:
        return None
    slug = _native_slug(platform, canonical)
    if definition.discovery_strategy == DiscoveryStrategy.NATIVE_API:
        if not slug:
            return None
        return ATSSourceEvidence(platform, slug, None, canonical)
    # For structured-web vendors, the exact grounded candidate URL is a valid
    # deterministic source.  The public-page adapter may follow board/detail
    # links but never invents a tenant endpoint.
    return ATSSourceEvidence(platform, None, canonical, canonical)


def enroll_ats_source(cur, *, company: str, apply_url: str | None,
                      evidence_source: str = "linkedin_browser_discovery") -> str | None:
    """Upsert one ATS polling source proven by an exact external application URL."""
    evidence = derive_ats_source(apply_url)
    if evidence is None:
        return None
    company_name = re.sub(r"\s+", " ", str(company or "").strip())[:300]
    if not company_name:
        return None
    note = f"Auto-enrolled from grounded {evidence_source} external apply URL: {evidence.evidence_url}"
    if evidence.slug:
        cur.execute(
            """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
               VALUES (%s,%s,%s,NULL,true,%s,now())
               ON CONFLICT (ats_platform,slug) WHERE nullif(trim(coalesce(slug,'')),'') IS NOT NULL
               DO UPDATE SET company_name=EXCLUDED.company_name,enabled=true,notes=EXCLUDED.notes,updated_at=now()
               RETURNING id::text;""",
            (company_name, evidence.platform, evidence.slug, note),
        )
    else:
        # A structured vendor may expose a distinct exact URL per posting.  One
        # company/platform source is enough; never grow ats_companies by one row
        # per LinkedIn result merely because each detail URL differs.
        cur.execute(
            """SELECT id::text FROM ats_companies
                 WHERE ats_platform=%s AND lower(trim(company_name))=lower(trim(%s))
                 ORDER BY created_at LIMIT 1;""",
            (evidence.platform, company_name),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        cur.execute(
            """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
               VALUES (%s,%s,NULL,%s,true,%s,now())
               ON CONFLICT (ats_platform,source_url) WHERE nullif(trim(coalesce(source_url,'')),'') IS NOT NULL
               DO UPDATE SET company_name=EXCLUDED.company_name,enabled=true,notes=EXCLUDED.notes,updated_at=now()
               RETURNING id::text;""",
            (company_name, evidence.platform, evidence.source_url, note),
        )
    row = cur.fetchone()
    return str(row[0]) if row else None
