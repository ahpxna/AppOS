from services.ats.public_page import fetch_public_job_board, normalize_jobposting, parse_structured_page


def test_jsonld_jobposting_is_normalized():
    html = '''<script type="application/ld+json">{
      "@context":"https://schema.org","@type":"JobPosting",
      "identifier":{"value":"REQ-42"},"title":"Staff Engineer",
      "description":"Build reliable distributed systems.",
      "hiringOrganization":{"name":"Acme"},
      "jobLocation":{"address":{"addressLocality":"Miami","addressRegion":"FL","addressCountry":"US"}},
      "url":"https://jobs.acme.test/req-42"
    }</script>'''
    page = parse_structured_page(html, base_url="https://jobs.acme.test")
    job = normalize_jobposting(page.job_postings[0], page_url="https://jobs.acme.test", company_hint="Acme")
    assert job["external_id"] == "REQ-42"
    assert job["location"].startswith("Miami, FL")


def test_board_can_follow_job_links_and_find_detail_jsonld():
    board = '<a href="/jobs/42">Staff Engineer</a>'
    detail = '''<script type="application/ld+json">{
      "@type":"JobPosting","identifier":{"value":"42"},"title":"Staff Engineer",
      "description":"Build systems.","url":"https://jobs.acme.test/jobs/42"
    }</script>'''
    def fetcher(*, url, **kwargs):
        if url.endswith('/jobs/42'):
            return detail, url
        return board, "https://jobs.acme.test/openings"
    jobs = fetch_public_job_board(
        career_url="https://jobs.acme.test/openings", platform="custom", company_hint="Acme",
        user_agent="test", fetcher=fetcher,
    )
    assert [j["external_id"] for j in jobs] == ["42"]
