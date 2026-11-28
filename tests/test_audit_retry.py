"""system#15 part 2 — audit writes retry on SQLITE_BUSY.

A lost audit write 500s a user request whose actual operation was fine
(observed live on flip day: two /mg probes 500ed because the @with_audit
denial-audit write lost the WAL write-lock race during the catch-up
burst). The helper: 3 attempts, linear backoff, re-raises on non-locked
errors and on final failure.
"""

import sqlite3

import pytest

from api.handlers.decorators import _write_audit_with_retry


class TestAuditRetry:
    def test_retries_through_a_locked_attempt(self):
        calls = {"n": 0}

        def write(soil):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")

        _write_audit_with_retry(write)
        assert calls["n"] == 2  # first attempt lost the race, second landed

    def test_retries_are_bounded_then_re_raise(self, monkeypatch):
        monkeypatch.setattr("api.handlers.decorators.time.sleep", lambda s: None)
        calls = {"n": 0}

        def write(soil):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            _write_audit_with_retry(write)
        assert calls["n"] == 3

    def test_non_locked_errors_raise_immediately(self):
        calls = {"n": 0}

        def write(soil):
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: item")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _write_audit_with_retry(write)
        assert calls["n"] == 1  # not a contention error — no retry
