"""
jobos_safety.py - nguon su that duy nhat cho lop chong-bia cua JobOS.

Ly do ton tai: truoc day logic suy luan evidence_strength va lap
do_not_overclaim_rules bi copy o nhieu script. Sua mot cho khong sua cho khac
=> lo hong. Tu gio moi script import tu day.

Nguyen tac cot loi:
  1. So khop PHAI co word boundary. "lab" khong duoc khop "collaborate".
  2. Bang chung phu dinh THANG bang chung khang dinh.
  3. Model chi duoc HA cap, KHONG duoc NANG cap so voi suy luan deterministic.
  4. do_not_overclaim_rules chi chua CAU CAM, khong chua danh tu tran.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# 1. Word-boundary matcher
# --------------------------------------------------------------------------

def _wb(*words: str) -> re.Pattern:
    """
    Tao regex khop nguyen tu, khong khop chuoi con.

    _wb("lab").search("collaborate")  -> None      (dung)
    "lab" in "collaborate"            -> True      (sai, bug cu)

    Dung (?<![\\w-]) thay vi \\b de dau gach ngang cung tinh la ranh gioi,
    tranh "job-market" bi cat thanh "job" + "market".
    Python re la unicode-aware nen \\w bao gom ca chu co dau tieng Viet.
    """
    body = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![\w-])(?:" + body + r")(?![\w-])", re.IGNORECASE)


# --------------------------------------------------------------------------
# 2. Tin hieu ngon ngu (Anh + Viet, vi corpus la song ngu)
# --------------------------------------------------------------------------

RE_LEARN_NEXT = _wb(
    "tools to learn next", "learn next", "to learn next",
    "job-market tools", "job market tools",
    "common job-description", "high-priority framework to align next",
    "need-to-learn", "need to learn",
    "can hoc them", "se hoc", "muc tieu hoc",
)

RE_HEDGE = _wb(
    # English
    "familiar", "familiarity", "exposure", "exposed",
    "not directly used", "if not directly used", "did not use",
    "material", "materials", "tool list", "reading",
    "do not overclaim", "not overstate", "not that you mastered",
    "should not claim", "avoid claiming",
    # Vietnamese
    "chua dung truc tiep", "khong truc tiep", "chi doc", "tham khao",
    "lam quen", "tiep xuc", "khong nen noi qua", "dung noi qua",
)

RE_GUIDANCE = _wb(
    "should", "you can say", "recommend", "recommended",
    "positioning", "phrasing", "wording",
    "nen noi", "nen viet", "goi y", "huong dan",
)

RE_LAB = _wb(
    "lab", "labs", "lab environment", "controlled lab", "virtual lab",
    "phong lab", "bai lab",
)

RE_PROJECT = _wb(
    "project", "projects", "capstone", "coursework project",
    "do an", "du an",
)

RE_ACTIVE = _wb(
    # English - dong tu chu dong thuc su lam
    "used", "configured", "implemented", "deployed", "performed",
    "conducted", "built", "executed", "captured", "parsed",
    "analyzed", "validated", "tested", "simulated", "measured",
    # Vietnamese
    "da dung", "da cau hinh", "trien khai", "thuc hien",
    "da chay", "da xay dung", "da phan tich", "da do",
)

# --------------------------------------------------------------------------
# 3. Thang do do manh cua bang chung
# --------------------------------------------------------------------------

ALLOWED_EVIDENCE_STRENGTHS = {
    "direct_lab_use",
    "project_use",
    "coursework_exposure",
    "material_exposure",
    "job_market_target",
    "guidance_only",
}

# Cang cao cang la khang dinh manh. Dung de chan model nang cap.
STRENGTH_RANK: Dict[str, int] = {
    "guidance_only": 0,
    "job_market_target": 1,
    "material_exposure": 2,
    "coursework_exposure": 3,
    "project_use": 4,
    "direct_lab_use": 5,
}

STRONG_STRENGTHS = {"direct_lab_use", "project_use"}

# Tool ma tai lieu goc noi ro la exposure-only.
# Bat ky claim manh nao ve mot trong so nay deu bi ha cap khong dieu kien.
HEDGED_TOOLS = {
    "metasploit", "sqlmap", "nessus", "openvas", "hydra", "patator",
    "hashcat", "john the ripper", "owasp zap", "zap", "burp suite",
    "wireshark", "tshark", "splunk", "sentinel", "nmap",
}


def infer_evidence_strength(section_text: str) -> str:
    """
    Suy luan deterministic do manh cua bang chung tu text nguon.

    Thu tu quan trong: yeu nhat truoc. Bang chung phu dinh
    ("familiar", "chua dung truc tiep") LUON thang bang chung khang dinh.

    Khang dinh manh (lab/project) doi hoi CA HAI:
      - ngu canh (lab / project)
      - dong tu chu dong (used / configured / da dung ...)
    Chi co chu "project" trong mot cau huong dan thi KHONG du.
    """
    t = section_text or ""
    has_active = bool(RE_ACTIVE.search(t))

    # Yeu nhat truoc. guidance_only (rank 0) < job_market_target (1)
    # < material_exposure (2). Text thuan huong dan, khong co dong tu chu dong
    # nao, thi khong phai bang chung gi ca.
    if not has_active and RE_GUIDANCE.search(t):
        return "guidance_only"

    if RE_LEARN_NEXT.search(t):
        return "job_market_target"

    if RE_HEDGE.search(t):
        return "material_exposure"

    if RE_LAB.search(t) and has_active:
        return "direct_lab_use"

    if RE_PROJECT.search(t) and has_active:
        return "project_use"

    return "coursework_exposure"


def clamp_strength(model_value: Optional[str], deterministic_value: str) -> str:
    """
    Model chi duoc HA cap, khong duoc NANG cap.

    Day la chot chan quan trong nhat cua ca he thong. Neu qwen tra
    "project_use" cho mot section noi "familiar with Metasploit",
    ham nay keo no ve "material_exposure".
    """
    mv = (model_value or "").strip().lower()
    if mv not in ALLOWED_EVIDENCE_STRENGTHS:
        return deterministic_value
    if STRENGTH_RANK.get(mv, 99) > STRENGTH_RANK.get(deterministic_value, 0):
        return deterministic_value
    return mv


def apply_downgrades(
    evidence_strength: str,
    claim_type: str,
    section_text: str,
    tool_name: str = "",
) -> tuple[str, str]:
    """
    Ha cap deterministic. Ap cho MOI strength manh, khong chi direct_lab_use.
    Tra ve (evidence_strength, claim_type) da chuan hoa.
    """
    t = section_text or ""

    if RE_LEARN_NEXT.search(t):
        return "job_market_target", "job_market_target"

    if RE_HEDGE.search(t) and evidence_strength in STRONG_STRENGTHS:
        evidence_strength = "material_exposure"
        if claim_type == "tool_experience":
            claim_type = "tool_exposure"

    # Chot chan theo ten tool: tai lieu goc da noi ro may cai nay la exposure.
    if tool_name and evidence_strength in STRONG_STRENGTHS:
        low_tool = tool_name.strip().lower()
        if any(h in low_tool for h in HEDGED_TOOLS):
            evidence_strength = "material_exposure"
            if claim_type == "tool_experience":
                claim_type = "tool_exposure"

    # Dong bo claim_type voi evidence_strength.
    if evidence_strength in STRONG_STRENGTHS and claim_type == "tool_exposure":
        claim_type = "tool_experience"
    if evidence_strength in {"material_exposure", "guidance_only"} and claim_type == "tool_experience":
        claim_type = "tool_exposure"
    if evidence_strength == "job_market_target":
        claim_type = "job_market_target"

    return evidence_strength, claim_type


# --------------------------------------------------------------------------
# 4. do_not_overclaim_rules - chi chua cau cam
# --------------------------------------------------------------------------

RULE_START = re.compile(
    r"^(do not|don't|does not|doesn't|do NOT|never|must not|cannot|can't|"
    r"no claims?|not a |not an |avoid|keep|bounded to|limit|"
    r"requires? (separate|validation|verification)|"
    r"theoretical|simulated|synthetic)\b",
    re.IGNORECASE,
)

# Gia tri enum / tag ky thuat khong bao gio duoc lam rule.
_ENUM_LIKE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")


def as_overclaim_rule(item: Any) -> Optional[str]:
    """
    Chuan hoa mot phan tu thanh CAU CAM, hoac loai bo.

    "Do not claim production experience."  -> giu nguyen
    "cloud infrastructure management"      -> "Do not claim cloud infrastructure management."
    "direct_lab_use"                       -> None  (enum lot vao)
    "pki_tls"                              -> None  (tag)
    """
    s = re.sub(r"\s+", " ", str(item or "")).strip()
    if not s or len(s) < 4 or len(s) > 320:
        return None
    if s.lower() in ALLOWED_EVIDENCE_STRENGTHS:
        return None
    if _ENUM_LIKE.match(s.lower()):
        return None
    if RULE_START.match(s):
        return s if s.endswith((".", "!")) else s + "."
    return f"Do not claim {s.rstrip('.')}."


BASE_OVERCLAIM_RULES = [
    "Do not present academic lab, coursework, or project exposure as professional employment.",
    "Do not claim production deployment, certification, or expert mastery unless separately supported.",
]


def build_overclaim_rules(*sources: Iterable[Any], cap: int = 25) -> List[str]:
    """
    Gop nhieu nguon thanh mot danh sach rule sach, khong trung, co gioi han.
    Moi nguon co the la list, str, hoac None.
    """
    out: List[str] = []
    for src in sources:
        if src is None:
            continue
        items = src if isinstance(src, (list, tuple, set)) else [src]
        for item in items:
            rule = as_overclaim_rule(item)
            if rule:
                out.append(rule)
    out.extend(BASE_OVERCLAIM_RULES)
    seen, uniq = set(), []
    for r in out:
        k = r.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq[:cap]


# --------------------------------------------------------------------------
# 5. Self-test - chay: python -m services.common.jobos_safety
# --------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0

    def check(name: str, got: Any, want: Any) -> None:
        global failures
        if got != want:
            failures += 1
            print(f"FAIL {name}\n     got  = {got!r}\n     want = {want!r}")
        else:
            print(f"ok   {name}")

    # Word boundary
    check("lab khong khop collaborate", RE_LAB.search("we collaborate daily"), None)
    check("lab khong khop syllabus", RE_LAB.search("see the syllabus"), None)
    check("lab khop lab that", bool(RE_LAB.search("in the lab we used")), True)
    check("project khong khop projection",
          RE_PROJECT.search("map projection method"), None)

    # Bang chung phu dinh thang
    check("familiar -> material_exposure",
          infer_evidence_strength(
              "Familiar with Metasploit as an authorized exploit-validation "
              "framework from penetration-testing materials; primary hands-on "
              "validation work used OSSTMM-style scanning."),
          "material_exposure")

    check("learn next -> job_market_target",
          infer_evidence_strength("Tools to learn next: Splunk, Sentinel."),
          "job_market_target")

    # Khang dinh manh can dong tu chu dong
    check("lab + used -> direct_lab_use",
          infer_evidence_strength("In the CYB 320 lab we used FTK Imager to "
                                  "create and verify forensic images."),
          "direct_lab_use")

    check("project khong dong tu -> coursework",
          infer_evidence_strength("Which class or project does this come from?"),
          "coursework_exposure")

    # clamp: model khong duoc nang cap
    check("clamp chan nang cap",
          clamp_strength("direct_lab_use", "material_exposure"),
          "material_exposure")
    check("clamp cho phep ha cap",
          clamp_strength("material_exposure", "direct_lab_use"),
          "material_exposure")
    check("clamp loai gia tri la",
          clamp_strength("super_expert", "project_use"),
          "project_use")

    # downgrade theo ten tool
    check("Metasploit bi ha cap",
          apply_downgrades("project_use", "tool_experience",
                           "used in a project", "Metasploit"),
          ("material_exposure", "tool_exposure"))
    check("FTK giu nguyen",
          apply_downgrades("direct_lab_use", "tool_experience",
                           "used in the lab", "FTK Imager"),
          ("direct_lab_use", "tool_experience"))

    # rule cleaning
    check("enum bi loai", as_overclaim_rule("direct_lab_use"), None)
    check("tag bi loai", as_overclaim_rule("pki_tls"), None)
    check("danh tu -> cau cam",
          as_overclaim_rule("cloud infrastructure management"),
          "Do not claim cloud infrastructure management.")
    check("cau cam giu nguyen",
          as_overclaim_rule("Do not claim production experience"),
          "Do not claim production experience.")

    rules = build_overclaim_rules(
        ["Do not claim production experience"],
        ["cloud infrastructure management", "direct_lab_use", "pki_tls"],
    )
    check("build_overclaim_rules sach",
          all(RULE_START.match(r) for r in rules), True)

    print("")
    if failures:
        print(f"===== {failures} TEST THAT BAI =====")
        raise SystemExit(1)
    print("===== TAT CA TEST PASS =====")
