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
        def __init__(self, rows=None): self.rows = rows or []
        def execute(self, *_a, **_k): return None
        def fetchall(self): return list(self.rows)
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    class Conn:
        def __init__(self): self.calls = 0
        def cursor(self):
            self.calls += 1
            return Cur([row] if self.calls == 1 else [])
        def commit(self): return None

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
