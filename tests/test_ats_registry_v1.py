from services.ats.registry import (
    DEFINITIONS, DiscoveryStrategy, candidate_domain_rows, detect_ats_platform,
    get_definition, normalize_ats_key, platform_keys,
)


def test_registry_is_broad_and_has_custom_fallback():
    keys = set(platform_keys())
    assert len(keys) >= 159
    for required in {
        "workday", "icims", "oracle_hcm", "oracle_taleo", "successfactors",
        "avature", "eightfold", "adp_workforcenow", "ukg", "dayforce",
        "bamboohr", "jazzhr", "clearcompany", "teamtailor", "pinpoint",
        "jobvite", "paycom", "paylocity", "neogov", "usajobs", "custom",
        "darwinbox", "spark_hire", "eploy",
    }:
        assert required in keys


def test_alias_normalization_is_canonical():
    assert normalize_ats_key("SAP SuccessFactors") == "successfactors"
    assert normalize_ats_key("Taleo") == "oracle_taleo"
    assert normalize_ats_key("ADP") == "adp_workforcenow"


def test_major_enterprise_url_detection():
    assert detect_ats_platform("https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/123") == "workday"
    assert detect_ats_platform("https://jobs-acme.icims.com/jobs/123/job") == "icims"
    assert detect_ats_platform("https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/10") == "oracle_hcm"
    assert detect_ats_platform("https://acme.taleo.net/careersection/2/jobdetail.ftl") == "oracle_taleo"


def test_unknown_official_portal_is_supported_as_custom_not_rejected():
    assert detect_ats_platform("https://jobs.example-corp.com/openings/123") == "custom"
    assert get_definition("custom").discovery_strategy == DiscoveryStrategy.STRUCTURED_WEB


def test_registry_has_unique_keys():
    assert len(DEFINITIONS) == len(platform_keys())


def test_every_dedicated_candidate_domain_round_trips_to_its_registry_identity():
    failures = []
    for item in DEFINITIONS.values():
        for domain in item.candidate_domains:
            detected = detect_ats_platform(f"https://{domain}/jobs/fixture")
            if detected != item.key:
                failures.append((item.key, domain, detected))
    assert not failures


def test_candidate_allowlist_rows_always_resolve_to_a_known_registry_platform():
    for domain, _category, _notes in candidate_domain_rows():
        assert detect_ats_platform(f"https://{domain}/jobs/fixture") != "custom"
