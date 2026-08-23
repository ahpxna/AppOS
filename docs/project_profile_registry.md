# Verified project profile registry

Run the local desktop form before tailoring a resume:

```bash
source .venv/bin/activate
python scripts/jobos_project_profile_app.py
```

On minimal Ubuntu desktop installations, install Tk first:

```bash
sudo apt install python3-tk
```

The form writes an untracked private JSON file at
`data/project-registry/project_profiles.json` (or the path passed by
`--path`). It does not call a browser, database, OpenClaw, or an LLM.

For each of the six pre-approved Word-template project blocks, fill in:

- the exact title, date, and GitHub URL you verified in the template;
- aliases that may occur in parsed documents, repository evidence, or approved
  profile asset titles;
- technologies, skills, JD keywords, factual summary, allowed claims, and
  explicit boundaries;
- source file paths, repository URLs, and other evidence locations.

`project_id`, block display name, and two resume slots are deliberately
read-only. The current template permits only CAROECT-D, CIG-AMF, PKI Sentinel,
ApplyOps, Enterprise NetSec IaC, and Optimixer. A new project needs a prepared
Word block (title/date/GitHub approved) before its catalog entry can be added.

## Mapping contract

`services.common.project_registry.map_parsed_profile_record()` takes a parsed
asset/document/repository record and returns a `jobos_project_mapping` object.
It looks only for the user-confirmed aliases in title/tags/text/source path.
No match returns `unmapped`; equally strong competing matches return
`ambiguous`. Neither case is silently attached to a project.

The resume generator loads these aliases to bind an approved `profile_asset` to
the correct immutable Word block. A valid asset from one project cannot become
a bullet under another project's heading. If the registry is malformed, the
generator uses its conservative built-in aliases and still fails closed for
unrecognized names.

For a future parser/aggregator that emits a JSON array, map it without an LLM
or GUI:

```bash
python scripts/jobos_project_profile_app.py \
  --map-input data/some_parsed_records.json \
  --map-output data/project-registry/some_parsed_records_mapped.json
```

The input may also be an object with a `records` or `assets` array. Each output
record retains its original fields and gets a `jobos_project_mapping` object.
