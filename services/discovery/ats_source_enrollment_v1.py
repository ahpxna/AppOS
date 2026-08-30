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


def _normalized_company(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def enroll_ats_source(cur, *, company: str, apply_url: str | None,
                      evidence_source: str = "linkedin_browser_discovery",
                      href_evidence: str | None = None) -> str | None:
    """Upsert one ATS source only from independently verified DOM href evidence.

    ``href_evidence`` is not descriptive text from an agent.  The browser worker
    passes the exact canonical href independently read from the live DOM through
    CDP.  Autonomous enrollment also refuses to rename an existing tenant/source
    to a different company and never changes an operator's ``enabled`` choice.
    """
    evidence = derive_ats_source(apply_url)
    if evidence is None:
        return None
    try:
        witness = canonical_job_url(str(href_evidence or ""))
    except (TypeError, ValueError):
        return None
    if witness != evidence.evidence_url:
        return None
    company_name = re.sub(r"\s+", " ", str(company or "").strip())[:300]
    if not company_name:
        return None
    note = f"Auto-enrolled from verified {evidence_source} DOM href: {evidence.evidence_url}"

    locator = evidence.slug or evidence.source_url or ""
    # Serialize both locator identity and company/platform projection. This keeps
    # concurrent discoveries deterministic without making company names into a
    # guessed ATS tenant authority.
    for lock_key in sorted({
        f"ats-company:{evidence.platform}:{company_name.casefold()}",
        f"ats-locator:{evidence.platform}:{locator.casefold()}",
    }):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (lock_key,))

    if evidence.slug:
        cur.execute(
            """SELECT id::text,company_name,enabled FROM ats_companies
                 WHERE ats_platform=%s AND slug=%s FOR UPDATE;""",
            (evidence.platform, evidence.slug),
        )
        existing = cur.fetchone()
        if existing:
            if _normalized_company(existing[1]) != _normalized_company(company_name):
                return None
            cur.execute(
                "UPDATE ats_companies SET notes=%s,updated_at=now() WHERE id=%s RETURNING id::text;",
                (note, existing[0]),
            )
            return str(cur.fetchone()[0])
        cur.execute(
            """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
               VALUES (%s,%s,%s,NULL,true,%s,now()) RETURNING id::text;""",
            (company_name, evidence.platform, evidence.slug, note),
        )
        return str(cur.fetchone()[0])

    # A board URL already owned by a different company is an identity conflict,
    # not evidence that the existing row should be renamed.
    cur.execute(
        """SELECT id::text,company_name FROM ats_companies
             WHERE ats_platform=%s AND source_url=%s FOR UPDATE;""",
        (evidence.platform, evidence.source_url),
    )
    locator_existing = cur.fetchone()
    if locator_existing and _normalized_company(locator_existing[1]) != _normalized_company(company_name):
        return None

    if locator_existing:
        cur.execute(
            "UPDATE ats_companies SET notes=%s,updated_at=now() WHERE id=%s RETURNING id::text;",
            (note, locator_existing[0]),
        )
        return str(cur.fetchone()[0])

    # Only an auto-enrolled row is ours to retarget when the employer publishes
    # a newer canonical board URL. Manual/operator rows are immutable authority
    # here and multiple legitimate boards for one company/platform may coexist.
    cur.execute(
        """SELECT id::text,source_url,notes FROM ats_companies
             WHERE ats_platform=%s AND lower(trim(company_name))=lower(trim(%s))
             ORDER BY created_at FOR UPDATE;""",
        (evidence.platform, company_name),
    )
    same_company = cur.fetchall()
    auto_owned = [row for row in same_company if str(row[2] or "").startswith("Auto-enrolled from verified ")]
    if len(auto_owned) == 1:
        cur.execute(
            """UPDATE ats_companies
                  SET source_url=%s, notes=%s, updated_at=now()
                WHERE id=%s RETURNING id::text;""",
            (evidence.source_url, note, auto_owned[0][0]),
        )
        return str(cur.fetchone()[0])
    cur.execute(
        """INSERT INTO ats_companies(company_name,ats_platform,slug,source_url,enabled,notes,updated_at)
           VALUES (%s,%s,NULL,%s,true,%s,now()) RETURNING id::text;""",
        (company_name, evidence.platform, evidence.source_url, note),
    )
    return str(cur.fetchone()[0])
