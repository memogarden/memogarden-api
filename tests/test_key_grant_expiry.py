"""api#98 + api#99 — expiry is enforceable.

An expired API key authenticates no longer (same class of response as a
revoked one) and the key list flags it; a grant issued with an expires_at
stores an ISO-8601 UTC string instead of failing the request with a 500.
"""

import json
from datetime import UTC, datetime, timedelta

from system.core import get_core
from utils import isodatetime

from api.middleware.api_keys import verify_api_key_and_get_user


def _create_key(client, auth_headers, expires_at=None):
    payload = {"name": "expiry-test"}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    response = client.post("/api-keys/", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _mg(client, headers, payload):
    return client.post("/mg", json=payload, headers=headers)


def _make_scope(client, auth_headers) -> str:
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope",
              "data": {"label": "Expiry Test", "active_participants": [],
                       "artifact_uuids": []}},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]["uuid"]


class TestApiKeyExpiry:
    """api#98: expires_at is enforced at verification time."""

    def test_expired_key_cannot_authenticate(self, client, auth_headers):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        key = _create_key(client, auth_headers, expires_at=past)

        with get_core() as core:
            assert verify_api_key_and_get_user(core._conn, key["key"]) is None

        response = _mg(client, {"X-API-Key": key["key"]},
                       {"op": "query", "target_type": "fact", "type": "Note"})
        assert response.status_code == 401

    def test_unexpired_key_still_authenticates(self, client, auth_headers):
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        key = _create_key(client, auth_headers, expires_at=future)

        with get_core() as core:
            result = verify_api_key_and_get_user(core._conn, key["key"])
        assert result is not None

        response = _mg(client, {"X-API-Key": key["key"]},
                       {"op": "query", "target_type": "fact", "type": "Note"})
        assert response.status_code == 200

    def test_key_list_flags_expired(self, client, auth_headers):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        expired = _create_key(client, auth_headers, expires_at=past)
        future = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        live = _create_key(client, auth_headers, expires_at=future)
        no_expiry = _create_key(client, auth_headers)

        response = client.get("/api-keys/", headers=auth_headers)
        assert response.status_code == 200
        by_id = {k["id"]: k for k in response.get_json()}
        assert by_id[expired["id"]]["expired"] is True
        assert by_id[live["id"]]["expired"] is False
        assert by_id[no_expiry["id"]]["expired"] is False


class TestGrantExpiry:
    """api#99: POST /grants with an expires_at stores an ISO string."""

    def test_grant_with_expiry_is_201_iso_string(self, client, auth_headers):
        key = _create_key(client, auth_headers)
        scope_uuid = _make_scope(client, auth_headers)

        response = client.post(
            "/grants",
            json={"key_id": key["id"], "scope_uuid": scope_uuid,
                  "expires_at": "2026-12-31T23:59:59Z"},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.get_json()
        grants = response.get_json()["grants"]
        assert len(grants) == 1
        assert grants[0]["expires_at"] == "2026-12-31T23:59:59Z"

        # The stored entry serializes: a second list call round-trips it.
        # (Listed entries carry the stripped scope uuid; creation accepted
        # the core_-prefixed one.)
        bare_scope = scope_uuid.split("core_", 1)[-1]
        response = client.get(f"/grants?key_id={key['id']}", headers=auth_headers)
        assert response.status_code == 200
        listed = [g for g in response.get_json()
                  if g.get("scope_uuid") == bare_scope]
        assert listed and listed[0]["expires_at"] == "2026-12-31T23:59:59Z"

    def test_grant_idempotent_regrant_unaffected(self, client, auth_headers):
        key = _create_key(client, auth_headers)
        scope_uuid = _make_scope(client, auth_headers)
        response = client.post(
            "/grants",
            json={"key_id": key["id"], "scope_uuid": scope_uuid,
                  "expires_at": "2026-12-31T23:59:59Z"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        response = client.post(
            "/grants",
            json={"key_id": key["id"], "scope_uuid": scope_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert response.get_json()["grants"][0]["expires_at"] == "2026-12-31T23:59:59Z"

    def test_grant_expiry_non_utc_offset_normalized(self, client, auth_headers):
        """Review should-fix: to_timestamp only rewrites +00:00, so an
        aware +08:00 datetime stored verbatim would misorder the
        lexicographic expiry check. Normalized to the UTC instant."""
        key = _create_key(client, auth_headers)
        scope_uuid = _make_scope(client, auth_headers)
        response = client.post(
            "/grants",
            json={"key_id": key["id"], "scope_uuid": scope_uuid,
                  "expires_at": "2027-01-01T05:00:00+08:00"},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["grants"][0]["expires_at"] == "2026-12-31T21:00:00Z"
