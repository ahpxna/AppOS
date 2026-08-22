import pytest

from services.discovery.linkedin_discovery_v1 import (
    LinkedInDiscoveryError,
    MAX_DISCOVERY_RESULTS,
    normalize_jobs,
    validate_search_request,
)


def test_linkedin_discovery_accepts_nested_agent_json_and_keeps_full_jd():
    response = {
        "parsed": {
            "message": (
                '{"jobs":[{"company":"Example Security","title":"Junior SOC Analyst",'
                '"location":"Remote","work_mode":"remote",'
                '"url":"https://www.linkedin.com/jobs/view/123456/",'
                '"jd_text":"' + ("Monitor alerts and investigate incidents. " * 12) + '"}]}'
            )
        }
    }
    rows = normalize_jobs(response, max_results=1)
    assert len(rows) == 1
    assert rows[0]["company"] == "Example Security"
    assert rows[0]["url"].endswith("/jobs/view/123456/")
    assert len(rows[0]["jd_text"]) >= 200


def test_linkedin_discovery_refuses_over_cap_and_non_job_urls():
    with pytest.raises(LinkedInDiscoveryError, match="1.."):
        validate_search_request("security", "", MAX_DISCOVERY_RESULTS + 1)
    with pytest.raises(LinkedInDiscoveryError, match="LinkedIn /jobs/"):
        normalize_jobs(
            {"jobs": [{
                "company": "Example", "title": "Analyst",
                "url": "https://www.linkedin.com/feed/",
                "jd_text": "x" * 300,
            }]},
            max_results=1,
        )
