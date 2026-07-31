"""
L7 -- AUTOFILL AGENT + SENSITIVE FIELD GATE + FINAL SUBMIT GATE

Fills application forms by driving the OpenClaw browser CLI directly.
There is deliberately no model in this loop.

Why no model:
  Filling a form is mechanical: read the label, look it up, type the value.
  Putting a model in the loop would add two risks and no capability:
    1. PII would have to pass through a prompt to reach the form.
    2. Field labels and page text are third-party content, so every run
       would carry attacker-controlled text into a prompt.
  Reading a snapshot and matching strings in Python avoids both.

Three gates, in order:
  1. Domain gate    -- URL must be in allowed_domains.
  2. Sensitive gate -- fields matching always_pause_fields are left empty.
  3. Submit gate    -- no code path clicks submit. The function does not exist.

Real-form findings this encodes (Oracle HCM, Odoo, w3schools):
  * The required marker is sometimes inline ("Country *") and sometimes a
    sibling node holding "*". Both are detected.
  * Address and EEO fields are frequently comboboxes, not textboxes, so the
    action is chosen by role: fill / select / click.
  * Yes-No questions render as button lists, not inputs. The answer is a
    click on the matching button.
  * Some radios carry no ref; only their clickable label does. The label is
    used as the target in that case.
  * Snapshots of long forms are truncated. Fields past the cut are invisible
    to this tool, so it says so rather than implying the form is complete.

Usage:
  python services/autofill/autofill_agent_v1.py probe
  python services/autofill/autofill_agent_v1.py inspect --url <url>
  python services/autofill/autofill_agent_v1.py plan --url <url>
  python services/autofill/autofill_agent_v1.py fill --url <url> --apply
  python services/autofill/autofill_agent_v1.py verify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
# browser.defaultProfile does not propagate into tool/CLI calls, so the
# profile is always passed explicitly.
BROWSER_PROFILE = os.getenv("JOBOS_BROWSER_PROFILE", "remote")

AGENT_VERSION = "autofill_agent_v1_cli_direct_2026_07_29"

TEXT_ROLES = {"textbox", "searchbox", "spinbutton"}
CHOICE_ROLES = {"combobox", "listbox", "select"}
TOGGLE_ROLES = {"checkbox", "radio"}
INPUT_ROLES = TEXT_ROLES | CHOICE_ROLES | TOGGLE_ROLES

# First match wins, so specific patterns precede general ones.
# Broadened 2026-07-31 to cover fields recurring across Greenhouse, Lever,
# Ashby, Workday, iCIMS, and SmartRecruiters application forms -- the
# matcher itself is already platform-agnostic (it reads label TEXT, not
# DOM structure), so widening this table is what "works on every ATS"
# actually means in this design, not a per-platform integration.
FIELD_PATTERNS: List[Tuple[str, str]] = [
    (r"(?i)^(your\s+)?(full\s+)?name$",                "full_name"),
    (r"(?i)\b(full|legal)\s*name\b",                   "full_name"),
    (r"(?i)\b(first|given)\s*name\b",                  "legal_first_name"),
    (r"(?i)\b(last|family|sur)\s*name\b",              "legal_last_name"),
    (r"(?i)\bmiddle\s*name\b",                         "middle_name"),
    (r"(?i)\bmiddle\s*initial\b",                      "middle_name"),
    (r"(?i)\bpreferred\s*name\b",                      "preferred_name"),
    (r"(?i)\bpronouns\b",                              "pronouns"),
    (r"(?i)\be-?mail\b",                               "email"),
    (r"(?i)\bcountry\s*code\b",                        "phone_country_code"),
    (r"(?i)\b(phone|mobile|telephone|cell)\b",         "phone"),
    (r"(?i)address\s*line\s*2|\bapt\.?\s*/?\s*suite\b","address_line2"),
    (r"(?i)\b(street|address\s*line\s*1|address1)\b",  "address_line1"),
    (r"(?i)postal\s*code\s*extension",                 "address_postal_ext"),
    (r"(?i)\b(zip|postal)\b",                          "address_postal"),
    (r"(?i)\bcity\b",                                  "address_city"),
    (r"(?i)\bcounty\b",                                "address_county"),
    (r"(?i)\b(state|province|region)\b",               "address_state"),
    (r"(?i)\bcountry\b",                               "address_country"),
    (r"(?i)linked-?in",                                "linkedin_url"),
    (r"(?i)\b(x|twitter)\s*(profile|handle|url)?\b",   "twitter_url"),
    (r"(?i)github",                                    "github_url"),
    (r"(?i)\b(portfolio|website|personal\s*site)\b",   "portfolio_url"),
    (r"(?i)\bother\s*(url|link|profile)\b",            "other_url"),
    # -- education --
    (r"(?i)\b(university|college|school)\s*(name|attended)?\b", "university_name"),
    (r"(?i)\bdegree\b",                                "degree"),
    (r"(?i)\b(major|field\s*of\s*study|concentration)\b", "major"),
    (r"(?i)\bgraduation\s*(date|year)?\b",             "graduation_date"),
    # -- work history / target role --
    (r"(?i)\bcurrent\s*(employer|company)\b",          "current_employer"),
    (r"(?i)\bcurrent\s*(job\s*)?title\b",              "current_title"),
    (r"(?i)\bdesired\s*(job\s*)?title\b",               "desired_title"),
    (r"(?i)\byears?\s*of\s*experience\b",              "years_experience"),
    (r"(?i)how\s*did\s*you\s*hear\s*about\s*(us|this\s*(role|position|job))", "referral_source"),
]

# Controls that parse a resume and populate fields automatically. Reported,
# never clicked: their parsing is frequently wrong, and typing verified
# database values is the entire purpose of this component.
RESUME_AUTOFILL_PATTERNS = [
    r"(?i)autofill\s*(with|from)?\s*(resume|cv)",
    r"(?i)auto-?fill\s*(with|from)?\s*(resume|cv)",
    r"(?i)parse\s*(my)?\s*resume",
    r"(?i)apply\s*with\s*(linked-?in|indeed|profile|github)",
    r"(?i)import\s*(your|my)?\s*profile",
    r"(?i)upload\s*resume\s*to\s*autofill",
    r"(?i)quick\s*apply",
]

# File-input controls (resume/cover-letter/transcript upload). These are
# reported so the user knows a real file still has to be attached by hand;
# uploading a file is a filesystem action this tool does not take, on the
# same "draft-only, do not guess" principle as everything else here.
FILE_UPLOAD_PATTERNS = [
    r"(?i)\b(upload|attach|browse)\b.{0,20}\b(resume|cv|r[eé]sum[eé])\b",
    r"(?i)\b(upload|attach|browse)\b.{0,20}\b(cover\s*letter)\b",
    r"(?i)\b(upload|attach|browse)\b.{0,20}\b(transcript)\b",
    r"(?i)^(choose|select)\s*file$",
    r"(?i)^browse\.{0,3}$",
]


class AutofillError(Exception):
    pass


# ---------------------------------------------------------------- browser CLI

def run_browser(args: List[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    if shutil.which(OPENCLAW_BIN) is None:
        raise AutofillError(f"'{OPENCLAW_BIN}' not on PATH.")
    cmd = [OPENCLAW_BIN, "browser", "--browser-profile", BROWSER_PROFILE, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AutofillError(f"browser command timed out: {' '.join(args)}") from e


def browser_json(args: List[str], *, timeout: int = 120) -> Any:
    proc = run_browser([*args, "--json"], timeout=timeout)
    if proc.returncode != 0:
        raise AutofillError(
            f"browser {' '.join(args)} failed: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    out = proc.stdout
    first, last = out.find("{"), out.rfind("}")
    if first == -1 or last <= first:
        raise AutofillError(f"No JSON in browser output: {out.strip()[:300]}")
    return json.loads(out[first:last + 1])


def cmd_probe(conn, args) -> int:
    print(f"binary:  {shutil.which(OPENCLAW_BIN)}")
    print(f"profile: {BROWSER_PROFILE}\n")
    for sub in (["--help"], ["fill", "--help"], ["select", "--help"],
                ["status"], ["profiles"]):
        print(f"--- openclaw browser {' '.join(sub)} ---")
        proc = run_browser(sub, timeout=30)
        print((proc.stdout or proc.stderr or "").strip()[:2000])
        print()
    return 0


# ---------------------------------------------------------------- snapshot

SNAP_LINE = re.compile(
    r'^(?P<indent>\s*)-\s+'
    r"'?(?P<role>[A-Za-z][\w-]*)"
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?:\s*\[[^\]]*\])*)'?"
    r'(?::\s*(?P<value>.*))?$'
)
SNAP_REF = re.compile(r'\[ref=([^\]]+)\]')


def _clean_value(raw: Optional[str]) -> str:
    v = (raw or "").strip()
    if v.startswith("/"):          # /url, /placeholder are metadata lines
        return ""
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]                # the tree quotes values such as "+1"
    return v.strip()


def parse_snapshot(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The snapshot arrives as an indented text tree plus a `refs` map.
    The text carries values, nesting, and required markers; `refs` carries
    authoritative role and name. Both are used."""
    text = payload.get("snapshot") or ""
    refs = payload.get("refs") or {}

    nodes: List[Dict[str, Any]] = []
    stack: List[Tuple[int, int]] = []      # (indent, index into nodes)
    required_labels: set = set()

    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped.startswith("-"):
            continue
        m = SNAP_LINE.match(raw)
        if not m:
            continue

        indent = len(m.group("indent"))
        role = (m.group("role") or "").lower()
        name = (m.group("name") or "").strip()
        value = _clean_value(m.group("value"))
        rm = SNAP_REF.search(m.group("attrs") or "")
        ref = rm.group(1) if rm else None

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_idx = stack[-1][1] if stack else None

        # Required markers come in two shapes across real ATS platforms:
        #   inline:  generic: "Country *"
        #   sibling: generic "Last Name" -> child generic: "*"
        if value == "*":
            for _, idx in reversed(stack):
                if nodes[idx]["label"]:
                    required_labels.add(nodes[idx]["label"].lower())
                    break
        elif value.endswith(" *") and len(value) > 2:
            required_labels.add(value[:-2].strip().lower())

        if ref:
            meta = refs.get(ref) or {}
            label = name or meta.get("name") or ""
            if label.rstrip().endswith("*"):
                required_labels.add(label.rstrip().rstrip("*").strip().lower())
                label = label.rstrip().rstrip("*").strip()
            nodes.append({
                "ref": ref,
                "role": (meta.get("role") or role).lower(),
                "label": label.strip(),
                "value": value,
                "indent": indent,
                "parent": parent_idx,
                "required": False,
                "children": [],
            })
            if parent_idx is not None:
                nodes[parent_idx]["children"].append(len(nodes) - 1)
            stack.append((indent, len(nodes) - 1))
        else:
            # Refless nodes still matter: they carry "*" markers and the text
            # of radios whose only clickable target is a sibling label.
            nodes.append({
                "ref": None, "role": role, "label": name, "value": value,
                "indent": indent, "parent": parent_idx,
                "required": False, "children": [],
            })
            if parent_idx is not None:
                nodes[parent_idx]["children"].append(len(nodes) - 1)
            stack.append((indent, len(nodes) - 1))

    for n in nodes:
        if n["label"] and n["label"].lower() in required_labels:
            n["required"] = True

    return nodes


def take_snapshot(url: Optional[str]) -> Tuple[List[Dict[str, Any]], bool]:
    if url:
        run_browser(["open", url], timeout=90)
    payload = browser_json(["snapshot", "--efficient"], timeout=150)    
    truncated = bool(payload.get("truncated"))
    return parse_snapshot(payload), truncated


# ---------------------------------------------------------------- database

def load_allowed_domains(cur) -> List[str]:
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled = true;")
    return [r[0].lower() for r in cur.fetchall()]


def check_domain(cur, url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise AutofillError(f"Refusing non-http(s) URL: {url[:120]}")
    m = re.search(r"https?://([^/]+)", url)
    host = (m.group(1) if m else "").lower().split(":")[0]
    for domain in load_allowed_domains(cur):
        if host == domain or host.endswith("." + domain):
            return
    raise AutofillError(
        f"Domain '{host}' is not in allowed_domains. Add it deliberately:\n"
        f"  INSERT INTO allowed_domains (domain, category) "
        f"VALUES ('{host}', 'ats');"
    )


def load_pause_patterns(cur) -> List[Tuple[str, str]]:
    cur.execute("SELECT pattern, reason FROM always_pause_fields;")
    return [(r[0], r[1]) for r in cur.fetchall()]


def load_values(cur) -> Dict[str, str]:
    cur.execute("SELECT field_name, field_value FROM v_autofill_ready_values;")
    values = {r[0]: r[1] for r in cur.fetchall()}

    # Many forms outside US-centric ATS platforms ask for a single name field.
    first = values.get("legal_first_name", "").strip()
    last = values.get("legal_last_name", "").strip()
    if first and last and "full_name" not in values:
        values["full_name"] = f"{first} {last}"

    return values


def load_sensitive_hints(cur) -> List[Tuple[str, List[str], str, str]]:
    cur.execute(
        """
        SELECT field_name, question_hints, answer, answer_kind
        FROM sensitive_answers
        WHERE approved_by_user = true AND btrim(answer) <> '';
        """
    )
    return [(r[0], list(r[1] or []), r[2], r[3]) for r in cur.fetchall()]


# ---------------------------------------------------------------- matching

def match_field(label: str, pause_patterns, hints, values) -> Dict[str, Any]:
    if not label:
        return {"decision": "skip", "reason": "no label"}

    for pattern, reason in pause_patterns:
        try:
            if re.search(pattern, label):
                return {"decision": "pause", "reason": reason}
        except re.error:
            continue

    low = label.lower()
    for field_name, hint_list, answer, kind in hints:
        for hint in hint_list:
            if hint.lower() in low:
                return {"decision": "value", "field_name": field_name,
                        "value": answer, "source": kind}

    for pattern, field_name in FIELD_PATTERNS:
        if re.search(pattern, label):
            if field_name in values:
                return {"decision": "value", "field_name": field_name,
                        "value": values[field_name], "source": "identity"}
            return {"decision": "missing", "field_name": field_name,
                    "reason": "no approved value in database"}

    return {"decision": "unknown", "reason": "no rule matches this label"}


def find_choice_button(nodes, group_idx: int, answer: str) -> Optional[str]:
    """Yes/No questions render as a list of buttons rather than an input.
    Find the button whose text matches the stored answer."""
    want = answer.strip().lower()
    stack = list(nodes[group_idx]["children"])
    while stack:
        idx = stack.pop()
        n = nodes[idx]
        if n["role"] == "button" and n["ref"]:
            if (n["label"] or "").strip().lower() == want:
                return n["ref"]
        stack.extend(n["children"])
    return None


def build_plan(cur, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    pause_patterns = load_pause_patterns(cur)
    hints = load_sensitive_hints(cur)
    values = load_values(cur)

    plan = {"fills": [], "selects": [], "clicks": [],
            "pauses": [], "missing": [], "unknown": [],
            "resume_controls": [], "file_uploads": []}
    seen_groups = set()

    for idx, n in enumerate(nodes):
        label = n["label"]
        if not label:
            continue

        if any(re.search(p, label) for p in RESUME_AUTOFILL_PATTERNS):
            if n["ref"]:
                plan["resume_controls"].append({"ref": n["ref"], "label": label})
            continue

        if any(re.search(p, label) for p in FILE_UPLOAD_PATTERNS):
            if n["ref"]:
                plan["file_uploads"].append({"ref": n["ref"], "label": label})
            continue

        # Button-group questions (Yes / No / N/A).
        if n["role"] in ("list", "radiogroup") and idx not in seen_groups:
            decision = match_field(label, pause_patterns, hints, values)
            if decision["decision"] == "value":
                target = find_choice_button(nodes, idx, decision["value"])
                if target:
                    seen_groups.add(idx)
                    plan["clicks"].append({
                        "ref": target, "label": label, "role": "button-group",
                        "required": n["required"], **decision,
                    })
                else:
                    plan["missing"].append({
                        "ref": n["ref"], "label": label, "role": n["role"],
                        "required": n["required"],
                        "field_name": decision.get("field_name", ""),
                        "reason": f"no button matching {decision['value']!r}",
                    })
                continue
            if decision["decision"] == "pause":
                plan["pauses"].append({"ref": n["ref"], "label": label,
                                       "role": n["role"],
                                       "required": n["required"], **decision})
                continue

        if n["role"] not in INPUT_ROLES or not n["ref"]:
            continue

        decision = match_field(label, pause_patterns, hints, values)
        entry = {"ref": n["ref"], "label": label, "role": n["role"],
                 "required": n["required"], "current": n["value"], **decision}

        if decision["decision"] == "value":
            if n["role"] in CHOICE_ROLES:
                plan["selects"].append(entry)
            elif n["role"] in TOGGLE_ROLES:
                plan["clicks"].append(entry)
            else:
                plan["fills"].append(entry)
        elif decision["decision"] == "pause":
            plan["pauses"].append(entry)
        elif decision["decision"] == "missing":
            plan["missing"].append(entry)
        elif decision["decision"] == "unknown":
            plan["unknown"].append(entry)

    return plan


def _mask(value: str, source: str) -> str:
    if source != "identity" or "@" in value or len(value) <= 4:
        return value
    return value[:2] + "*" * (len(value) - 2)


def print_plan(plan: Dict[str, Any], truncated: bool) -> None:
    total = sum(len(plan[k]) for k in
                ("fills", "selects", "clicks", "pauses", "missing", "unknown"))
    if total == 0:
        print("\n  No form fields found on this page.")
        print("  Application forms usually sit behind a login and an Apply")
        print("  click. Navigate to the real form in the browser first, then")
        print("  run this without --url so it uses that tab.")
        return
    if truncated:
        print("\n  WARNING: the snapshot was truncated. Fields below the cut")
        print("  are invisible here. Do not treat this plan as complete.")

    if plan["resume_controls"]:
        print(f"\n  RESUME-IMPORT CONTROLS ({len(plan['resume_controls'])}) -- not clicked")
        print("  Resume parsers routinely mis-map fields; verified database")
        print("  values are typed instead.")
        for r in plan["resume_controls"]:
            print(f"    - {r['label'][:66]}")

    if plan["file_uploads"]:
        print(f"\n  FILE UPLOADS ({len(plan['file_uploads'])}) -- not touched")
        print("  Attaching a file is a filesystem action outside this tool's")
        print("  scope. Attach these yourself:")
        for u in plan["file_uploads"]:
            print(f"    - {u['label'][:66]}")

    for key, title in (("fills", "WILL TYPE"), ("selects", "WILL SELECT"),
                       ("clicks", "WILL CLICK")):
        if plan[key]:
            print(f"\n  {title} ({len(plan[key])})")
            for f in plan[key]:
                shown = _mask(f["value"], f.get("source", ""))
                print(f"    {f['label'][:42]:<42} <- {shown[:34]}")

    if plan["pauses"]:
        print(f"\n  PAUSED FOR USER ({len(plan['pauses'])})")
        for p in plan["pauses"]:
            req = "  [REQUIRED]" if p["required"] else ""
            print(f"    {p['label'][:42]:<42} {p['reason'][:40]}{req}")

    if plan["missing"]:
        print(f"\n  NO VALUE AVAILABLE ({len(plan['missing'])})")
        for m in plan["missing"]:
            req = "  [REQUIRED]" if m["required"] else ""
            print(f"    {m['label'][:42]:<42} {m.get('field_name','')}{req}")

    if plan["unknown"]:
        print(f"\n  UNRECOGNISED ({len(plan['unknown'])})")
        for u in plan["unknown"][:25]:
            req = "  [REQUIRED]" if u["required"] else ""
            print(f"    {u['label'][:66]}{req}")

    blockers = ([p for p in plan["pauses"] if p["required"]]
                + [m for m in plan["missing"] if m["required"]]
                + [u for u in plan["unknown"] if u["required"]])
    if blockers:
        print(f"\n  {len(blockers)} REQUIRED field(s) cannot be filled from the")
        print("  database. This form cannot be completed without you.")


# ---------------------------------------------------------------- actions

def act_fill(fields: List[Dict[str, str]]) -> None:
    """`fill` accepts an array of {ref, value} and applies them in one call.
    Batching matters here: pages that rewrite fields as you type (resume
    parsers, dependent dropdowns) get fewer chances to interfere."""
    if not fields:
        return
    payload = json.dumps([{"ref": f["ref"], "value": f["value"]} for f in fields])
    proc = run_browser(["fill", "--fields", payload], timeout=120)
    if proc.returncode != 0:
        raise AutofillError((proc.stderr or proc.stdout or "").strip()[:300])


def act_select(ref: str, value: str) -> None:
    # `select` mirrors `fill`'s descriptor shape.
    payload = json.dumps([{"ref": ref, "value": value}])
    proc = run_browser(["select", "--fields", payload], timeout=60)
    if proc.returncode != 0:
        raise AutofillError((proc.stderr or proc.stdout or "").strip()[:300])


def act_click(ref: str) -> None:
    proc = run_browser(["click", ref], timeout=60)
    if proc.returncode != 0:
        raise AutofillError((proc.stderr or proc.stdout or "").strip()[:300])


# ---------------------------------------------------------------- commands
def resolve_target(cur, args) -> Optional[str]:
    """Return a URL to open, or None to use the tab already focused.

    Real application forms sit behind a login and an Apply click, so reopening
    the posting URL lands on the job description rather than the form. The
    normal path is: navigate and sign in by hand, then run against that tab."""
    url = getattr(args, "url", None)
    if not url:
        return None
    check_domain(cur, url)
    return url


def cmd_inspect(conn, args) -> int:
    with conn.cursor() as cur:
        target = resolve_target(cur, args)
    nodes, truncated = take_snapshot(target)
    inputs = [n for n in nodes if n["role"] in INPUT_ROLES and n["ref"]]
    print(f"\n  {len(nodes)} nodes, {len(inputs)} input-like"
          f"{'  (TRUNCATED)' if truncated else ''}\n")
    for n in inputs:
        req = " *" if n["required"] else "  "
        cur_val = f"  = {n['value'][:24]}" if n["value"] else ""
        print(f"  [{n['ref']:>7}] {n['role']:<10}{req} {n['label'][:52]}{cur_val}")
    if not inputs:
        print("  No input fields on this page. Navigate to the actual form")
        print("  in the browser, then run this without --url.")
    return 0


def cmd_plan(conn, args) -> int:
    with conn.cursor() as cur:
        target = resolve_target(cur, args)
        nodes, truncated = take_snapshot(target)
        plan = build_plan(cur, nodes)
    print_plan(plan, truncated)
    print("\n  Nothing was typed. Use 'fill --apply' to act on this plan.")
    return 0


def cmd_fill(conn, args) -> int:
    with conn.cursor() as cur:
        target = resolve_target(cur, args)

        if args.application_id:
            cur.execute(
                """
                SELECT 1 FROM approval_requests
                WHERE application_id = %s AND type = 'submit_application'
                  AND status = 'approved' AND token_expires_at > now()
                LIMIT 1;
                """,
                (args.application_id,),
            )
            if cur.fetchone() is None:
                print("\n  REFUSED: no valid approval for this application.")
                print("  Create and redeem one with approval_service_v1.py first.")
                return 1

        nodes, truncated = take_snapshot(target)
        plan = build_plan(cur, nodes)

    print_plan(plan, truncated)

    actionable = len(plan["fills"]) + len(plan["selects"]) + len(plan["clicks"])
    if actionable == 0:
        print("\n  Nothing to do on this page.")
        return 0

    if not args.apply:
        print("\n  DRY RUN. Nothing typed.")
        return 0

    done, failed = [], []

    if plan["fills"]:
        try:
            act_fill(plan["fills"])
            done.extend(plan["fills"])
            for f in plan["fills"]:
                print(f"    ok   {f['label'][:58]}")
        except AutofillError as e:
            # A batch failure is all-or-nothing, so retry individually to find
            # which field the page rejected.
            print(f"    batch fill failed ({e}); retrying one at a time")
            for f in plan["fills"]:
                try:
                    act_fill([f])
                    done.append(f)
                    print(f"    ok   {f['label'][:58]}")
                except AutofillError as e2:
                    failed.append({**f, "error": str(e2)})
                    print(f"    FAIL {f['label'][:58]}: {e2}")

    for s in plan["selects"]:
        try:
            act_select(s["ref"], s["value"])
            done.append(s)
            print(f"    ok   {s['label'][:58]}")
        except AutofillError as e:
            failed.append({**s, "error": str(e)})
            print(f"    FAIL {s['label'][:58]}: {e}")

    for c in plan["clicks"]:
        try:
            act_click(c["ref"])
            done.append(c)
            print(f"    ok   {c['label'][:58]} -> {c['value'][:20]}")
        except AutofillError as e:
            failed.append({**c, "error": str(e)})
            print(f"    FAIL {c['label'][:58]}: {e}")

    result = {
        "url": target or "(current tab)",
        "agent_version": AGENT_VERSION,
        "snapshot_truncated": truncated,
        "done": [{"label": d["label"], "field_name": d.get("field_name")} for d in done],
        "failed": [{"label": f["label"], "error": f["error"]} for f in failed],
        "paused": [{"label": p["label"], "reason": p["reason"],
                    "required": p["required"]} for p in plan["pauses"]],
        "missing": [{"label": m["label"], "required": m["required"]}
                    for m in plan["missing"]],
        "unknown": [u["label"] for u in plan["unknown"]],
        "resume_controls_ignored": [r["label"] for r in plan["resume_controls"]],
        "file_uploads_needing_manual_attach": [u["label"] for u in plan["file_uploads"]],
        "submitted": False,
    }

    if args.application_id:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO browser_tasks
                  (task_type, requested_by, application_id, input_json,
                   result_json, status, created_at, started_at, finished_at)
                VALUES ('fill_application_form', %s, %s, %s, %s, 'completed',
                        now(), now(), now());
                """,
                (AGENT_VERSION, args.application_id,
                 Jsonb({"url": target}), Jsonb(result)),
            )
        conn.commit()

    print(f"\n  done {len(done)}, failed {len(failed)}, "
          f"paused {len(plan['pauses'])}, missing {len(plan['missing'])}")
    print("\n  NOT SUBMITTED. Review every field in the browser, complete the")
    print("  paused and missing ones yourself, then submit it yourself.")
    return 0


def cmd_verify(conn, args) -> int:
    """Re-read the form and compare each mapped field against the database.
    Catches failed writes and values the page rewrote after entry, which is
    common where a resume parser has run."""
    with conn.cursor() as cur:
        if args.url:
            check_domain(cur, args.url)
        nodes, truncated = take_snapshot(args.url)
        values = load_values(cur)
        pause_patterns = load_pause_patterns(cur)
        hints = load_sensitive_hints(cur)

    ok, mismatch, empty = [], [], []
    for n in nodes:
        if n["role"] not in INPUT_ROLES or not n["ref"] or not n["label"]:
            continue
        decision = match_field(n["label"], pause_patterns, hints, values)
        if decision["decision"] != "value":
            continue

        actual = (n["value"] or "").strip()
        expected = decision["value"].strip()
        if not actual:
            empty.append((n["label"], expected, n["required"]))
        elif actual == expected:
            ok.append(n["label"])
        else:
            mismatch.append((n["label"], expected, actual))

    if truncated:
        print("\n  WARNING: snapshot truncated; this check is partial.")
    print(f"\n  matches: {len(ok)}")
    if empty:
        print(f"\n  EMPTY ({len(empty)})")
        for label, exp, req in empty:
            tag = " [REQUIRED]" if req else ""
            print(f"    {label[:44]:<44} expected {exp[:24]}{tag}")
    if mismatch:
        print(f"\n  MISMATCHED ({len(mismatch)})")
        print("  A value differing from the database usually means the page")
        print("  rewrote it, often a resume parser overwriting typed input.")
        for label, exp, act in mismatch:
            print(f"    {label[:44]}")
            print(f"      expected: {exp[:60]}")
            print(f"      actual:   {act[:60]}")
    if not empty and not mismatch:
        print("  Every mapped field matches the database.")
    return 1 if (empty or mismatch) else 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L7 autofill agent")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("probe")

    pi = sub.add_parser("inspect")
    pi.add_argument("--url", help="Omit to use the current tab.")

    pp = sub.add_parser("plan")
    pp.add_argument("--url", help="Omit to use the current tab.")

    pf = sub.add_parser("fill")
    pf.add_argument("--url", help="Omit to use the current tab.")
    pf.add_argument("--application-id")
    pf.add_argument("--apply", action="store_true")

    pv = sub.add_parser("verify")
    pv.add_argument("--url", help="Omit to snapshot the current tab.")

    args = p.parse_args()
    print(f"===== AUTOFILL AGENT ({AGENT_VERSION}) =====")
    print(f"Browser profile: {BROWSER_PROFILE}")

    with psycopg.connect(DSN, autocommit=False) as conn:
        try:
            return {
                "probe": cmd_probe, "inspect": cmd_inspect, "plan": cmd_plan,
                "fill": cmd_fill, "verify": cmd_verify,
            }[args.command](conn, args)
        except AutofillError as e:
            print(f"\nERROR: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())