"""Canonical ATS/candidate-system registry for JobOS V1.0.

The registry is intentionally independent from browser execution.  It gives
all intake/application paths one vocabulary for ATS identity, URL/signature
detection, discovery strategy and generic browser capability seeding.  Vendor
CSS selectors do not belong here: browser writes continue through the shared
accessibility-snapshot engine and its exact approval/page/domain gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import urlsplit


class DiscoveryStrategy(StrEnum):
    NATIVE_API = "native_api"
    STRUCTURED_WEB = "structured_web"
    EXTERNAL_SOURCE = "external_source"


class AutofillVerification(StrEnum):
    REVIEW_ONLY = "review_only"
    GENERIC_ACCESSIBILITY = "generic_accessibility"
    FIXTURE_VERIFIED = "fixture_verified"
    LIVE_VERIFIED = "live_verified"


@dataclass(frozen=True)
class ATSDefinition:
    key: str
    display_name: str
    aliases: tuple[str, ...] = ()
    candidate_domains: tuple[str, ...] = ()
    text_markers: tuple[str, ...] = ()
    discovery_strategy: DiscoveryStrategy = DiscoveryStrategy.STRUCTURED_WEB
    autofill_mode: str = "generic_browser"
    autofill_verification: AutofillVerification = AutofillVerification.GENERIC_ACCESSIBILITY
    category: str = "ats"


def _d(key: str, name: str, *, aliases=(), domains=(), markers=(),
       strategy: DiscoveryStrategy = DiscoveryStrategy.STRUCTURED_WEB,
       mode: str = "generic_browser", category: str = "ats",
       verification: AutofillVerification | None = None) -> ATSDefinition:
    return ATSDefinition(
        key=key, display_name=name, aliases=tuple(aliases),
        candidate_domains=tuple(str(x).casefold().strip(".") for x in domains if x),
        text_markers=tuple(str(x).casefold() for x in markers if x),
        discovery_strategy=strategy, autofill_mode=mode,
        autofill_verification=(verification if verification is not None else
                               (AutofillVerification.REVIEW_ONLY if mode == "review_only" else AutofillVerification.GENERIC_ACCESSIBILITY)),
        category=category,
    )


# Broad US-facing catalog.  The first seven keep their existing public JSON
# adapters; the rest use deterministic structured-web discovery when a career
# URL exposes machine-readable JobPosting data.  Exact job URLs can always
# enter through another intake source, and unknown/internal ATSes use custom.
_DEFINITIONS = (
    _d("greenhouse", "Greenhouse", domains=("greenhouse.io",), markers=("greenhouse",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("lever", "Lever", domains=("lever.co",), markers=("lever",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("ashby", "Ashby", domains=("ashbyhq.com",), markers=("ashby",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("smartrecruiters", "SmartRecruiters", domains=("smartrecruiters.com",), markers=("smartrecruiters",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("recruitee", "Recruitee", domains=("recruitee.com",), markers=("recruitee",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("workable", "Workable", domains=("workable.com",), markers=("workable",), strategy=DiscoveryStrategy.NATIVE_API),
    _d("breezy", "Breezy HR", aliases=("breezy hr",), domains=("breezy.hr",), markers=("breezy",), strategy=DiscoveryStrategy.NATIVE_API),

    _d("workday", "Workday Recruiting", aliases=("workday recruiting",), domains=("myworkdayjobs.com",), markers=("workday", "myworkdayjobs")),
    _d("darwinbox", "Darwinbox Recruiting", aliases=("darwinbox ats", "darwinbox recruiting"), domains=("darwinbox.in",), markers=("darwinbox",)),
    _d("spark_hire", "Spark Hire Recruit", aliases=("spark hire", "spark hire recruit"), domains=("sparkhire.com",), markers=("spark hire",)),
    _d("eploy", "Eploy", aliases=("eploy ats",), domains=("eploy.co.uk",), markers=("eploy",)),
    _d("icims", "iCIMS", domains=("icims.com",), markers=("icims",)),
    _d("jibe_icims", "Jibe / iCIMS Candidate Experience", aliases=("jibe", "jibe icims"), domains=("jibeapply.com",), markers=("jibe",)),
    _d("oracle_taleo", "Oracle Taleo Enterprise", aliases=("taleo", "oracle taleo", "taleo enterprise"), domains=("taleo.net",), markers=("taleo",)),
    _d("oracle_hcm", "Oracle Recruiting Cloud / Fusion HCM", aliases=("oracle", "oracle recruiting cloud", "oracle hcm", "fusion hcm"), domains=("oraclecloud.com",), markers=("oracle recruiting", "candidate experience")),
    _d("oracle_peoplesoft_candidate_gateway", "Oracle PeopleSoft Candidate Gateway", aliases=("peoplesoft candidate gateway", "oracle peoplesoft"), markers=("peoplesoft candidate gateway",)),
    _d("oracle_selectminds", "Oracle SelectMinds / Taleo Social Sourcing", aliases=("selectminds", "oracle selectminds"), markers=("selectminds",)),
    _d("successfactors", "SAP SuccessFactors Recruiting", aliases=("sap successfactors", "success factors"), domains=("successfactors.com",), markers=("successfactors",)),
    _d("successfactors_legacy_rcm", "SAP SuccessFactors Legacy RCM", aliases=("successfactors rcm",), markers=("successfactors recruiting management",)),
    _d("successfactors_rmk", "SAP SuccessFactors RMK / Career Site Builder", aliases=("successfactors rmk", "rmk", "career site builder"), domains=("jobs2web.com",), markers=("jobs2web", "successfactors rmk")),
    _d("jobvite", "Jobvite", domains=("jobvite.com",), markers=("jobvite",)),
    _d("adp_workforcenow", "ADP Workforce Now Recruiting", aliases=("adp", "adp recruiting", "workforce now"), domains=("workforcenow.adp.com", "myjobs.adp.com"), markers=("adp workforce now", "adp recruiting")),
    _d("ukg", "UKG Pro Recruiting / Ultimate", aliases=("ultipro", "ultimate software", "ukg pro", "ukg recruiting"), domains=("ultipro.com", "ukg.net"), markers=("ukg pro recruiting", "ultipro")),
    _d("dayforce", "Dayforce Recruiting", aliases=("ceridian dayforce", "ceridian"), domains=("dayforcehcm.com",), markers=("dayforce",)),
    _d("bamboohr", "BambooHR Hiring", aliases=("bamboo hr",), domains=("bamboohr.com",), markers=("bamboohr",)),
    _d("jazzhr", "JazzHR / ApplyToJob", aliases=("jazz hr", "applytojob"), domains=("applytojob.com",), markers=("jazzhr", "applytojob")),
    _d("clearcompany", "ClearCompany", aliases=("clear company",), domains=("clearcompany.com",), markers=("clearcompany",)),
    _d("teamtailor", "Teamtailor", aliases=("team tailor",), domains=("teamtailor.com",), markers=("teamtailor",)),
    _d("pinpoint", "Pinpoint", domains=("pinpointhq.com",), markers=("pinpoint",)),
    _d("rippling", "Rippling ATS", aliases=("rippling recruiting",), domains=("rippling.com",), markers=("rippling recruiting",)),
    _d("paycom", "Paycom ATS", domains=("paycomonline.net",), markers=("paycom",)),
    _d("paylocity", "Paylocity Recruiting", domains=("recruiting.paylocity.com",), markers=("paylocity",)),
    _d("paycor", "Paycor Recruiting", domains=("paycor.com",), markers=("paycor recruiting",)),
    _d("paychex", "Paychex Applicant Tracking / OasisRecruit", aliases=("oasisrecruit", "paychex recruiting"), domains=("paychex.com",), markers=("paychex recruiting", "oasisrecruit")),
    _d("isolved", "isolved Talent Acquisition", aliases=("isolved recruiting",), domains=("isolvedhire.com",), markers=("isolved talent",)),
    _d("personio", "Personio Recruiting", domains=("personio.com",), markers=("personio",)),
    _d("zoho_recruit", "Zoho Recruit", aliases=("zoho",), domains=("zohorecruit.com",), markers=("zoho recruit",)),
    _d("freshteam", "Freshteam", domains=("freshteam.com",), markers=("freshteam",)),
    _d("manatal", "Manatal", domains=("manatal.com",), markers=("manatal",)),
    _d("loxo", "Loxo", domains=("loxo.co",), markers=("loxo",)),
    _d("recruiterflow", "Recruiterflow", domains=("recruiterflow.com",), markers=("recruiterflow",)),
    _d("recruitcrm", "Recruit CRM", aliases=("recruit crm",), domains=("recruitcrm.io",), markers=("recruit crm",)),
    _d("jobadder", "JobAdder", domains=("jobadder.com",), markers=("jobadder",)),
    _d("bullhorn", "Bullhorn", domains=("bullhornstaffing.com",), markers=("bullhorn",)),
    _d("avionte", "Avionté", aliases=("avionte",), domains=("avionte.com",), markers=("avionte",)),
    _d("ceipal", "CEIPAL ATS", aliases=("ceipal ats",), domains=("ceipal.com",), markers=("ceipal",)),
    _d("jobdiva", "JobDiva", aliases=("job diva",), domains=("jobdiva.com",), markers=("jobdiva",)),
    _d("crelate", "Crelate", domains=("crelate.com",), markers=("crelate",)),
    _d("pcrecruiter", "PCRecruiter", aliases=("pc recruiter",), domains=("pcrecruiter.net",), markers=("pcrecruiter",)),
    _d("trackerrms", "TrackerRMS", aliases=("tracker rms",), domains=("tracker-rms.com",), markers=("trackerrms", "tracker rms")),
    _d("vincere", "Vincere", domains=("vincere.io",), markers=("vincere",)),
    _d("tempworks", "TempWorks", aliases=("temp works",), domains=("tempworks.com",), markers=("tempworks",)),
    _d("akkencloud", "AkkenCloud", aliases=("akken cloud",), domains=("akkencloud.com",), markers=("akkencloud",)),
    _d("smartsearch", "SmartSearch / Advanced Personnel Systems", aliases=("smart search ats", "advanced personnel systems"), domains=("smartsearchonline.com",), markers=("smartsearch",)),
    _d("avature", "Avature", domains=("avature.net",), markers=("avature",)),
    _d("eightfold", "Eightfold AI", aliases=("eightfold",), domains=("eightfold.ai",), markers=("eightfold",)),
    _d("phenom", "Phenom", aliases=("phenom people",), domains=("phenompeople.com",), markers=("phenom",)),
    _d("beamery", "Beamery", domains=("beamery.com",), markers=("beamery",)),
    _d("gem", "Gem ATS", aliases=("gem recruiting",), domains=("gem.com",), markers=("gem recruiting",)),
    _d("paradox_olivia", "Paradox / Olivia", aliases=("paradox", "olivia recruiting"), domains=("paradox.ai",), markers=("paradox", "olivia")),
    _d("clinch_career_pages", "Clinch / career-pages.com", aliases=("clinch",), domains=("career-pages.com",), markers=("clinch",)),
    _d("radancy_talentbrew", "Radancy / TalentBrew", aliases=("radancy", "talentbrew"), domains=("talentbrew.com",), markers=("radancy", "talentbrew")),
    _d("talemetry", "Talemetry Career Sites", aliases=("talemetry",), domains=("talemetry.com",), markers=("talemetry",)),
    _d("findly_cws", "Symphony Talent / Findly CWS", aliases=("findly", "symphony talent"), domains=("findly.com",), markers=("findly", "symphony talent")),
    _d("getro", "Getro", domains=("getro.com",), markers=("getro",)),
    _d("join", "JOIN", aliases=("join ats",), domains=("join.com",), markers=("join recruiting",)),
    _d("softgarden", "Softgarden", domains=("softgarden.io",), markers=("softgarden",)),
    _d("factorial", "Factorial HR", aliases=("factorial",), domains=("factorialhr.com",), markers=("factorial hr",)),
    _d("polymer", "Polymer", aliases=("polymer hiring",), domains=("polymer.co",), markers=("polymer hiring",)),
    _d("deel", "Deel Hire", aliases=("deel hiring",), domains=("deel.com",), markers=("deel hire",)),
    _d("cornerstone_csod", "Cornerstone OnDemand / CSOD", aliases=("cornerstone", "csod"), domains=("csod.com",), markers=("cornerstone ondemand", "csod")),
    _d("ibm_kenexa_brassring", "IBM Kenexa BrassRing", aliases=("kenexa", "brassring", "ibm brassring"), domains=("brassring.com",), markers=("brassring", "kenexa")),
    _d("infor_cloudsuite_hcm", "Infor CloudSuite HCM", aliases=("infor hcm",), markers=("infor cloudsuite hcm",)),
    _d("peoplefluent", "PeopleFluent / PeopleClick RMS", aliases=("peopleclick", "people fluent"), domains=("peoplefluent.com",), markers=("peoplefluent", "peopleclick")),
    _d("pageup", "PageUp", aliases=("page up",), domains=("pageuppeople.com",), markers=("pageup",)),
    _d("silkroad", "SilkRoad Recruiting", aliases=("silkroad",), domains=("silkroad.com",), markers=("silkroad recruiting",)),
    _d("deltek", "Deltek Talent Management", aliases=("deltek talent",), markers=("deltek talent",)),
    _d("arcoro", "Arcoro / BirdDogHR", aliases=("birddoghr", "bird dog hr"), domains=("arcoro.com",), markers=("arcoro", "birddoghr")),
    _d("healthcaresource_symplr", "HealthcareSource / symplr", aliases=("healthcaresource", "symplr recruiting"), domains=("healthcaresource.com",), markers=("healthcaresource", "symplr recruiting")),
    _d("frontline_applitrack", "Frontline Education / AppliTrack", aliases=("applitrack", "frontline recruiting"), domains=("applitrack.com",), markers=("applitrack",)),
    _d("powerschool_talented_peopleadmin_schoolspring", "PowerSchool TalentEd / PeopleAdmin / SchoolSpring", aliases=("talented", "peopleadmin", "schoolspring", "powerschool talent"), domains=("schoolspring.com", "peopleadmin.com"), markers=("talented", "peopleadmin", "schoolspring")),
    _d("neogov", "NEOGOV / GovernmentJobs", aliases=("governmentjobs", "neo gov"), domains=("governmentjobs.com",), markers=("neogov", "governmentjobs")),
    _d("jobaps", "JobAps", aliases=("job aps",), domains=("jobapscloud.com",), markers=("jobaps",)),
    _d("calcareers", "CalCareers", domains=("calcareers.ca.gov",), markers=("calcareers",), category="government"),
    _d("statejobsny", "StateJobsNY", aliases=("state jobs ny",), domains=("statejobs.ny.gov",), markers=("statejobsny",), category="government"),
    _d("usajobs", "USAJOBS / USA Staffing", aliases=("usa staffing", "usajobs"), domains=("usajobs.gov",), markers=("usajobs", "usa staffing"), category="government"),
    _d("usps_applytohire", "USPS ApplyToHire", aliases=("applytohire", "usps careers"), markers=("applytohire",), category="government"),
    _d("nyc_teacher_support_network", "NYC Teacher Support Network", aliases=("teacher support network",), markers=("teacher support network",), category="education"),
    _d("winocular", "WinOcular", domains=("winocular.com",), markers=("winocular",)),
    _d("applicantpro", "ApplicantPro", domains=("applicantpro.com",), markers=("applicantpro",)),
    _d("applicantstack", "ApplicantStack", domains=("applicantstack.com",), markers=("applicantstack",)),
    _d("applicantpool", "ApplicantPool", domains=("applicantpool.com",), markers=("applicantpool",)),
    _d("hiringthing", "HiringThing", domains=("hiringthing.com",), markers=("hiringthing",)),
    _d("jobscore", "JobScore", domains=("jobscore.com",), markers=("jobscore",)),
    _d("hirebridge", "Hirebridge", domains=("hirebridge.com",), markers=("hirebridge",)),
    _d("trakstar_hire", "Trakstar Hire / Recruiterbox", aliases=("recruiterbox", "trakstar hire"), domains=("recruiterbox.com",), markers=("recruiterbox", "trakstar hire")),
    _d("exacthire", "ExactHire", domains=("exacthire.com",), markers=("exacthire",)),
    _d("eddy", "Eddy Hiring", aliases=("eddy hr",), domains=("eddy.com",), markers=("eddy hiring",)),
    _d("wizehire", "WizeHire", aliases=("wize hire",), domains=("wizehire.com",), markers=("wizehire",)),
    _d("cats", "CATS Applicant Tracking", aliases=("catsone", "cats ats"), domains=("catsone.com",), markers=("cats applicant tracking",)),
    _d("gohire", "GoHire", aliases=("go hire",), domains=("gohire.io",), markers=("gohire",)),
    _d("homerun", "Homerun", aliases=("homerun hr",), domains=("homerun.co",), markers=("homerun hiring",)),
    _d("occupop", "Occupop", domains=("occupop.com",), markers=("occupop",)),
    _d("careerspage", "CareersPage", aliases=("careers page",), domains=("careers-page.com",), markers=("careerspage",)),
    _d("applicantai", "ApplicantAI", aliases=("applicant ai",), domains=("applicantai.com",), markers=("applicantai",)),
    _d("careerpuck", "CareerPuck", aliases=("career puck",), domains=("careerpuck.com",), markers=("careerpuck",)),
    _d("hrm_direct", "HRM Direct", aliases=("hrmdirect",), domains=("hrmdirect.com",), markers=("hrm direct",)),
    _d("peopleforce", "PeopleForce", domains=("peopleforce.io",), markers=("peopleforce",)),
    _d("talentlyft", "TalentLyft", aliases=("talent lyft",), domains=("talentlyft.com",), markers=("talentlyft",)),
    _d("talexio", "Talexio", domains=("talexio.com",), markers=("talexio",)),
    _d("sagehr", "Sage HR Recruiting", aliases=("sage hr",), domains=("sage.hr",), markers=("sage hr recruiting",)),
    _d("careerplug", "CareerPlug", domains=("careerplug.com",), markers=("careerplug",)),
    _d("hireology", "Hireology", domains=("hireology.com",), markers=("hireology",)),
    _d("fountain", "Fountain", aliases=("fountain hiring",), domains=("fountain.com",), markers=("fountain",)),
    _d("workstream", "Workstream", aliases=("workstream hiring",), domains=("workstream.us",), markers=("workstream",)),
    _d("harri", "Harri", domains=("harri.com",), markers=("harri",)),
    _d("homebase", "Homebase Hiring", domains=("joinhomebase.com", "homebase.com"), markers=("homebase hiring",)),
    _d("talentreef", "TalentReef", domains=("talentreef.com",), markers=("talentreef",)),
    _d("apploi", "Apploi", domains=("apploi.com",), markers=("apploi",)),
    _d("oleeo", "Oleeo", domains=("oleeo.com",), markers=("oleeo",)),
    _d("yello", "Yello", domains=("yello.co",), markers=("yello recruiting",)),
    _d("comeet", "Comeet", domains=("comeet.com",), markers=("comeet",)),
    _d("dover", "Dover", aliases=("dover recruiting",), domains=("dover.com",), markers=("dover recruiting",)),
    _d("onehundredhires", "100Hires", aliases=("100 hires", "100hires"), domains=("100hires.com",), markers=("100hires",)),
    _d("ismartrecruit", "iSmartRecruit", aliases=("i smart recruit",), domains=("ismartrecruit.com",), markers=("ismartrecruit",)),
    _d("jobsoid", "Jobsoid", domains=("jobsoid.com",), markers=("jobsoid",)),
    _d("recooty", "Recooty", domains=("recooty.com",), markers=("recooty",)),
    _d("targetrecruit", "TargetRecruit", aliases=("target recruit",), domains=("targetrecruit.com",), markers=("targetrecruit",)),
    _d("firefish", "Firefish Software", aliases=("firefish",), domains=("firefishsoftware.com",), markers=("firefish",)),
    _d("happlicant", "Happlicant", domains=("happlicant.com",), markers=("happlicant",)),
    _d("recruitbpm", "RecruitBPM", aliases=("recruit bpm",), domains=("recruitbpm.com",), markers=("recruitbpm",)),
    _d("laboredge", "LaborEdge", aliases=("labor edge",), domains=("laboredge.com",), markers=("laboredge",)),
    _d("brightmove", "BrightMove", domains=("brightmove.com",), markers=("brightmove",)),
    _d("corecruit", "CoRecruit", aliases=("co recruit",), domains=("corecruit.com",), markers=("corecruit",)),
    _d("supportfinity", "SupportFinity", domains=("supportfinity.com",), markers=("supportfinity",)),
    _d("keka", "Keka Recruit", aliases=("keka",), domains=("keka.com",), markers=("keka recruit",)),
    _d("hireez", "hireEZ ATS / Recruiting", aliases=("hireez", "hire ez"), domains=("hireez.com",), markers=("hireez",)),
    _d("recruiterpm", "RecruiterPM", aliases=("recruiter pm",), domains=("recruiterpm.com",), markers=("recruiterpm",)),

    # US agency, higher-ed, transportation and long-lived legacy portals.
    _d("top_echelon", "Top Echelon / TE Recruit", aliases=("te recruit", "big biller", "top echelon"), domains=("topechelon.com",), markers=("te recruit", "top echelon")),
    _d("scout_talent", "Scout Talent :Recruit", aliases=("scout recruit", "scout talent recruit"), domains=("scouttalent.com",), markers=("scout talent", ":recruit")),
    _d("jobylon", "Jobylon", domains=("jobylon.com",), markers=("jobylon",)),
    _d("tenstreet", "Tenstreet / IntelliApp", aliases=("ten street", "intelliapp"), domains=("tenstreet.com",), markers=("tenstreet", "intelliapp")),
    _d("acquiretm", "AcquireTM", aliases=("acquire tm",), domains=("acquiretm.com",), markers=("acquiretm",)),
    _d("talentnest", "TalentNest", domains=("talentnest.com",), markers=("talentnest",)),
    _d("the_applicant_manager", "The Applicant Manager (TAM)", aliases=("applicant manager", "tam ats"), markers=("the applicant manager",)),
    _d("hiretouch", "HireTouch", aliases=("hire touch",), markers=("hiretouch",), category="education"),
    _d("newton_software", "Newton Software / Paycor Recruiting legacy", aliases=("newton software", "newton ats"), markers=("newton software",)),
    _d("ultrastaff_edge", "Ultra-Staff EDGE", aliases=("ultrastaff", "ultra staff", "ultra-staff edge"), markers=("ultra-staff edge", "ultrastaff")),
    _d("mystaffingpro", "myStaffingPro", aliases=("my staffing pro",), markers=("mystaffingpro",)),
    _d("balancetrak", "BALANCEtrak", aliases=("balance trak",), markers=("balancetrak",)),
    _d("ats_on_demand", "ATS On Demand", aliases=("ats on demand",), markers=("ats on demand",)),
    _d("nowhire", "NowHire", aliases=("now hire",), markers=("nowhire",)),
    _d("vikus", "Vikus", markers=("vikus",)),
    _d("virtus", "VIRTUS", aliases=("virtus ats",), markers=("virtus recruiting",)),

    # Recruitment/job-distribution platforms are detectable/readable but not
    # employer application systems; keep browser write authority review-only.
    _d("appcast_wordpress_jobs", "Appcast WordPress Jobs Plugin", aliases=("appcast",), domains=("appcast.io",), markers=("appcast",), category="career_platform", mode="review_only"),
    _d("attrax_cityjobs", "Attrax / CityJobs", aliases=("attrax",), domains=("attrax.co.uk",), markers=("attrax",), category="career_platform", mode="review_only"),
    _d("directemployers_jobs", "DirectEmployers / .jobs", aliases=("directemployers",), domains=("directemployers.org",), markers=("directemployers",), category="career_platform", mode="review_only"),
    _d("nlx_google_talent", "NLx / Google Talent", aliases=("national labor exchange", "nlx"), markers=("national labor exchange",), category="career_platform", mode="review_only"),

    _d("custom", "Company-hosted / custom / internal ATS", aliases=("internal ats", "custom_company_hosted", "unknown"), strategy=DiscoveryStrategy.STRUCTURED_WEB, mode="multi_page", category="custom"),
)


DEFINITIONS: dict[str, ATSDefinition] = {item.key: item for item in _DEFINITIONS}
if len(DEFINITIONS) != len(_DEFINITIONS):
    raise RuntimeError("duplicate ATS registry key")

_ALIAS_TO_KEY: dict[str, str] = {}
for _item in _DEFINITIONS:
    for _alias in (_item.key, _item.display_name, *_item.aliases):
        normalized = re.sub(r"[^a-z0-9]+", "_", _alias.casefold()).strip("_")
        previous = _ALIAS_TO_KEY.setdefault(normalized, _item.key)
        if previous != _item.key:
            raise RuntimeError(f"ambiguous ATS alias {_alias!r}: {previous!r} vs {_item.key!r}")


def platform_keys(*, include_custom: bool = True) -> tuple[str, ...]:
    keys = tuple(DEFINITIONS)
    return keys if include_custom else tuple(k for k in keys if k != "custom")


def discovery_platform_keys(*, include_custom: bool = True) -> tuple[str, ...]:
    keys = tuple(item.key for item in _DEFINITIONS if item.discovery_strategy != DiscoveryStrategy.EXTERNAL_SOURCE)
    return keys if include_custom else tuple(k for k in keys if k != "custom")


def normalize_ats_key(value: str | None, *, default: str = "custom") -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return default
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return _ALIAS_TO_KEY.get(normalized, default)


def get_definition(value: str | None) -> ATSDefinition:
    return DEFINITIONS[normalize_ats_key(value)]


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def detect_ats_platform(url: str | None, *, snapshot_text: str | None = None) -> str:
    """Conservatively detect a known ATS; unknowns return ``custom``."""
    raw_url = str(url or "")
    host = (urlsplit(raw_url).hostname or "").casefold()
    if host:
        matches: list[tuple[int, str]] = []
        for item in _DEFINITIONS:
            for suffix in item.candidate_domains:
                if _host_matches(host, suffix):
                    matches.append((len(suffix), item.key))
        if matches:
            matches.sort(reverse=True)
            return matches[0][1]

    text = str(snapshot_text or "").casefold()
    branding_cues = (
        "powered by", "careers powered by", "recruiting powered by",
        "applicant tracking", "candidate experience", "recruiting software",
        "application powered by", "jobs powered by",
    )
    if text and any(cue in text for cue in branding_cues):
        for item in _DEFINITIONS:
            if item.key == "custom":
                continue
            for marker in item.text_markers:
                marker = marker.casefold().strip()
                if not marker:
                    continue
                idx = text.find(marker)
                while idx >= 0:
                    window = text[max(0, idx - 96): idx + len(marker) + 96]
                    if any(cue in window for cue in branding_cues):
                        return item.key
                    idx = text.find(marker, idx + 1)
    return "custom"


def candidate_domain_rows() -> tuple[tuple[str, str, str], ...]:
    """Conservative global allowlist seed for dedicated candidate hosts only."""
    safe_domains = {
        "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
        "recruitee.com", "workable.com", "breezy.hr", "myworkdayjobs.com",
        "icims.com", "jibeapply.com", "taleo.net", "oraclecloud.com",
        "successfactors.com", "jobs2web.com", "jobvite.com",
        "workforcenow.adp.com", "myjobs.adp.com", "ultipro.com", "ukg.net",
        "dayforcehcm.com", "bamboohr.com", "applytojob.com", "teamtailor.com",
        "pinpointhq.com", "paycomonline.net", "recruiting.paylocity.com",
        "zohorecruit.com", "freshteam.com", "recruiterflow.com", "jobadder.com",
        "bullhornstaffing.com", "avature.net", "eightfold.ai", "career-pages.com",
        "csod.com", "brassring.com", "pageuppeople.com", "applitrack.com",
        "governmentjobs.com", "jobapscloud.com", "calcareers.ca.gov",
        "statejobs.ny.gov", "usajobs.gov", "applicantpro.com", "applicantstack.com",
        "applicantpool.com", "hiringthing.com", "jobscore.com", "hirebridge.com",
        "recruiterbox.com", "exacthire.com", "catsone.com", "softgarden.io",
        "factorialhr.com", "join.com", "talentlyft.com", "peopleforce.io",
        "careers-page.com", "darwinbox.in", "sparkhire.com", "eploy.co.uk",
    }
    rows: list[tuple[str, str, str]] = []
    for domain in sorted(safe_domains):
        definition = next((item for item in _DEFINITIONS if domain in item.candidate_domains), None)
        category = definition.category if definition else "ats"
        display = definition.display_name if definition else "ATS registry"
        rows.append((domain, category, f"ATS registry: {display}"))
    return tuple(rows)


def capability_rows() -> tuple[tuple[str, bool, bool, bool, bool, bool, bool, str, str], ...]:
    """DB seed rows for the generalized accessibility-snapshot form engine."""
    rows = []
    for item in _DEFINITIONS:
        browser = item.category in {"ats", "custom", "government", "education"}
        rows.append((
            item.key,
            item.discovery_strategy != DiscoveryStrategy.EXTERNAL_SOURCE,
            browser, browser, browser, browser,
            browser and item.autofill_mode in {"multi_page", "generic_browser"},
            item.autofill_mode if browser else "review_only",
            f"Registry identity; discovery={item.discovery_strategy.value}; "
            f"autofill_verification={item.autofill_verification.value}. "
            "Generic accessibility support is not a vendor-specific selector certification; "
            "writes remain page-bound, approval-bound, primitive-control checked, and safe-pause on unknown controls.",
        ))
    return tuple(rows)
