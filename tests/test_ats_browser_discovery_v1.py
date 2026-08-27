from __future__ import annotations

from dataclasses import dataclass

from services.ats.browser_discovery import discover_public_jobs_with_browser


@dataclass
class Target:
    target_id: str
    url: str


class FakeTransport:
    def __init__(self):
        self.urls = {
            "https://careers.acme.example/jobs": "board",
            "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/engineer/REQ-1": "detail",
        }
        self.focused: list[str] = []
        self.closed: list[str] = []

    def open(self, url: str):
        key = self.urls[url]
        return Target(key, url)

    def current_url(self, target_id: str) -> str:
        for url, key in self.urls.items():
            if key == target_id:
                return url
        raise KeyError(target_id)

    def snapshot(self, target_id: str):
        if target_id == "board":
            return {
                "snapshot": "\n".join([
                    '- heading "Careers" [ref=h1]',
                    '- link "Software Engineer" [ref=l1]',
                    '  - generic: /url: https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/engineer/REQ-1',
                ]),
                "refs": {
                    "h1": {"role": "heading", "name": "Careers"},
                    "l1": {"role": "link", "name": "Software Engineer", "url": "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/engineer/REQ-1"},
                },
                "truncated": False,
            }
        body = " ".join([
            "Design reliable distributed systems with Python and PostgreSQL.",
            "Build APIs, write automated tests, review production telemetry, and collaborate with product teams.",
            "Candidates should have software engineering experience, strong communication skills, and knowledge of cloud infrastructure.",
            "This hybrid role works with engineering, security, and operations to ship maintainable services.",
        ] * 3)
        return {
            "snapshot": f'- heading "Software Engineer" [ref=h1]\n- generic "{body}"',
            "refs": {"h1": {"role": "heading", "name": "Software Engineer"}},
            "truncated": False,
        }

    def focus(self, target_id: str):
        self.focused.append(target_id)
        return Target(target_id, self.current_url(target_id))

    def close(self, target_id: str) -> None:
        self.closed.append(target_id)


def test_js_only_custom_board_can_handoff_to_known_ats_read_only():
    transport = FakeTransport()
    jobs = discover_public_jobs_with_browser(
        career_url="https://careers.acme.example/jobs",
        platform="custom",
        company_hint="Acme",
        transport=transport,
    )
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer"
    assert jobs[0]["url"].startswith("https://acme.wd5.myworkdayjobs.com/")
    assert jobs[0]["jd_quality"] == "complete"
    assert jobs[0]["discovery_method"] == "readonly_browser_snapshot"
    assert transport.closed == ["detail", "board"]
