import pytest

from services.ats.contracts import JDQuality, assess_jd_quality, infer_work_mode
from services.ats.http_client import DiscoveryHttpError
from services.ats.public_page import (
    PublicPageDiscoveryError, fetch_public_job_board, normalize_jobposting, parse_structured_page,
)


LONG_DESCRIPTION = (
    "Build and operate reliable distributed systems for a production platform. "
    "Partner with product and engineering teams to design services, review code, "
    "improve observability, test failure scenarios, document decisions, and own "
    "deployments. The role requires strong software engineering fundamentals, "
    "clear communication, incident response, and experience delivering maintainable "
    "systems in a collaborative environment."
)


def _job_json(*, url: str, description: str = LONG_DESCRIPTION, job_location_type=None) -> str:
    extra = f',"jobLocationType":"{job_location_type}"' if job_location_type else ""
    return f'''<script type="application/ld+json">{{
      "@context":"https://schema.org","@type":"JobPosting",
      "identifier":{{"value":"REQ-42"}},"title":"Staff Engineer",
      "description":{description!r},
      "hiringOrganization":{{"name":"Acme"}},
      "jobLocation":{{"address":{{"addressLocality":"Miami","addressRegion":"FL","addressCountry":"US"}}}},
      "url":"{url}"{extra}
    }}</script>'''.replace("'", '"')


def test_jsonld_jobposting_is_normalized():
    html = _job_json(url="https://jobs.acme.test/req-42")
    page = parse_structured_page(html, base_url="https://jobs.acme.test")
    job = normalize_jobposting(page.job_postings[0], page_url="https://jobs.acme.test", company_hint="Acme")
    assert job["external_id"] == "REQ-42"
    assert job["location"].startswith("Miami, FL")
    assert job["jd_quality"] == JDQuality.COMPLETE.value


def test_board_can_follow_job_links_and_find_detail_jsonld():
    board = '<a href="/jobs/42">Staff Engineer</a>'
    detail = _job_json(url="https://jobs.acme.test/jobs/42")

    def fetcher(*, url, **kwargs):
        if url.endswith('/jobs/42'):
            return detail, url
        return board, "https://jobs.acme.test/openings"

    jobs = fetch_public_job_board(
        career_url="https://jobs.acme.test/openings", platform="custom", company_hint="Acme",
        user_agent="test", fetcher=fetcher,
    )
    assert [j["external_id"] for j in jobs] == ["REQ-42"]


def test_listing_stub_is_not_admitted_and_detail_is_followed():
    stub = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Staff Engineer","description":"See details",
      "url":"https://jobs.acme.test/jobs/42"
    }</script>'''
    detail = _job_json(url="https://jobs.acme.test/jobs/42")
    calls = []

    def fetcher(*, url, **kwargs):
        calls.append(url)
        return (detail, url) if url.endswith("/jobs/42") else (stub, url)

    jobs = fetch_public_job_board(
        career_url="https://jobs.acme.test/openings", platform="custom", company_hint="Acme",
        user_agent="test", fetcher=fetcher,
    )
    assert jobs[0]["jd_quality"] == "complete"
    assert any(url.endswith("/jobs/42") for url in calls)
    assert assess_jd_quality("See details") == JDQuality.LISTING_STUB


def test_custom_company_board_may_follow_only_known_cross_host_ats():
    board = '<a href="https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/42">Staff Engineer</a>'
    detail = _job_json(url="https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/42")

    def fetcher(*, url, **kwargs):
        if "myworkdayjobs.com" in url:
            return detail, url
        return board, "https://careers.acme.com/jobs"

    jobs = fetch_public_job_board(
        career_url="https://careers.acme.com/jobs", platform="custom", company_hint="Acme",
        user_agent="test", fetcher=fetcher,
    )
    assert len(jobs) == 1
    assert "myworkdayjobs.com" in jobs[0]["url"]


def test_custom_board_refuses_unknown_cross_host_link():
    board = '<a href="https://tracking.unknown.example/jobs/42">Staff Engineer</a>'

    def fetcher(*, url, **kwargs):
        return board, "https://careers.acme.com/jobs"

    with pytest.raises(PublicPageDiscoveryError, match="no complete deterministic"):
        fetch_public_job_board(
            career_url="https://careers.acme.com/jobs", platform="custom", company_hint="Acme",
            user_agent="test", fetcher=fetcher,
        )


def test_structured_http_transient_semantics_survive_wrapper():
    def fetcher(**kwargs):
        raise DiscoveryHttpError("http", kwargs["url"], True, status=503)

    with pytest.raises(PublicPageDiscoveryError) as caught:
        fetch_public_job_board(
            career_url="https://jobs.acme.test/openings", platform="custom", company_hint="Acme",
            user_agent="test", fetcher=fetcher,
        )
    assert caught.value.transient is True
    assert caught.value.kind == "http"


def test_work_mode_structured_telecommute_and_negation_are_not_confused():
    assert infer_work_mode("TELECOMMUTE") == "remote"
    assert infer_work_mode("", "This is not a remote role. On-site five days per week.") == "on_site"
    assert infer_work_mode("", "Hybrid schedule: three days in office and two from home.") == "hybrid"
