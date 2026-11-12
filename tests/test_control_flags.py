"""bot/control_flags.py -- DB-flow tests against a fake psycopg2 connection/
cursor (no real Postgres available in this sandbox; see bot/newsfeed.py's
test file for the same pattern and the reason for it).
"""
from __future__ import annotations

import pytest

from bot import control_flags


class FakeCursor:
    def __init__(self, calls, fetchone_queue):
        self.calls = calls
        self.fetchone_queue = fetchone_queue

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetchone_queue.pop(0) if self.fetchone_queue else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, fetchone_queue=None):
        self.calls: list = []
        self.fetchone_queue = fetchone_queue or []
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self.calls, self.fetchone_queue)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(control_flags, "ENABLED", True)
    monkeypatch.setattr(control_flags, "DATABASE_URL", "postgres://fake")
    monkeypatch.setattr(control_flags, "_schema_ready", False)
    yield


def test_disabled_functions_are_safe_noops(monkeypatch):
    monkeypatch.setattr(control_flags, "ENABLED", False)
    assert control_flags.get_flag("aggressive_btc_paused") is None
    control_flags.set_flag("aggressive_btc_paused", True)  # must not raise


def test_set_flag_upserts_and_commits(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(control_flags, "_get_conn", lambda: conn)

    control_flags.set_flag("aggressive_btc_paused", True)

    assert conn.committed is True
    upsert_calls = [c for c in conn.calls if "INSERT INTO" in c[0]]
    assert len(upsert_calls) == 1
    name, value, _ts = upsert_calls[0][1]
    assert name == "aggressive_btc_paused"
    assert value is True


def test_set_flag_tolerates_a_connection_failure(monkeypatch):
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(control_flags, "_get_conn", boom)

    control_flags.set_flag("aggressive_btc_paused", True)  # must not raise


def test_get_flag_returns_the_stored_value(monkeypatch):
    conn = FakeConn(fetchone_queue=[(True,)])
    monkeypatch.setattr(control_flags, "_get_conn", lambda: conn)

    assert control_flags.get_flag("aggressive_btc_paused") is True


def test_get_flag_returns_none_when_never_set(monkeypatch):
    conn = FakeConn(fetchone_queue=[None])
    monkeypatch.setattr(control_flags, "_get_conn", lambda: conn)

    assert control_flags.get_flag("aggressive_btc_paused") is None


def test_get_flag_returns_none_on_connection_failure(monkeypatch):
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(control_flags, "_get_conn", boom)

    assert control_flags.get_flag("aggressive_btc_paused") is None
