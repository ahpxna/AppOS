from pathlib import Path
import sys
import types

import pytest

try:
    from psycopg.types.json import Jsonb as _Jsonb  # noqa: F401
except ModuleNotFoundError:
    psycopg = types.ModuleType("psycopg")
    psycopg_types = types.ModuleType("psycopg.types")
    psycopg_json = types.ModuleType("psycopg.types.json")
    psycopg_json.Jsonb = lambda value: value
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.types", psycopg_types)
    sys.modules.setdefault("psycopg.types.json", psycopg_json)

from services.discovery.linkedin_discovery_v1 import LinkedInDiscoveryError, normalize_jobs, validate_saved_request
from services.autofill.autofill_executor_v1 import OpenClawTransport


def test_saved_request_is_bounded():
    assert validate_saved_request(1)["max_results"] == 1
    assert validate_saved_request(20)["max_results"] == 20
    with pytest.raises(LinkedInDiscoveryError):
        validate_saved_request(21)


def test_saved_jobs_use_same_strict_canonical_job_evidence_boundary():
    response = {"jobs": [{"company": "Example", "title": "Security Engineer",
                           "location": "New Jersey", "work_mode": "hybrid",
                           "url": "https://www.linkedin.com/jobs/view/123456789/?trackingId=ignored",
                           "jd_text": "A" * 250}]}
    rows = normalize_jobs(response, 10)
    assert rows[0]["url"] == "https://www.linkedin.com/jobs/view/123456789/"
    assert len(rows[0]["jd_text"]) == 250


def test_screenshot_path_extraction_requires_existing_file(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    assert OpenClawTransport._find_media_path({"data": {"path": str(image)}}) == image.resolve()
    assert OpenClawTransport._find_media_path({"path": str(tmp_path / "missing.png")}) is None



def test_linkedin_search_discovery_has_fail_closed_blocker_boundary():
    worker = (Path(__file__).resolve().parents[1] / "services" / "browser-controller" / "browser_queue_worker.py").read_text(encoding="utf-8")
    start = worker.index("def handle_discover_linkedin_jobs")
    end = worker.index("def handle_discover_linkedin_saved_jobs", start)
    handler = worker[start:end]
    assert "execute_parallel_bypass" not in handler
    assert "_fake_mouse_routine" not in handler
    assert "raise PermanentTaskError" in handler
    for marker in ("captcha", "verification", "security check", "checkpoint", "login required"):
        assert marker in handler.casefold()
