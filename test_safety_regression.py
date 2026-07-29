#!/usr/bin/env python3
"""
test_safety_regression.py - chung minh lo hong C1/C2 da bit.

Chay tu goc repo SAU khi da apply patch:
    python jobos_patch/test_safety_regression.py

Khong can DB, khong can Ollama. Chi import ham va goi truc tiep.
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "services" / "profile-ingestion").is_dir():
            return p
    raise SystemExit("FATAL: chay tu goc repo job-apply-os")


ROOT = find_repo_root(Path.cwd().resolve())
sys.path.insert(0, str(ROOT / "services" / "profile-ingestion"))
sys.path.insert(0, str(ROOT / "services"))

import build_structured_evidence_units_qwen_v2 as B  # noqa: E402

FAIL = 0


def check(name: str, got, want) -> None:
    global FAIL
    if got != want:
        FAIL += 1
        print(f"FAIL {name}\n     got  = {got!r}\n     want = {want!r}")
    else:
        print(f"ok   {name}")


# Text that thay tu section 8.2 Metasploit trong corpus cua m.
METASPLOIT = (
    "Familiar with Metasploit as an authorized exploit-validation framework "
    "from penetration-testing materials; primary hands-on validation work "
    "used OSSTMM-style scanning, enumeration and attack-chain analysis."
)

FTK_LAB = (
    "In the CYB 320 forensics lab we used FTK Imager to create and verify "
    "forensic disk images, then validated hash integrity."
)

GUIDANCE = (
    "Portfolio positioning: You should say you understand the workflow, "
    "not that you mastered the tool. Which class or project does this come from?"
)

LEARN_NEXT = "Tools to learn next: Splunk, Microsoft Sentinel, deeper Wireshark."


def row(text: str, title: str) -> dict:
    return {
        "section_text": text,
        "section_title": title,
        "file_name": "PROFILE__tool_workflow_mapping__v01.pdf",
        "section_id": "test",
        "profile_document_id": "test",
        "raw_file_id": "test",
        "chunk_id": None,
        "structured_section_kind": "structured_tool_section",
    }


print("=== 1. infer_evidence_strength (deterministic) ===")
check("Metasploit 'familiar' -> material_exposure",
      B.infer_evidence_strength(METASPLOIT), "material_exposure")
check("FTK lab + used -> direct_lab_use",
      B.infer_evidence_strength(FTK_LAB), "direct_lab_use")
check("guidance khong dong tu -> guidance_only",
      B.infer_evidence_strength(GUIDANCE), "guidance_only")
check("learn next -> job_market_target",
      B.infer_evidence_strength(LEARN_NEXT), "job_market_target")

print("")
print("=== 2. word boundary (bug C2 cu) ===")
check("'collaborate' KHONG thanh direct_lab_use",
      B.infer_evidence_strength("Analysts collaborate on incident reports daily "
                                "and used the shared queue."),
      "coursework_exposure")
check("'projection' KHONG thanh project_use",
      B.infer_evidence_strength("The map projection was used for the diagram."),
      "coursework_exposure")

print("")
print("=== 3. model KHONG duoc nang cap (bug C1 cu) ===")

# Truoc patch: qwen tra project_use cho Metasploit -> giu nguyen -> len CV.
u = B.normalize_unit(
    {
        "tool_name": "Metasploit",
        "claim_type": "tool_experience",
        "evidence_strength": "project_use",      # qwen noi lao
        "evidence_summary": "x" * 80,
        "claim": "Used Metasploit for exploit validation in a project.",
    },
    row(METASPLOIT, "8.2 Metasploit"),
)
check("qwen 'project_use' cho Metasploit bi ha",
      u["evidence_strength"], "material_exposure")
check("claim_type theo do ha xuong tool_exposure",
      u["claim_type"], "tool_exposure")

u2 = B.normalize_unit(
    {
        "tool_name": "Metasploit",
        "claim_type": "tool_experience",
        "evidence_strength": "direct_lab_use",   # qwen noi lao kieu khac
        "evidence_summary": "x" * 80,
        "claim": "Ran Metasploit modules in the lab.",
    },
    row(METASPLOIT, "8.2 Metasploit"),
)
check("qwen 'direct_lab_use' cho Metasploit cung bi ha",
      u2["evidence_strength"], "material_exposure")

# Bang chung that thi phai duoc giu.
u3 = B.normalize_unit(
    {
        "tool_name": "FTK Imager",
        "claim_type": "tool_experience",
        "evidence_strength": "direct_lab_use",
        "evidence_summary": "x" * 80,
        "claim": "Created and verified forensic images with FTK Imager.",
    },
    row(FTK_LAB, "4.1 FTK Imager"),
)
check("FTK Imager giu direct_lab_use", u3["evidence_strength"], "direct_lab_use")
check("FTK Imager giu tool_experience", u3["claim_type"], "tool_experience")

print("")
print("=== 4. do_not_overclaim_rules sach (bug H3 cu) ===")
sys.path.insert(0, str(ROOT / "services"))
from common import jobos_safety as S  # noqa: E402

rules = S.build_overclaim_rules(
    ["Do not claim production experience"],
    ["cloud infrastructure management", "physical network operations"],
    ["direct_lab_use", "pki_tls", "security_automation"],
)
check("khong con enum trong rules",
      any(r.lower().strip(".") in S.ALLOWED_EVIDENCE_STRENGTHS for r in rules), False)
check("khong con tag gach duoi",
      any("_" in r and " " not in r for r in rules), False)
check("moi rule deu la cau cam",
      all(S.RULE_START.match(r) for r in rules), True)
check("danh tu da duoc nang thanh cau cam",
      "Do not claim cloud infrastructure management." in rules, True)

print("")
if FAIL:
    print(f"===== {FAIL} TEST THAT BAI. DUNG, dung chay pipeline. =====")
    raise SystemExit(1)
print("===== TAT CA TEST PASS. Lop chong-bia da hoat dong. =====")
