from __future__ import annotations

import pytest

from scripts import apply_migrations


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, _sql, *_args):
        return _Result(self.row)


def test_adoption_before_050_needs_no_050_contract():
    apply_migrations.verify_adoption_contract(_Conn(None), 49)


def test_adoption_through_050_requires_actual_050_schema():
    with pytest.raises(RuntimeError, match="Refusing to adopt migration history through 050"):
        apply_migrations.verify_adoption_contract(_Conn((False,) * 7), 50)


def test_adoption_through_050_accepts_complete_contract():
    apply_migrations.verify_adoption_contract(_Conn((True,) * 7), 50)


def test_recorded_adopted_050_is_revalidated(monkeypatch):
    seen = []
    monkeypatch.setattr(apply_migrations, "verify_adoption_contract", lambda _conn, through: seen.append(through))
    apply_migrations.verify_recorded_adoption(
        object(),
        [("050_immigration_and_browser_integrity.sql", "sha", "ledger-v1-adopted")],
    )
    assert seen == [50]
