"""Regression guards for the daily-use control surface and cost ownership."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_sensitive_runtime_question_has_one_tap_jobos_handoff_without_secret_echo():
    from services.telegram import telegram_review_bot_v1 as tg

    class Cur:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            return ("source",)

    payload = {
        "question": "Will you now or in the future require sponsorship?",
        # Defend against a future accidental payload addition: cards must not
        # echo sensitive profile values into a chat destination.
        "candidate_confirmed_value": "Yes",
    }
    card = tg._message_text(("id", "sensitive_question_required", "urgent", "title", "summary",
                             "Example", "Role", payload))
    keyboard = tg._keyboard(Cur(), "id", 1, "sensitive_question_required", payload)
    assert "sponsorship" in card
    assert "candidate" not in card.casefold()
    assert "Yes" not in card
    assert "Focus JobOS form" in keyboard
    assert "recheck form" in keyboard
    assert 'tok("focus_browser"), tok("sensitive_confirm")' in _source("services/telegram/telegram_review_bot_v1.py")


def test_action_required_cannot_be_permanently_dismissed_while_state_needs_handoff():
    from services.telegram import telegram_review_bot_v1 as tg

    class Cur:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            return ("source",)

    normal = tg._keyboard(Cur(), "id", 1, "action_required", {"action_kind": "workflow_followup_required"})
    email = tg._keyboard(Cur(), "id", 1, "action_required", {"action_kind": "email_verification_candidate_ambiguity"})
    assert "Reject email" not in normal
    assert "Reject email" in email
    source = _source("services/review/review_service_v1.py")
    block = source[source.index("def ensure_action_required_review"):source.index("def sync_action_required")]
    assert 'if action_kind.startswith("email_verification_")' in block


def test_pending_natural_reply_is_force_reply_bound_ttl_and_cleared_on_other_resolution():
    source = _source("services/telegram/telegram_review_bot_v1.py")
    assert '"force_reply": True' in source
    assert "pending_question_expires_at > now()" in source
    assert "prompt_message_id" in source
    assert "reply_to_message" in source
    assert "_clear_pending_question(cur, chat_id, item_id=item_id)" in source


def test_snooze_invalidates_live_tokens_and_safe_batch_requires_unsnoozed_items():
    review = _source("services/review/review_service_v1.py")
    telegram = _source("services/telegram/telegram_review_bot_v1.py")
    snooze = review[review.index("def snooze_review_item"):review.index("def question_quick_choices")]
    assert "UPDATE telegram_callback_tokens SET used_at=now()" in snooze
    assert "pending_question_expires_at=NULL" in snooze
    assert "snoozed_until IS NULL OR snoozed_until <= now()" in telegram


def test_auth_focus_and_watcher_are_independent_of_telegram_long_poll():
    telegram = _source("services/telegram/telegram_review_bot_v1.py")
    supervisor = _source("scripts/jobos_runtime_supervisor.py")
    watcher = _source("services/auth/browser_state_watcher_v1.py")
    assert '"focus_browser"' in telegram
    assert 'default=5' in telegram
    assert '"browser-state-watcher"' in supervisor
    assert '"--poll-seconds", "5"' in supervisor
    assert "def main() -> int" in watcher


def test_paid_call_budget_ownership_is_immutable_across_midnight():
    migration = _source("db/migrations/078_daily_ux_liveness_and_budget_date.sql")
    accounting = _source("services/common/llm_cost_accounting_v1.py")
    assert "ADD COLUMN IF NOT EXISTS budget_date date" in migration
    assert "reserved_cost_usd,status,budget_date" in accounting
    assert "WHERE date=%s FOR UPDATE" in accounting
    assert "RETURNING budget_date" in accounting
    assert "current - reserved + actual" in accounting


def test_status_rejects_stale_runtime_file_and_infra_flags_are_independent():
    jobos = _source("scripts/jobos.py")
    supervisor = _source("scripts/jobos_runtime_supervisor.py")
    assert 'runtime["running"] = pid_alive(pid)' in jobos
    start = supervisor[supervisor.index("def _start_infra"):supervisor.index("def start")]
    assert 'if not _truthy("JOBOS_RUNTIME_START_POSTGRES", True):\n        return' not in start
    assert 'JOBOS_RUNTIME_START_POSTGRES' in start
    assert 'JOBOS_RUNTIME_START_OPENCLAW' in start
