"""Conservative ATS source enrollment from grounded external application URLs.

This module never guesses a company tenant from a company name.  It only
projects an ATS polling source when another discovery path returned an exact
HTTP(S) application/careers URL whose vendor identity and locator can be
derived deterministically from the URL itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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


def _structured_board_url(platform: str, url: str) -> str | None:
    """Reduce a *grounded* detail URL to its stable board/careers scope.

    This never manufactures a tenant.  It preserves the witnessed host and
    only removes the trailing job-detail suffix that is recognizable for the
    detected structured vendor.
    """
    parsed = urlsplit(url)
    parts = _path_parts(url)
    keep: list[str] | None = None
    lowered = [part.casefold() for part in parts]
    if platform == "workday":
        # /en-US/<board>/job/<id>/... -> /en-US/<board>
        if "job" in lowered:
            index = lowered.index("job")
            keep = parts[:index]
    elif platform in {"icims", "taleo", "oracle", "successfactors", "custom"}:
        for marker in ("jobs", "job", "career", "careers", "posting"):
            if marker in lowered:
                keep = parts[: lowered.index(marker) + 1]
                break
    if not keep:
        return None
    path = "/" + "/".join(keep)
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") + "/", "", ""))


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
    # Structured polling is board-scoped, never detail-scoped.  If the exact
    # witnessed URL cannot be deterministically reduced to that scope, defer
    # enrollment rather than polling a single posting forever.
    board_url = _structured_board_url(platform, canonical)
    if not board_url:
        return None
    return ATSSourceEvidence(platform, None, board_url, canonical)


def enroll_ats_source(cur, *, company: str, apply_url: str | None,
                      evidence_source: str = "linkedin_browser_discovery",
                      href_evidence: str | None = None) -> str | None:
    """Upsert one ATS source only from browser/DOM href evidence.

    Manual ``ats add`` is intentionally separate.  Autonomous enrollment must
    prove that the exact external URL was present in the browser snapshot; an
    agent-provided URL alone is never an enrollment authority.
    """
    evidence = derive_ats_source(apply_url)
    if evidence is None:
        return None
    witness = str(href_evidence or "")
    if not witness or evidence.evidence_url not in witness:
        return None
    company_name = re.sub(r"\s+", " ", str(company or "").strip())[:300]
    if not company_name:
        return None
    note = f"Auto-enrolled from grounded {evidence_source} external apply URL: {evidence.evidence_url}"
    # Serialize same company/platform enrollment. DB unique indexes remain the
    # final authority for locator collisions across workers.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (f"ats:{evidence.platform}:{company_name.casefold()}",))
    if evidence.slug:
        cur.execute(
            """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
               VALUES (%s,%s,%s,NULL,true,%s,now())
               ON CONFLICT (ats_platform,slug) WHERE nullif(trim(coalesce(slug,'')),'') IS NOT NULL
               DO UPDATE SET company_name=EXCLUDED.company_name,
                             notes=EXCLUDED.notes,updated_at=now()
               RETURNING id::text;""",
            (company_name, evidence.platform, evidence.slug, note),
        )
    else:
        # One company/platform source is enough. Refresh its canonical board
        # URL when a newer valid href witness is observed, while preserving an
        # operator's enabled=false decision.
        cur.execute(
            """SELECT id::text FROM ats_companies
                 WHERE ats_platform=%s AND lower(trim(company_name))=lower(trim(%s))
                 ORDER BY created_at LIMIT 1 FOR UPDATE;""",
            (evidence.platform, company_name),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """UPDATE ats_companies
                      SET source_url=%s, notes=%s, updated_at=now()
                    WHERE id=%s RETURNING id::text;""",
                (evidence.source_url, note, existing[0]),
            )
            return str(cur.fetchone()[0])
        cur.execute(
            """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
               VALUES (%s,%s,NULL,%s,true,%s,now())
               ON CONFLICT (ats_platform,source_url) WHERE nullif(trim(coalesce(source_url,'')),'') IS NOT NULL
               DO UPDATE SET company_name=EXCLUDED.company_name,
                             notes=EXCLUDED.notes,updated_at=now()
               RETURNING id::text;""",
            (company_name, evidence.platform, evidence.source_url, note),
        )
    row = cur.fetchone()
    return str(row[0]) if row else None
