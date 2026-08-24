# JobOS Human Review Hub + LinkedIn Saved Jobs + Telegram Review

Implementation date: 2026-08-24

## Product boundary

This batch adds three capabilities without weakening the exact approval model:

1. LinkedIn Saved Jobs is a read-only alternate intake source.
2. Human Review Hub materializes human-required decisions into one inbox.
3. Telegram is a remote UI adapter for that inbox, including resume/cover-letter PDFs and post-autofill screenshots.

Telegram approval of an autofill screenshot means only “the populated form looks correct”. Final application submission remains human-only in the real browser.

## File plan and implementation

| File | Action | Implementation |
|---|---|---|
| `db/migrations/059_linkedin_saved_jobs.sql` | Create | `linkedin_saved_syncs`; Saved Jobs provenance on `applications`; bounded 1–20 batch. |
| `db/migrations/060_human_review_hub.sql` | Create | `review_bundles`, `human_review_items`, `human_review_artifacts`, unified inbox view, `application_ready` state. |
| `db/migrations/061_telegram_review_channel.sql` | Create | Delivery ledger, hashed single-use callback tokens, durable long-poll offset. |
| `services/discovery/linkedin_discovery_v1.py` | Modify | Shared strict LinkedIn JD normalization; `validate_saved_request`; `ingest_saved_jobs`. |
| `services/discovery/linkedin_intake_v1.py` | Modify | `queue-saved`; one sync record + one browser task. |
| `services/browser-controller/browser_queue_worker.py` | Modify | Remove FakeMouse; Saved Jobs handler; durable post-autofill screenshot; review-item creation; `form_filled` only after completed deterministic execution. |
| `services/autofill/autofill_executor_v1.py` | Modify | Screenshot primitive scoped to the already pinned target; no agent prompt. |
| `services/approval/approval_service_v1.py` | Modify | Trusted review-UI decision entrypoint that reruns canonical binding checks; restores `--expected-page-fingerprint`. |
| `services/review/review_service_v1.py` | Create | Canonical materialized review inbox for docs, questions, capability approvals, autofill screenshots, reconciliation, final human submit. |
| `services/review/render_review_artifacts_v1.py` | Create | Deterministic physical PDF artifacts; resume reuses fixed template renderer; cover letter renders truth-checked content. |
| `services/telegram/telegram_review_bot_v1.py` | Create | Long polling; sends PDF/screenshots; allowlisted user; opaque hashed callbacks; `/answer`; `discover-id`. |
| `scripts/jobos.py` | Modify | `saved sync`, `review ...`, `telegram discover-id/start`; doctor through migration 062. |
| `.env.example` | Modify | Saved Jobs, Review Hub, Telegram configuration. |
| `scripts/bootstrap_ubuntu_24.sh` | Modify | Installs LibreOffice Writer for PDF export. |
| `tests/test_review_saved_features.py` | Create | Pure boundary tests. |
| `tests/integration/test_human_review_hub.py` | Create | DB regression for stale-content rejection and exact document approval. |

## Human intervention types

- `document_review`: approve exact QA-passed document, or request revision.
- `approval_request`: wraps canonical `approval_requests`; Review Hub never becomes a second capability engine.
- `autofill_review`: screenshot + verified/failed/paused summary; no submit authority.
- `question_required`: explicit human answer; defaults to company-scoped memory; legal/immigration answers are refused here.
- `reconciliation_required`: uncertain browser side effect; cannot be remotely cleared while the task still needs reconciliation.
- `application_ready`: final human-submit reminder; no Telegram approval button.

## Stale-decision prevention

Document review stores SHA-256 of exact generated content and rehashes on decision. Autofill review binds SHA-256 of the exact final task result. Capability approval delegates to the existing exact document/artifact/origin/page-fingerprint/input-hash/action-scope checks. Telegram stores only SHA-256 of a random callback token plus allowed user/action/expiry.

## Runtime flows

```text
LinkedIn Saved Jobs
  -> discover_linkedin_saved_jobs
  -> read-only linkedin_discovery agent
  -> canonical /jobs/view/<id>/ + 200+ char JD validation
  -> applications(discovery_channel=saved)
  -> normal screening / fit / document pipeline
```

```text
truth checker -> QA pass -> Review Hub -> PDF artifact -> Telegram
-> human approve -> generated_documents.approved=true
```

```text
exact autofill approval -> deterministic AutofillSession -> per-field verification journal
-> screenshot same pinned tab -> Review Hub -> Telegram screenshot review
-> application_ready -> HUMAN clicks final Submit in browser
```

## Environment

```env
JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED=true
JOBOS_REVIEW_AUTO_RENDER_PDFS=true
JOBOS_REVIEW_ARTIFACT_DIR=./data/review-artifacts
JOBOS_TELEGRAM_BOT_TOKEN=<BotFather token>
JOBOS_TELEGRAM_ALLOWED_USER_ID=<numeric user id>
JOBOS_TELEGRAM_CHAT_ID=<private chat id, usually same as user id>
```

## Commands

```bash
python scripts/apply_migrations.py
python scripts/jobos.py saved sync --limit 10
python services/browser-controller/browser_queue_worker.py --once

python scripts/jobos.py review inbox
python scripts/jobos.py review show <review-id>
python scripts/jobos.py review approve <review-id>
python scripts/jobos.py review revise <review-id> --note "..."
python scripts/jobos.py review reject <review-id>
python scripts/jobos.py review answer <review-id> --text "..." --scope company

# Telegram bootstrap: set BOT_TOKEN, message /start, then discover ids.
python scripts/jobos.py telegram discover-id
# Set ALLOWED_USER_ID + CHAT_ID, then:
python scripts/jobos.py telegram start --dispatch-only
python scripts/jobos.py telegram start
```

## Telegram privacy boundary

Telegram review is opt-in and not local-only. Enabling it uploads selected resume/cover-letter PDFs and autofill screenshots to Telegram. Use one private allowlisted account/chat; rotate a leaked bot token immediately; disable Telegram if these artifacts must remain on the Ubuntu host.

## Deliberate non-features

- No final application submit from Telegram.
- No LinkedIn save/unsave mutation.
- No CAPTCHA solving/checkpoint bypass in Saved Jobs.
- No Telegram direct SQL mutation of approval capabilities.
- No generic memory path for immigration/legal answers.
- No automatic replay after uncertain browser writes.
