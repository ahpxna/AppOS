from __future__ import annotations

from services.common.domain_authority_v1 import host_is_authorized


class Cur:
    def __init__(self, globals_, scoped=False):
        self.globals = list(globals_)
        self.scoped = scoped
        self._next = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_params = params
        if "FROM allowed_domains" in sql:
            self._next = self.globals
        elif "FROM application_scoped_domain_trusts" in sql:
            self._next = [(1,)] if self.scoped else []
        else:
            self._next = []

    def fetchall(self):
        result = self._next or []
        self._next = None
        return result

    def fetchone(self):
        result = self._next or []
        self._next = None
        return result[0] if result else None


def test_candidate_catalog_does_not_grant_global_browser_authority():
    cur = Cur([("oraclecloud.com", "ats_candidate_catalog")])
    assert not host_is_authorized(cur, "https://unrelated-service.oraclecloud.com/")


def test_application_scoped_trust_authorizes_exact_host_only():
    cur = Cur([("oraclecloud.com", "ats_candidate_catalog")], scoped=True)
    assert host_is_authorized(
        cur, "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/",
        application_id="app-1", purpose="employer_handoff",
    )
    assert cur.last_params == ("app-1", "acme.fa.us2.oraclecloud.com", "employer_handoff")


def test_explicit_global_policy_still_supports_subdomains():
    cur = Cur([("jobs.lever.co", "ats_global")])
    assert host_is_authorized(cur, "https://jobs.lever.co/acme/123")
