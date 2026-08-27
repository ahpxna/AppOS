import argparse
import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

# PATCH: module an toan dung chung (evidence strength + overclaim rules).
import sys as _sys
from pathlib import Path as _Path

_COMMON = _Path(__file__).resolve().parents[1] / "common"
if str(_COMMON.parent) not in _sys.path:
    _sys.path.insert(0, str(_COMMON.parent))
from common import jobos_safety as _safety  # noqa: E402
from common.llm_gateway import chat_text as _chat_text  # noqa: E402
from common import model_config as _model_config  # noqa: E402
from common.config import database_dsn  # noqa: E402



OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = _model_config.get_model("structured_evidence_unit")

STRUCTURED_SECTION_VERSION = "structured_section_boundary_v1_2026_04_27"
VERSION = "structured_evidence_unit_builder_qwen_v2_2026_04_27"


SYSTEM_PROMPT = """You are the Structured Evidence Unit Builder inside a job-application operating system.

You extract source-grounded evidence units from structured tool/framework inventory sections.

Do NOT write final resume content.
Do NOT create profile assets.
Do NOT approve evidence.
Do NOT invent hands-on use, professional experience, certifications, production deployment, awards, publications, or mastery.
Preserve evidence boundaries exactly.

For each section, return JSON only:
{
  "evidence_units": [
    {
      "claim": "specific claim supported by the section",
      "claim_type": "tool_experience|tool_exposure|framework_exposure|workflow_mapping|role_positioning|resume_safe_phrase|must_not_claim|job_market_target",
      "tool_name": "tool/framework/topic name, or empty string",
      "tool_category": "digital_forensics|dfir|network_security|web_application_security|grc|pki_tls|cryptography|data_database|software_engineering|security_analytics|job_market_alignment|unknown",
      "workflow_group": "forensic_acquisition|artifact_analysis|live_response|password_recovery|network_discovery|traffic_analysis|web_app_testing|enterprise_network_controls|pki_tls_validation|governance_control_mapping|data_security_analysis|security_automation|job_market_tooling|unknown",
      "evidence_strength": "direct_lab_use|project_use|coursework_exposure|material_exposure|job_market_target|guidance_only",
      "source_boundaries": {
        "document": "source file name",
        "section": "section title",
        "courses": ["CYB 320"],
        "labs": ["Lab 01"],
        "projects": [],
        "boundary_note": "controlled academic lab / coursework / material exposure / job target"
      },
      "evidence_summary": "grounded summary of what this section supports",
      "resume_safe_phrase": "safe phrase if explicitly supported, otherwise empty string",
      "role_relevance": ["SOC_analyst", "DFIR", "GRC", "AppSec", "NetworkSecurity", "SoftwareEngineering"],
      "must_not_claim": ["specific overclaim boundaries"],
      "supports_claims": ["claims this evidence can safely support"],
      "does_not_support_claims": ["claims this evidence must not support"],
      "competency_tags": ["specific competencies"],
      "tool_tags": ["tools/frameworks mentioned"],
      "project_tags": ["course/project/source tags"],
      "source_confidence": 0.90,
      "grounding_confidence": 0.90
    }
  ]
}

Return 0-2 evidence units. Prefer 1 high-quality evidence unit. Return 0 if the section is only a table of contents, page break artifact, or too vague.
"""


FORBIDDEN_OVERCLAIM_TERMS = [
    "professional experience",
    "production experience",
    "production deployment",
    "expert",
    "mastery",
    "enterprise lead",
    "certified",
    "certification earned",
    "employed as",
    "worked professionally",
    "advanced penetration tester",
]

ALLOWED_TOOL_CATEGORIES = {
    "digital_forensics",
    "dfir",
    "network_security",
    "web_application_security",
    "grc",
    "pki_tls",
    "cryptography",
    "data_database",
    "software_engineering",
    "security_analytics",
    "job_market_alignment",
    "unknown",
}

ALLOWED_WORKFLOW_GROUPS = {
    "forensic_acquisition",
    "artifact_analysis",
    "live_response",
    "password_recovery",
    "network_discovery",
    "traffic_analysis",
    "web_app_testing",
    "enterprise_network_controls",
    "pki_tls_validation",
    "governance_control_mapping",
    "data_security_analysis",
    "security_automation",
    "job_market_tooling",
    "unknown",
}

ALLOWED_EVIDENCE_STRENGTHS = {
    "direct_lab_use",
    "project_use",
    "coursework_exposure",
    "material_exposure",
    "job_market_target",
    "guidance_only",
}


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def lower_norm(s: Any) -> str:
    return norm(s).lower()


def parse_json_content(content: str) -> Dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Model output JSON must be an object.")


def call_ollama_json(prompt: str, model: str, retries: int = 2) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            content = _chat_text(
                role="structured_evidence_unit",
                model=model,
                local_url=OLLAMA_URL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                timeout=600,
                temperature=0.05,
                num_ctx=8192,
                json_mode=True,
            )
            return parse_json_content(content)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"LLM JSON call failed after {retries} retries: {last_error}")


def clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [norm(x) for x in value if isinstance(x, str) and norm(x)]
    if isinstance(value, str) and norm(value):
        return [norm(value)]
    return []


def clamp_float(value: Any, default: float = 0.90) -> float:
    try:
        x = float(value)
        return max(0.0, min(1.0, x))
    except Exception:
        return default


def extract_courses(text: str) -> List[str]:
    return sorted(set(re.findall(r"\b(?:CSC|CYB|CIS)\s*[_-]?\s*\d{3}\b", text, flags=re.I)))


def extract_labs(text: str) -> List[str]:
    return sorted(set(re.findall(r"\bLab\s*\d+\b", text, flags=re.I)))


def infer_tool_name(section_title: str) -> str:
    title = norm(section_title)
    title = re.sub(r"^\d+(?:\.\d+)+\s+", "", title).strip()
    title = re.sub(r"^\d+\.\s+", "", title).strip()
    return title[:180]


def infer_tool_category(title: str, body: str) -> str:
    blob = lower_norm(f"{title} {body[:1600]}")

    if any(x in blob for x in ["ftk", "autopsy", "forensic", "redline", "magnet", "regripper", "hxd", "bulk extractor", "email header", "browser history", "veracrypt", "pdfcrack"]):
        return "digital_forensics"
    if any(x in blob for x in ["nmap", "tcpdump", "wireshark", "tshark", "ping", "traceroute", "gns3", "cisco", "radius", "syslog", "ntp", "bgp", "vlan", "firewall"]):
        return "network_security"
    if any(x in blob for x in ["burp", "owasp", "juice shop", "zap", "sqlmap", "cookie", "http request", "web application"]):
        return "web_application_security"
    if any(x in blob for x in ["openssl", "apache https", "mod_ssl", "mitmproxy", "ocsp", "certificate", "tls", "pki"]):
        return "pki_tls"
    if any(x in blob for x in ["nist", "iso", "cis benchmark", "grc", "governance", "control", "compliance", "audit"]):
        return "grc"
    if any(x in blob for x in ["sql", "python", "pandas", "numpy", "scikit", "matplotlib"]):
        return "data_database"
    if any(x in blob for x in ["sdlc", "uml", "git", "github"]):
        return "software_engineering"
    if any(x in blob for x in ["splunk", "sentinel", "security onion", "zeek", "suricata", "soc"]):
        return "security_analytics"

    return "unknown"


def infer_workflow_group(category: str, title: str, body: str) -> str:
    blob = lower_norm(f"{title} {body[:2000]}")

    # More specific volatile/live-response signals must win before generic "acquisition".
    if any(x in blob for x in ["ram", "memory", "live acquisition", "process capture", "volatile", "redline", "magnet ram", "magnet process"]):
        return "live_response"
    if any(x in blob for x in ["forensic image", "ftk imager", "e01", "md5", "sha1", "hash verification", "evidence preservation", "physical image", "logical image"]):
        return "forensic_acquisition"
    if any(x in blob for x in ["artifact", "registry", "browser", "pagefile", "file system", "data carving", "hex"]):
        return "artifact_analysis"
    if any(x in blob for x in ["password", "credential", "john the ripper", "hashcat", "pdfcrack", "veracrypt"]):
        return "password_recovery"
    if any(x in blob for x in ["nmap", "discovery", "port scanning", "service detection", "ping", "traceroute"]):
        return "network_discovery"
    if any(x in blob for x in ["packet", "tcpdump", "wireshark", "tshark", "traffic"]):
        return "traffic_analysis"
    if any(x in blob for x in ["burp", "owasp", "juice shop", "sql injection", "xss", "web application"]):
        return "web_app_testing"
    if any(x in blob for x in ["gns3", "asav", "vios", "radius", "syslog", "ntp", "bgp", "acl", "vlan", "segmentation"]):
        return "enterprise_network_controls"
    if any(x in blob for x in ["openssl", "apache", "ocsp", "mitmproxy", "certificate", "tls", "pki", "netem"]):
        return "pki_tls_validation"
    if any(x in blob for x in ["nist", "iso", "cis benchmark", "control", "governance", "compliance", "audit"]):
        return "governance_control_mapping"
    if any(x in blob for x in ["sql", "python", "pandas", "numpy", "dashboard", "analytics"]):
        return "data_security_analysis"
    if "job" in blob or "role" in blob or "market" in blob:
        return "job_market_tooling"

    if category == "digital_forensics":
        return "artifact_analysis"
    if category == "network_security":
        return "network_discovery"
    if category == "web_application_security":
        return "web_app_testing"
    if category == "grc":
        return "governance_control_mapping"

    return "unknown"


def infer_evidence_strength(section_text: str) -> str:
    # PATCH C2: delegate sang module chung. Word-boundary + yeu cau dong tu
    # chu dong cho khang dinh manh. Xem services/common/jobos_safety.py
    return _safety.infer_evidence_strength(section_text)


def default_role_relevance(category: str, workflow_group: str) -> List[str]:
    mapping = {
        "digital_forensics": ["DFIR", "SOC_analyst", "GRC_evidence"],
        "network_security": ["NetworkSecurity", "SOC_analyst", "SecurityEngineering"],
        "web_application_security": ["AppSec", "SecurityAnalyst", "SoftwareEngineering"],
        "pki_tls": ["NetworkSecurity", "AppSec", "SecurityEngineering"],
        "grc": ["GRC", "SecurityCompliance", "SecurityAnalyst"],
        "data_database": ["SecurityAnalytics", "DataAnalyst", "SOC_analyst"],
        "software_engineering": ["SoftwareEngineering", "AppSec"],
        "security_analytics": ["SOC_analyst", "SecurityAnalytics"],
    }
    roles = mapping.get(category, ["SecurityAnalyst"])
    if workflow_group == "job_market_tooling":
        roles = list(dict.fromkeys(roles + ["JobMarketAlignment"]))
    return roles


def default_must_not_claim(section_text: str, strength: str) -> List[str]:
    low = lower_norm(section_text)
    rules = []

    if strength in {"material_exposure", "job_market_target", "guidance_only"}:
        rules.append("Do not claim direct hands-on use unless separately supported by lab or project evidence.")
    if "do not overclaim" in low or "not overstate" in low or "not that you mastered" in low:
        rules.append("Do not overstate mastery; use exposure/familiarity wording when direct use is not explicit.")
    if any(x in low for x in ["kali", "metasploit", "hydra", "patator", "sqlmap"]):
        rules.append("Do not imply unauthorized offensive use or advanced penetration-testing expertise.")
    if any(x in low for x in ["production", "enterprise"]):
        rules.append("Do not imply production or enterprise work experience beyond academic/project context.")

    if not rules:
        rules.append("Keep claim bounded to coursework, lab, project, or material context shown in the source.")

    return rules


def build_prompt(row: Dict[str, Any]) -> str:
    payload = {
        "document": {
            "file_name": row["file_name"],
            "document_type": row["document_type"],
            "source_role": row["source_role"],
        },
        "section": {
            "section_title": row["section_title"],
            "structured_section_kind": row["structured_section_kind"],
            "source_boundary_json": row["source_boundary_json"] or {},
            "section_text": row["section_text"],
        },
        "deterministic_hints": {
            "tool_name": infer_tool_name(row["section_title"]),
            "tool_category": infer_tool_category(row["section_title"], row["section_text"]),
            "workflow_group": infer_workflow_group(
                infer_tool_category(row["section_title"], row["section_text"]),
                row["section_title"],
                row["section_text"],
            ),
            "evidence_strength": infer_evidence_strength(row["section_text"]),
            "courses": extract_courses(row["section_text"]),
            "labs": extract_labs(row["section_text"]),
        },
    }

    return (
        "Extract structured evidence unit(s) from this section only. "
        "Use the deterministic hints, but correct them if the section text clearly supports a better value. "
        "Return JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )




def _source_contains_term(term: Any, section_title: str, section_text: str) -> bool:
    needle = lower_norm(term)
    if not needle:
        return False
    haystack = lower_norm(f"{section_title} {section_text}")
    pattern = re.escape(needle).replace(r"\ ", r"\s+")
    if needle[0].isalnum():
        pattern = r"(?<![a-z0-9])" + pattern
    if needle[-1].isalnum():
        pattern = pattern + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _grounded_list(value: Any, section_title: str, section_text: str) -> List[str]:
    return [
        item for item in clean_list(value)
        if _source_contains_term(item, section_title, section_text)
    ]

def normalize_unit(raw: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    section_text = row["section_text"] or ""
    section_title = row["section_title"] or ""
    file_name = row["file_name"] or ""

    det_category = infer_tool_category(section_title, section_text)
    det_workflow = infer_workflow_group(det_category, section_title, section_text)
    det_strength = infer_evidence_strength(section_text)

    claim_type_allowed = {
        "tool_experience",
        "tool_exposure",
        "framework_exposure",
        "workflow_mapping",
        "role_positioning",
        "resume_safe_phrase",
        "must_not_claim",
        "job_market_target",
    }

    claim_type = norm(raw.get("claim_type")) or "tool_exposure"
    if claim_type not in claim_type_allowed:
        claim_type = "tool_exposure"

    raw_tool_name = norm(raw.get("tool_name"))
    tool_name = (
        raw_tool_name if _source_contains_term(raw_tool_name, section_title, section_text)
        else infer_tool_name(section_title)
    )
    tool_category = norm(raw.get("tool_category")) or det_category
    workflow_group = norm(raw.get("workflow_group")) or det_workflow
    evidence_strength = norm(raw.get("evidence_strength")) or det_strength

    # Hard-normalize controlled fields. Qwen may return multiple workflow groups
    # in one string; one evidence unit should have one primary workflow group.
    if tool_category not in ALLOWED_TOOL_CATEGORIES:
        tool_category = det_category

    if "," in workflow_group or workflow_group not in ALLOWED_WORKFLOW_GROUPS:
        workflow_group = det_workflow

    if evidence_strength not in ALLOWED_EVIDENCE_STRENGTHS:
        evidence_strength = det_strength

    # PATCH C1: chot chan ba lop.
    # 1. model chi duoc HA cap so voi suy luan deterministic
    evidence_strength = _safety.clamp_strength(evidence_strength, det_strength)

    # 2. ha cap deterministic, ap cho MOI strength manh (khong chi direct_lab_use),
    #    cong denylist theo ten tool (Metasploit, sqlmap, Nessus, ...)
    evidence_strength, claim_type = _safety.apply_downgrades(
        evidence_strength, claim_type, section_text, tool_name
    )

    # 3. clamp lan cuoi phong truong hop apply_downgrades bi sua sau nay
    evidence_strength = _safety.clamp_strength(evidence_strength, det_strength)

    # Provenance belongs to the deterministic section producer, not the LLM.
    # Never let generated JSON replace the authoritative document/section or
    # invent course/lab/project boundaries.
    deterministic_boundary = (
        dict(row.get("source_boundary_json") or {})
        if isinstance(row.get("source_boundary_json"), dict) else {}
    )
    source_boundaries = {
        **deterministic_boundary,
        "document": file_name,
        "section": section_title,
        "tool_or_topic": deterministic_boundary.get("tool_or_topic") or infer_tool_name(section_title),
        "courses": extract_courses(section_text),
        "labs": extract_labs(section_text),
        "projects": _grounded_list(
            (raw.get("source_boundaries") or {}).get("projects")
            if isinstance(raw.get("source_boundaries"), dict) else [],
            section_title, section_text,
        ),
        "boundary_note": evidence_strength,
    }

    role_relevance = clean_list(raw.get("role_relevance")) or default_role_relevance(tool_category, workflow_group)

    raw_must_not_claim = clean_list(raw.get("must_not_claim"))
    must_not_claim = default_must_not_claim(section_text, evidence_strength)

    # Preserve useful model boundaries, but normalize common vague/odd forms.
    for item in raw_must_not_claim:
        low_item = lower_norm(item)

        if any(x in low_item for x in ["certification", "certified"]):
            must_not_claim.append("Do not claim certification unless separately supported by an official credential.")
        elif any(x in low_item for x in ["production", "enterprise", "professional"]):
            must_not_claim.append("Do not claim production, enterprise, or professional work experience beyond academic/project context.")
        elif any(x in low_item for x in ["developed", "created", "designed"]) and lower_norm(tool_name) in low_item:
            must_not_claim.append("Do not claim tool development, vendor-level expertise, or ownership of the tool itself.")
        elif "mastery" in low_item or "expert" in low_item:
            must_not_claim.append("Do not claim mastery or expert-level capability.")
        elif item:
            must_not_claim.append(item)

    must_not_claim = list(dict.fromkeys(must_not_claim))

    supports_claims = clean_list(raw.get("supports_claims"))
    if not supports_claims:
        supports_claims = [
            f"Can discuss {tool_name} in the context of {evidence_strength.replace('_', ' ')} and {workflow_group.replace('_', ' ')}."
        ]

    does_not_support_claims = clean_list(raw.get("does_not_support_claims"))
    if not does_not_support_claims:
        does_not_support_claims = must_not_claim[:]

    resume_safe_phrase = norm(raw.get("resume_safe_phrase"))
    claim = norm(raw.get("claim"))
    evidence_summary = norm(raw.get("evidence_summary"))

    if not claim:
        claim = f"{tool_name} supports {workflow_group.replace('_', ' ')} evidence in {evidence_strength.replace('_', ' ')} context."

    if not evidence_summary:
        evidence_summary = claim

    return {
        "claim": claim[:1200],
        "claim_type": claim_type,
        "tool_name": tool_name[:250],
        "tool_category": tool_category[:120],
        "workflow_group": workflow_group[:160],
        "source_boundaries": source_boundaries,
        "evidence_strength": evidence_strength[:120],
        "resume_safe_phrase": resume_safe_phrase[:1500],
        "role_relevance": role_relevance,
        "must_not_claim": must_not_claim,
        "evidence_type": "tool_workflow" if claim_type in {"tool_experience", "tool_exposure"} else "career_positioning",
        "evidence_title": f"{tool_name} — {workflow_group.replace('_', ' ').title()}"[:300],
        "direct_quote": "",
        "evidence_summary": evidence_summary[:4000],
        "supports_claims": supports_claims,
        "does_not_support_claims": does_not_support_claims,
        "role_families": role_relevance,
        "competency_tags": clean_list(raw.get("competency_tags")) or [workflow_group, tool_category],
        "tool_tags": _grounded_list(raw.get("tool_tags"), section_title, section_text) or ([tool_name] if tool_name else []),
        "project_tags": _grounded_list(raw.get("project_tags"), section_title, section_text) or extract_courses(section_text),
        "source_confidence": clamp_float(raw.get("source_confidence"), 0.90),
        "grounding_confidence": clamp_float(raw.get("grounding_confidence"), 0.90),
        "structured_extraction_json": raw,
    }


def validate_unit(unit: Dict[str, Any], row: Dict[str, Any]) -> tuple[bool, str]:
    # EUB v2 extracts from leaf sections such as "4.1 FTK Imager",
    # not overview sections such as "2. Master Tool Narrative".
    if not re.match(r"^\d+[.]\d+", norm(row.get("section_title"))):
        return False, "non_leaf_overview_section"

    # Tool/topic name can be broad for overview/role-positioning sections,
    # but should still exist as a topic label.
    if not unit["tool_name"] or len(unit["tool_name"]) < 2:
        return False, "missing_tool_or_topic_name"

    if len(unit["evidence_summary"]) < 60:
        return False, "evidence_summary_too_short"

    # Only scan positive claim surfaces for overclaim terms.
    # Do NOT scan must_not_claim or does_not_support_claims because those fields
    # are supposed to contain phrases like "do not claim production experience".
    positive_text = " ".join([
        unit["claim"],
        unit["evidence_summary"],
        unit["resume_safe_phrase"],
        " ".join(unit["supports_claims"]),
    ]).lower()

    boundary_text = " ".join([
        " ".join(unit["does_not_support_claims"]),
        " ".join(unit["must_not_claim"]),
    ]).lower()

    for term in FORBIDDEN_OVERCLAIM_TERMS:
        if term in positive_text:
            # Allow if the local wording is explicitly negated in the same positive surface.
            negated_patterns = [
                f"do not claim {term}",
                f"does not support {term}",
                f"must not claim {term}",
                f"not {term}",
                f"without claiming {term}",
            ]
            if any(p in positive_text for p in negated_patterns):
                continue
            return False, f"forbidden_overclaim_term:{term}"

    if unit["evidence_strength"] in {"job_market_target", "material_exposure", "guidance_only"}:
        risky = ["hands-on experience", "used ", "implemented ", "deployed "]
        if any(x in lower_norm(unit["claim"]) for x in risky) and "not directly used" in lower_norm(row["section_text"]):
            return False, "direct_use_claim_on_non_direct_evidence"

    return True, "ok"



def extract_labeled_span(text: str, labels: List[str]) -> str:
    text = text or ""
    label_pattern = "|".join(re.escape(x) for x in labels)
    stop_labels = [
        "Source:",
        "What it does:",
        "Portfolio positioning:",
        "Resume phrase:",
        "Resume-safe phrase:",
        "Resume-safe phrasing:",
        "Do not overclaim:",
        "Must not claim:",
        "Tools to learn next:",
    ]
    stop_pattern = "|".join(re.escape(x) for x in stop_labels)

    m = re.search(
        rf"(?:{label_pattern})\s*(.+?)(?=\s+(?:{stop_pattern})|$)",
        text,
        flags=re.I | re.S,
    )
    if not m:
        return ""

    return norm(m.group(1))[:1500]


def compact_text(text: str, max_chars: int = 900) -> str:
    cleaned = norm(text)
    cleaned = re.sub(r"--- PAGE \d+ ---", " ", cleaned, flags=re.I)
    return norm(cleaned)[:max_chars]


def deterministic_fallback_raw_unit(row: Dict[str, Any]) -> Dict[str, Any]:
    section_title = row.get("section_title") or ""
    section_text = row.get("section_text") or ""
    file_name = row.get("file_name") or ""

    tool_name = infer_tool_name(section_title)
    tool_category = infer_tool_category(section_title, section_text)
    workflow_group = infer_workflow_group(tool_category, section_title, section_text)
    evidence_strength = infer_evidence_strength(section_text)

    if evidence_strength in {"direct_lab_use", "project_use"}:
        claim_type = "tool_experience"
    elif evidence_strength == "job_market_target":
        claim_type = "job_market_target"
    else:
        claim_type = "tool_exposure"

    what_it_does = extract_labeled_span(section_text, ["What it does:"])
    positioning = extract_labeled_span(section_text, ["Portfolio positioning:"])
    resume_safe = extract_labeled_span(
        section_text,
        ["Resume phrase:", "Resume-safe phrase:", "Resume-safe phrasing:"],
    )

    evidence_summary_parts = [
        x for x in [what_it_does, positioning] if x
    ]

    if evidence_summary_parts:
        evidence_summary = " ".join(evidence_summary_parts)
    else:
        evidence_summary = compact_text(section_text, 900)

    if not evidence_summary:
        evidence_summary = (
            f"{tool_name} is represented in the structured tool inventory "
            f"for {workflow_group.replace('_', ' ')} with evidence strength "
            f"{evidence_strength.replace('_', ' ')}."
        )

    claim = (
        f"{tool_name} supports {workflow_group.replace('_', ' ')} "
        f"in a {evidence_strength.replace('_', ' ')} context, bounded to the source section."
    )

    source_boundaries = {
        "document": file_name,
        "section": section_title,
        "courses": extract_courses(section_text),
        "labs": extract_labs(section_text),
        "projects": [],
        "boundary_note": evidence_strength,
    }

    return {
        "claim": claim,
        "claim_type": claim_type,
        "tool_name": tool_name,
        "tool_category": tool_category,
        "workflow_group": workflow_group,
        "evidence_strength": evidence_strength,
        "source_boundaries": source_boundaries,
        "evidence_summary": evidence_summary,
        "resume_safe_phrase": resume_safe,
        "role_relevance": default_role_relevance(tool_category, workflow_group),
        "must_not_claim": default_must_not_claim(section_text, evidence_strength),
        "supports_claims": [
            f"Can discuss {tool_name} for {workflow_group.replace('_', ' ')} in the bounded source context."
        ],
        "does_not_support_claims": default_must_not_claim(section_text, evidence_strength),
        "competency_tags": [workflow_group, tool_category],
        "tool_tags": [tool_name],
        "project_tags": extract_courses(section_text),
        "source_confidence": 0.86,
        "grounding_confidence": 0.86,
        "fallback_used": True,
    }


def fetch_sections(cur, limit: int, file_like: Optional[str], force: bool):
    params: List[Any] = [STRUCTURED_SECTION_VERSION]

    file_filter = ""
    if file_like:
        file_filter = "AND rf.file_name ILIKE %s"
        params.append(f"%{file_like}%")

    exists_filter = ""
    if not force:
        exists_filter = """
        AND NOT EXISTS (
          SELECT 1
          FROM profile_evidence_units peu
          WHERE peu.profile_document_section_id = pds.id
            AND peu.builder_version = %s
        )
        """
        params.append(VERSION)

    params.append(limit)

    cur.execute(
        f"""
        SELECT
          pds.id AS section_id,
          pds.profile_document_id,
          pds.raw_file_id,
          pds.chunk_id,
          rf.file_name,
          pd.document_type,
          pd.source_role,
          pds.section_index,
          pds.section_title,
          pds.structured_section_kind,
          pds.source_boundary_json,
          pds.section_text
        FROM profile_document_sections pds
        JOIN raw_files rf ON rf.id = pds.raw_file_id
        JOIN profile_documents pd ON pd.id = pds.profile_document_id
        WHERE pds.model_notes = %s
          AND pds.section_title ~ '^[0-9]+[.][0-9]+'
          {file_filter}
          {exists_filter}
        ORDER BY rf.file_name, pds.section_index
        LIMIT %s
        """,
        params,
    )

    keys = [
        "section_id",
        "profile_document_id",
        "raw_file_id",
        "chunk_id",
        "file_name",
        "document_type",
        "source_role",
        "section_index",
        "section_title",
        "structured_section_kind",
        "source_boundary_json",
        "section_text",
    ]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def delete_existing_for_section(cur, section_id):
    cur.execute(
        """
        DELETE FROM profile_evidence_units
        WHERE profile_document_section_id = %s
          AND builder_version = %s
          AND status = 'draft'
        """,
        (section_id, VERSION),
    )


def insert_unit(cur, row: Dict[str, Any], unit: Dict[str, Any], model: str):
    cur.execute(
        """
        INSERT INTO profile_evidence_units (
          profile_document_id,
          profile_document_section_id,
          raw_file_id,
          chunk_id,

          evidence_type,
          evidence_title,
          direct_quote,
          evidence_summary,
          supports_claims,
          does_not_support_claims,
          role_families,
          competency_tags,
          tool_tags,
          project_tags,
          abstraction_level,
          source_confidence,
          grounding_confidence,
          status,
          builder_version,
          builder_model,

          claim,
          claim_type,
          tool_name,
          tool_category,
          workflow_group,
          source_boundaries,
          evidence_strength,
          resume_safe_phrase,
          role_relevance,
          must_not_claim,
          structured_extraction_json,
          extraction_strategy
        )
        VALUES (
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s,
          'structured_evidence_unit',
          %s, %s,
          'draft',
          %s, %s,

          %s, %s, %s, %s, %s,
          %s,
          %s, %s,
          %s, %s,
          %s,
          'structured_section_boundary_qwen_v2'
        )
        """,
        (
            row["profile_document_id"],
            row["section_id"],
            row["raw_file_id"],
            row["chunk_id"],

            unit["evidence_type"],
            unit["evidence_title"],
            unit["direct_quote"],
            unit["evidence_summary"],
            unit["supports_claims"],
            unit["does_not_support_claims"],
            unit["role_families"],
            unit["competency_tags"],
            unit["tool_tags"],
            unit["project_tags"],
            unit["source_confidence"],
            unit["grounding_confidence"],
            VERSION,
            model,

            unit["claim"],
            unit["claim_type"],
            unit["tool_name"],
            unit["tool_category"],
            unit["workflow_group"],
            Jsonb(unit["source_boundaries"]),
            unit["evidence_strength"],
            unit["resume_safe_phrase"],
            unit["role_relevance"],
            unit["must_not_claim"],
            Jsonb(unit["structured_extraction_json"]),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--file-like", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    print("===== STRUCTURED EVIDENCE UNIT BUILDER QWEN V2 =====")
    print(f"Version:   {VERSION}")
    print(f"Mode:      {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Force:     {args.force}")
    print(f"Limit:     {args.limit}")
    print(f"File like: {args.file_like}")
    print(f"Model:     {args.model}")
    print("")

    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            rows = fetch_sections(cur, args.limit, args.file_like, args.force)

            print(f"Structured sections selected: {len(rows)}")

            inserted = 0
            skipped = 0
            failed = 0

            for i, row in enumerate(rows, start=1):
                print("")
                print(f"--- Section {i}/{len(rows)} ---")
                print(f"File:    {row['file_name']}")
                print(f"Index:   {row['section_index']}")
                print(f"Section: {row['section_title']}")
                print(f"Kind:    {row['structured_section_kind']}")

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT structured_eub_v2_sp")
                try:
                    if args.force:
                        delete_existing_for_section(cur, row["section_id"])

                    prompt = build_prompt(row)
                    result = call_ollama_json(prompt, args.model)

                    raw_units = result.get("evidence_units", [])
                    if not isinstance(raw_units, list):
                        raise RuntimeError("Model output does not contain evidence_units list.")

                    section_inserted = 0
                    section_skipped = 0

                    for raw in raw_units[:2]:
                        if not isinstance(raw, dict):
                            continue

                        unit = normalize_unit(raw, row)
                        ok, reason = validate_unit(unit, row)

                        if not ok:
                            print(f"Skipped unit: {reason}")
                            section_skipped += 1
                            continue

                        insert_unit(cur, row, unit, args.model)
                        section_inserted += 1

                    if section_inserted == 0:
                        fallback_raw = deterministic_fallback_raw_unit(row)
                        unit = normalize_unit(fallback_raw, row)
                        ok, reason = validate_unit(unit, row)

                        if ok:
                            insert_unit(cur, row, unit, args.model)
                            section_inserted += 1
                            print("Inserted fallback unit.")
                        else:
                            print(f"Skipped fallback unit: {reason}")
                            section_skipped += 1

                    cur.execute("RELEASE SAVEPOINT structured_eub_v2_sp")
                    conn.commit()

                    inserted += section_inserted
                    skipped += section_skipped

                    print(f"Inserted: {section_inserted}")
                    print(f"Skipped:  {section_skipped}")

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT structured_eub_v2_sp")
                    cur.execute("RELEASE SAVEPOINT structured_eub_v2_sp")
                    failed += 1
                    print(f"FAILED: {e}")

    print("")
    print("===== SUMMARY =====")
    print(f"Sections selected: {len(rows)}")
    print(f"Evidence inserted: {inserted}")
    print(f"Units skipped:     {skipped}")
    print(f"Sections failed:   {failed}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
