# Migration numbering — read this before adding a new file

Apply order is plain filename sort. `for f in db/migrations/*.sql; do psql ... -f "$f"; done`
in that order, every time, including on a brand-new database.

## Before running against a real database

Run `python scripts/migration_lint.py` from the repo root first. It statically simulates the
whole migration sequence in order (no database needed) and catches the three bug classes
documented below — bad `ON CONFLICT` targets against partial unique indexes, `CREATE OR
REPLACE VIEW` column reorders, and `CREATE TABLE IF NOT EXISTS` no-ops against a table an
earlier file already created with a different schema — across every file in one pass, before
you spend time on a real install. It's not a full SQL engine (see its own docstring for exact
limits) but it's what found and confirmed the fixes below, and it's self-tested against
synthetic bad input (4/4 cases) so a clean run means something. It exits non-zero and prints
every issue with a file reference if it finds anything; exit 0 and "No issues found" means
none of these three bug classes exist anywhere in the current 49 files.

## Known duplicate/lettered numbers (historical, left as-is)

A few numbers exist more than once: `007` / `007_seed_mock_profile_fixed` / `007a`,
`024` / `024a`, `025` / `025a` / `025b`, `030_profile_asset_deepseek_review_views` /
`030_structured_profile_evidence_schema`, `031_profile_asset_audits_structured_workflow` /
`031_profile_capability_builder_tables` / `031b`.

These were not renamed, on purpose: renaming an already-applied migration file doesn't
undo what it already did to anyone's existing database, and there is no migration-tracking
table in this project (no `schema_migrations` row per file) — reordering filenames only
changes fresh-install behavior, and could make people believe a rename fixed something on
a database it never touched. Each of these was checked individually:

- `007_seed_mock_profile.sql` vs `007_seed_mock_profile_fixed.sql` vs `007a_fix_conflict_indexes.sql`,
  `024` vs `024a`, `025` vs `025a` vs `025b` — sibling "fix the previous file" patches for
  the deprecated atom-fact pipeline (see `026_deprecate_atom_fact_pipeline_and_create_profile_assets.sql`).
  Alphabetical sort runs the base file before its `a`/`b`/`_fixed` patch, which is what each
  patch assumes — **but this assumption silently depended on the runner tolerating errors on
  the base file.** `007_seed_mock_profile.sql` specifically had a real bug (`ON CONFLICT
  (input_hash)` didn't match the partial unique index on `profile_context_packs.input_hash`,
  see `003_extended_schema.sql`) that made it fail on every fresh install, confirmed live by
  a real install attempt on 2026-08-01. Under a lenient runner (errors printed, loop
  continues) this was harmless in practice: `007_seed_mock_profile_fixed.sql` ran right after
  and did the real seeding. Under `psql -v ON_ERROR_STOP=1` (used by `scripts/apply_migrations.sh`,
  added after this file was first written), the base file's failure aborts the whole run and
  never reaches the fix — a strict runner turns a "harmless known issue" into a hard blocker.
  **Fixed in place** on 2026-08-01, same precedent as the 031 fix below: this file never
  successfully applied on any install, so there was nothing to preserve by leaving it broken.
  A second pass the same day found `007_seed_mock_profile.sql` had the *identical* bug on its
  `raw_files` insert too (`ON CONFLICT (sha256)` vs. the partial `idx_raw_files_sha256`,
  `WHERE sha256 IS NOT NULL`) — confirmed live, a real install got past the first fix and
  failed on this second one immediately after. `007_seed_mock_profile_fixed.sql` had the exact
  same `sha256` bug (it was only ever fixed for `input_hash`, not `sha256`) and was fixed too,
  so the file that's supposed to be "the fixed one" is now actually fully fixed, not just
  partially. `007a_fix_conflict_indexes.sql` was left as-is; its full-unique-index on
  `raw_files(sha256)` is now redundant belt-and-suspenders, harmless to keep.

  There are **9 partial unique indexes** across `db/migrations/*.sql` in total (found by
  grepping every `CREATE UNIQUE INDEX ... WHERE ... IS NOT NULL`, not by memory):
  `browser_tasks.idempotency_key`, `browser_tasks.approval_request_id`,
  `approval_requests.idempotency_key`, `raw_files.sha256`,
  `profile_context_packs.input_hash`, `candidate_profile_facts.dedup_key`,
  `applications.jd_hash`, `cost_ledger.component_run_id`, `messages.external_id`. Every
  `ON CONFLICT` clause in every migration file was cross-checked against this list on
  2026-08-01 — only the two `raw_files.sha256` / `profile_context_packs.input_hash` cases
  above (both confined to the `007` pair) were missing their `WHERE ... IS NOT NULL`
  predicate. (An earlier verification pass claimed "no other partial unique index exists" —
  that was wrong, based on an incomplete grep that missed multi-line `CREATE UNIQUE INDEX`
  statements. This note replaces that claim with the actual full list.)
  `024`/`024a`, `025`/`025a`/`025b` don't touch any of the 9 columns above, so this failure
  mode does not apply to them.
- `030_profile_asset_deepseek_review_views.sql` (a view) vs `030_structured_profile_evidence_schema.sql`
  (unrelated ALTER TABLEs on `profile_documents`/`profile_document_sections`/`profile_evidence_units`).
  Checked: the view's actual dependencies (`profile_asset_audits`, `profile_asset_evidence_items`,
  `profile_assets.compiler_version`) all come from migration 026, which runs first. No hazard.
- `031_profile_asset_audits_structured_workflow.sql` **was** a real, breaking bug — it assumed
  `profile_asset_audits` didn't exist yet and tried to `CREATE INDEX` on columns
  (`audit_status`, `recommended_action`) that don't exist on the table `027` actually
  creates. On a fresh install this raised `column "audit_status" does not exist` and
  aborted the migration's transaction. **Fixed in place** on 2026-07-31 (see the comment
  block at the top of that file) rather than left as a landmine, because it never
  successfully applied its own schema on any install — there is nothing to preserve by
  leaving it broken.

## Second bug class found 2026-08-01: `CREATE OR REPLACE VIEW` column reorder

`CREATE OR REPLACE VIEW` can only *append* new columns at the end of an existing view's
column list — it cannot reorder, rename, or insert columns among ones that already exist,
and raises `cannot change name of view column "X" to "Y"` if you try. Three migrations did
this to a view that an earlier migration had already created with a different column
layout, all confirmed live on a real install:

- `024_profile_retrieval_signals.sql` — `v_profile_retrieval_latest_results` (first created
  in `023_profile_retrieval_api.sql`) had 4 new signal columns inserted in the middle of the
  SELECT list. **Fixed** by moving them to the end, after every column `023` already had, in
  their original order.
- `025_candidate_fact_semantic_dedup.sql` — `v_candidate_fact_dedup_review` (first created in
  `010_semantic_dedup.sql`) was given an entirely different column layout with no `DROP VIEW`
  first. **Fixed** by adding `DROP VIEW IF EXISTS` right before the `CREATE OR REPLACE VIEW`
  — safe because `025a_fix_candidate_fact_dedup_schema.sql` already does its own
  `DROP VIEW` + recreate for this same view right after, so the final shape is unaffected
  either way; this just stops the run from aborting before reaching `025a`.
- `031_profile_capability_builder_tables.sql` — `v_profile_capability_review` (first created
  in `027_profile_intelligence_layer.sql`) inserted `builder_version`/`builder_model` before
  `role_families`, shifting every later column. **Fixed** the same way: `DROP VIEW IF EXISTS`
  added before the recreate, safe because `031b_recreate_profile_capability_review_views.sql`
  already drops + recreates this view again right after.

All `CREATE OR REPLACE VIEW <name>` occurrences across every migration file were
cross-referenced (by view name, across files) to find every case where a later migration
redefines a view an earlier one already created without a preceding `DROP VIEW` in the same
file. This surfaced 9 such pairs; 3 had the bug above (now fixed), and the other 6 were
checked column-by-column and confirmed to match exactly — `v_profile_asset_deepseek_review`,
`v_profile_asset_approval_candidates`, `v_profile_asset_deepseek_audit_summary` (all
`030_profile_asset_deepseek_review_views.sql` → `041_wiring_fixes_and_gates.sql`),
`v_documents_pending_qa` (`034` → `041`), and `v_autofill_ready_values` (`038` → `041`) — all
five are `041`'s own changes from the previous verification pass, and only changed `WHERE`
clauses or appended trailing columns, never reordered anything, so no fix was needed there.

## Third bug class found 2026-08-01: `CREATE TABLE IF NOT EXISTS` on a table an earlier migration already created with a different schema

`CREATE TABLE IF NOT EXISTS` is a full no-op if the table already exists — it does not add
new columns, so a later migration that assumes a richer schema than an earlier migration
already committed will fail the moment it tries to index or select a column that was never
added. `025_candidate_fact_semantic_dedup.sql` did this against `010_semantic_dedup.sql`,
confirmed live: `010` creates `candidate_fact_dedup_groups` with `status`/no `dedup_version`
etc.; `025`'s `CREATE TABLE IF NOT EXISTS` for the same table (with `group_status`,
`dedup_version`, `member_count`, and 8 more new columns) is a silent no-op on any install
that already ran `010` — which is every fresh install — and the next statement,
`CREATE INDEX ... ON candidate_fact_dedup_groups(group_status)`, fails with
`column "group_status" does not exist`. The same problem exists one table over:
`candidate_fact_dedup_group_members` (also first created by `010`) is missing
`similarity_to_canonical`/`source_rank`, which `v_candidate_fact_dedup_review` selects.

`025a_fix_candidate_fact_dedup_schema.sql` already has the correct fix for both tables
(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every new column, plus a backfill `UPDATE`
for legacy rows) but runs after `025`, so — same story as the other two bug classes — it
never got a chance to under a strict runner. **Fixed** by adding the same
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements directly into `025`, mirroring `025a`'s
column definitions, right after each `CREATE TABLE IF NOT EXISTS` and before the first
statement that needs the new columns. Idempotent either way: a no-op if `025`'s own
`CREATE TABLE` did fire (fresh table, columns already present).

Cross-checked every `CREATE TABLE IF NOT EXISTS <name>` across all 44 files for the same
table name appearing in more than one file with a later file assuming extra columns — only
these two tables had the problem, both confined to `025`, both fixed above.

## If you add a new migration

Use the next integer after the highest number present (currently `042`). Don't reuse a
letter suffix pattern (`041a`, `041b`) unless you are patching a migration that has
*already shipped and been applied by someone* and you specifically do not want to touch
the original file's already-executed statements. Otherwise just take the next number.

## If you ever do want a clean baseline

The safe way to collapse this history without breaking anyone's already-applied database:

```bash
pg_dump --schema-only job_apply_os > db/migrations/000_baseline_$(date +%Y%m%d).sql
```

Then start numbering fresh installs from that file forward, while leaving `001`-`042`
in place for anyone who applied them individually already. Not done automatically here —
it's a one-way decision about which environment "wins" as ground truth, and that's the
project owner's call, not something to make silently inside a verification pass.
