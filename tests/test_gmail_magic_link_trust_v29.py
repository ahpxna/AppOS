from datetime import datetime, timezone


def test_watcher_binds_magic_link_secret_context_into_trust_approval(monkeypatch):
    from services.auth import gmail_verification_watcher_v1 as watcher

    candidate = {
        "message_id": "m1",
        "sender": "noreply@verify.example",
        "subject": "Verify",
        "received_at": datetime.now(timezone.utc),
        "kind": "magic_link",
        "secret_sha256": "a" * 64,
        "secret_context": {
            "kind": "magic_link",
            "link_origin": "https://verify.example",
            "link_path": "/magic",
        },
    }
    row = (
        "app-1", "candidate@example.com", "https://ats.example",
        "https://ats.example/verify", "fp", {}, datetime.now(timezone.utc),
    )

    class Cur:
        def __init__(self, rows=None): self.rows = rows or []; self.sql = ""
        def execute(self, sql, *_a, **_k): self.sql = " ".join(str(sql).split())
        def fetchall(self):
            if "FROM application_auth_sessions" in self.sql:
                return list(self.rows)
            if "status='rejected'" in self.sql:
                return []
            return []
        def fetchone(self):
            if "JOIN application_auth_sessions" in self.sql and self.rows:
                r = self.rows[0]
                return ("needs_email_verification", "needs_email_verification", r[1], r[2])
            return None
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    class Conn:
        def cursor(self): return Cur([row])
        def commit(self): return None
        def rollback(self): return None

    captured = []
    monkeypatch.setattr(watcher, "discover_verification", lambda **_k: candidate)
    monkeypatch.setattr(watcher, "persist_candidate", lambda _cur, **_k: "cand-1")
    monkeypatch.setattr(watcher, "_host_is_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(watcher, "gmail_account", lambda: "candidate@example.com")
    monkeypatch.setattr(
        watcher, "create_privileged_request",
        lambda _cur, **kwargs: captured.append(kwargs) or "approval-1",
    )

    assert watcher.process_pending(Conn()) == 1
    assert len(captured) == 1
    assert captured[0]["action_type"] == "privileged_trust_external_domain"
    payload = captured[0]["payload"]
    assert payload["verification_kind"] == "magic_link"
    assert payload["secret_sha256"] == candidate["secret_sha256"]
    assert payload["secret_context"] == candidate["secret_context"]
