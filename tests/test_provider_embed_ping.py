"""Embed-on-ingest ping (api#95 phase 2 wrap-up).

After each provider run completes, the runtime fires a fire-and-forget
{"op":"ping"} at the embed daemon's socket so its startup catch-up keeps
the vector index fresh with ingested items (≤1h via the heartbeat) —
without ever letting search quality depend on the ping succeeding.

The run wire-through test lives in test_providers.py (TestRuns) where
the provider-run scaffolding is; this file covers the ping contract.
"""

import json

from api.provider_runtime import _ping_embed_daemon


def test_ping_sends_op_ping_and_drains_reply(ping_server):
    _ping_embed_daemon()  # must not raise
    payload = ping_server.wait_for_ping()
    assert json.loads(payload) == {"op": "ping"}


def test_ping_missing_socket_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MEMOGARDEN_EMBED_SOCK",
        str(tmp_path / "does-not-exist" / "embed.sock"),
    )
    _ping_embed_daemon()  # swallowed by contract — the real assertion is "no raise"
