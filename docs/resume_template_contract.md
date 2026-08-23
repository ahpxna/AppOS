# Resume template contract

Reference template: `data/resume-template/VU PHAN AN NGUYEN-official_For_all.docx`.
Reference SHA-256: `d2e7aa168e14db506478c64a62f2c95070c1245139b4f1bc1087d4913033bd50`.
The authoritative visual target is the user-supplied one-page Letter PDF.

The template has one portrait Letter section: left/right margins 1.00 in, top
0.51 in, bottom 0.42 in. Arial is the dominant font; the template uses direct
run formatting and must be copied rather than rebuilt.

Preserve verbatim: name/contact, Education, Experience & Activities, project
headings/dates, Certifications, all page geometry, paragraph styles, headings,
tabs, bullets, and font sizes.

The renderer snapshots every protected paragraph plus every hyperlink target
before editing and compares them after saving. A mismatch blocks the output.
Therefore neither the LLM nor template renderer can rename a project, change a
date, replace your GitHub URL, or insert an arbitrary external link.

Editable slots only:

- 12 existing `List Paragraph` items between `PROJECTS` and `CERTIFICATIONS`:
  project-bullet text only, in order, no inserted rows.
- Five paragraphs directly after `SKILLS`: existing category/item rows only.
- The six header subtitles only: text between the fixed project name `—` and
  the fixed `| GitHub` link. Project name, GitHub target/text, and date stay
  protected.

The six approved project blocks are fixed. The model chooses only their
description slots; it cannot choose, rename, insert, or link a different
project:

| Slots | Immutable project block |
| --- | --- |
| 1–2 | CAROECT-D |
| 3–4 | CIG-AMF |
| 5–6 | PKI Sentinel |
| 7–8 | ApplyOps |
| 9–10 | Enterprise NetSec IaC |
| 11–12 | Optimixer |

For each selected block, its odd-numbered primary slot is capped at 200
characters (the measured two-line budget) and its even-numbered secondary slot
is capped at 105 characters (the measured one-line budget). A secondary slot is
invalid without the primary slot directly before it. The generator must select
only blocks whose profile asset directly supports a JD requirement, rank skill
rows by JD relevance and evidence, and keep a mix of relevant skill categories.
It must omit weakly related or unproven skills instead of filling space.

Unselected bullet text is cleared, so a generated resume never inherits a
generic/irrelevant project description from the template. Its project heading,
date, GitHub link, paragraph style, and page geometry remain unchanged. The
current approved template therefore retains the six fixed headings even when
only some blocks have tailored bullet text; dynamic removal or reordering of a
whole block is intentionally not part of this version.

Each project description is also bound to that block's approved profile asset
by a hard-coded title/alias map. A fact from an approved project can never be
placed under another project's title. If an approved asset's title does not
match the map, that project block fails closed until its alias is deliberately
added in `FIXED_RESUME_PROJECT_ASSET_TERMS`.

## Header-subtitle change audit

The editable subtitle is not a free rewrite. Each proposed subtitle must carry
its exact previous subtitle, a verbatim JD requirement quote, a verbatim quote
from the cited project asset, an overall reason it is more accurate/relevant,
and a before/after rationale covering every substantive changed term. The
generator rejects malformed metadata. The truth checker independently verifies
the quotes against the original JD and the approved asset and then asks the
verifier model to judge the complete change. Any rejected subtitle change is a
fatal QA result: no DOCX is exported.

Project bullets use the same strict contract. A replacement bullet must name
the exact prior template bullet, quote the JD and its matching approved project
asset verbatim, explain why the new wording is more relevant, and account for
every substantive changed term. The bullet verifier rejects new tools, results,
responsibilities, or experience not confirmed by that project asset. A project
without a real JD match is omitted rather than creatively reframed.

The DOCX is the working artifact. The renderer copies it for each verified
application and changes only fixed text runs in the 12 project-bullet slots and
five skill rows; Word retains the template's direct run/paragraph formatting.
The user opens the resulting file in Word and chooses Print/Save as PDF. This
avoids LibreOffice pagination drift. The renderer rejects more than 12 project
bullets or five skill rows and never shrinks font/spacing.
