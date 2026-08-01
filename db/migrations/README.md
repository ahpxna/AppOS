# Migration numbering — read this before adding a new file

Apply order is plain filename sort. `for f in db/migrations/*.sql; do psql ... -f "$f"; done`
in that order, every time, including on a brand-new database.

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
  `007_seed_mock_profile_fixed.sql` and `007a_fix_conflict_indexes.sql` were left as-is; they're
  now redundant re-application of the same fix (harmless — `007_fixed` inserts one duplicate
  set of mock chunk/fact/brief rows for the test application, `007a`'s extra full-unique-index
  is just belt-and-suspenders on top of the partial index).
  `024`/`024a`, `025`/`025a`/`025b` were re-checked against this same failure mode (`ON CONFLICT`
  target vs. a *partial* unique index) — no other partial unique index exists anywhere in
  `db/migrations/*.sql` besides the one above, so this specific hazard cannot recur elsewhere
  in the current migration set.
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
