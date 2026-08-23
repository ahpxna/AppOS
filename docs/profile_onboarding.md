# Profile onboarding on a new machine

This is the canonical path for private candidate data. Nothing in this guide
uploads a file, calls an LLM, or auto-approves a claim.

## 1. Stage private inputs

First complete the Ubuntu bootstrap so `.venv`, PostgreSQL, and `python-docx`
exist. Then copy files from their original locations with the helper rather
than placing them by hand:

```bash
source .venv/bin/activate

# Inspect what is still missing; no write.
python scripts/jobos_profile_onboarding.py status

# Copy the fixed Word template. It verifies the required immutable sections.
python scripts/jobos_profile_onboarding.py stage \
  --resume-template /absolute/path/to/your_resume_template.docx

# Classify source evidence. Repeat this command for transcripts, project notes,
# certificates, or exported GitHub/project descriptions as appropriate.
python scripts/jobos_profile_onboarding.py stage \
  --bucket official --source /absolute/path/to/resume.pdf --source /absolute/path/to/transcript.pdf
python scripts/jobos_profile_onboarding.py stage \
  --bucket project --source /absolute/path/to/project_description.md
```

The helper writes only to ignored local locations:

| Input | Local destination | Meaning |
| --- | --- | --- |
| Fixed Word resume template | `data/resume-template/` | Required by the template-preserving DOCX renderer. |
| Resume/transcript/certificates | `data/profile_sources_v2/00_official/` | Candidate evidence requiring later review. |
| Project descriptions | `data/profile_sources_v2/02_project_profiles/` | Project evidence; it is not proof of claims until reviewed. |
| Coursework mapping | `data/profile_sources_v2/01_course_profiles/` | Enriched academic evidence. |
| Guidance/reference material | `05_guidance_not_truth/` / `04_source_papers_and_course_readings/` | Context only; never candidate truth by itself. |

`--replace` is required before overwriting a staged file. Use `--dry-run` to
inspect a staging operation first.

## 2. Parse and ingest evidence

```bash
python services/profile-ingestion/parse_profile_sources_v2.py
python services/profile-ingestion/ingest_profile_sources_v2.py --apply
```

Review the resulting profile assets before approving them. Approval is manual
because it authorizes later resume/cover-letter claims; do not bulk approve
for convenience. The first command is read-only; `approve` is dry-run unless
you explicitly add `--apply`.

```bash
python scripts/jobos_profile_onboarding.py review
python scripts/jobos_profile_onboarding.py approve <asset-id> --note "Checked against source PDF"
python scripts/jobos_profile_onboarding.py approve <asset-id> --note "Checked against source PDF" --apply
```

Once appropriate assets/capabilities are approved, run:

```bash
python services/profile-ingestion/prepare_profile_for_pipeline_v1.py status
python services/profile-ingestion/prepare_profile_for_pipeline_v1.py build --apply
python services/orchestrator/pipeline_preflight_v1.py --json
```

## 3. Register the six fixed resume projects

```bash
python scripts/jobos_project_profile_app.py
```

This local GUI writes the ignored `data/project-registry/project_profiles.json`.
It records allowed facts, source locations, aliases, and boundaries for the six
pre-existing Word template blocks. It cannot change project names, dates, or
GitHub links in the template.

## 4. Legal profile and browser login

Enter F-1/OPT/STEM answers through the immigration CLI only after reviewing
each field; JobOS will not infer them. Login to LinkedIn or an ATS only in the
isolated browser yourself. Those two manual steps are intentional.
