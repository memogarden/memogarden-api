"""E3 restricted key classes: capability-gated API keys.

A key minted with capabilities=["provider_run"] may ONLY request
provider runs; every other endpoint 403s (capability_denied). Keys
minted without capabilities are unrestricted — byte-for-byte legacy
behavior. Issuance and capability changes stay operator-gated JWT
(RFC-010 invariant).
"""

import pytest


def _mint_restricted_key(client, auth_headers, capabilities=("provider_run",),
                         name="sessions-daemon"):
    payload = {"name": name}
    if capabilities is not None:
        payload["capabilities"] = list(capabilities)
    response = client.post("/api-keys/", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["capabilities"] == list(capabilities or [])
    return {"X-API-Key": data["key"], "Content-Type": "application/json"}


class TestMinting:
    def test_create_restricted_key_returns_capabilities(self, client, auth_headers):
        response = client.post(
            "/api-keys/",
            json={"name": "restricted", "capabilities": ["provider_run"]},
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert response.get_json()["capabilities"] == ["provider_run"]

    def test_create_unrestricted_key_has_empty_capabilities(self, client, auth_headers):
        response = client.post("/api-keys/", json={"name": "plain"}, headers=auth_headers)
        assert response.status_code == 201
        assert response.get_json()["capabilities"] == []

    def test_unknown_capability_rejected(self, client, auth_headers):
        response = client.post(
            "/api-keys/",
            json={"name": "bad", "capabilities": ["fly_to_the_moon"]},
            headers=auth_headers,
        )
        assert response.status_code == 400  # validation layer maps pydantic errors

    def test_key_holder_cannot_mint_keys_even_restricted(self, client, auth_headers):
        """JWT-only issuance: even the capability holder can't mint."""
        restricted = _mint_restricted_key(client, auth_headers)
        response = client.post(
            "/api-keys/", json={"name": "escalate"}, headers=restricted)
        assert response.status_code == 401

    def test_patch_capabilities(self, client, auth_headers):
        created = client.post("/api-keys/", json={"name": "k"}, headers=auth_headers)
        key_id = created.get_json()["id"]

        response = client.patch(
            f"/api-keys/{key_id}",
            json={"capabilities": ["provider_run"]},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["capabilities"] == ["provider_run"]

        # back to unrestricted
        response = client.patch(
            f"/api-keys/{key_id}", json={"capabilities": []}, headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["capabilities"] == []


class TestEnforcement:
    def test_restricted_key_can_request_provider_runs(self, client, auth_headers):
        restricted = _mint_restricted_key(client, auth_headers)
        response = client.post("/api/v1/providers/run", headers=restricted)
        # 202 = the capability gate PASSED (the view itself just finds no
        # due work)
        assert response.status_code == 202, response.get_json()

    def test_restricted_key_cannot_read_providers(self, client, auth_headers):
        restricted = _mint_restricted_key(client, auth_headers)
        response = client.get("/api/v1/providers", headers=restricted)
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "capability_denied"

    def test_restricted_key_cannot_read_semantic_api(self, client, auth_headers):
        restricted = _mint_restricted_key(client, auth_headers)
        response = client.post(
            "/mg",
            json={"op": "query", "type": "Transaction", "limit": 1},
            headers=restricted,
        )
        assert response.status_code == 403

    def test_restricted_key_cannot_hit_named_provider_run_less_surface(
        self, client, auth_headers
    ):
        """Same capability, different verb: reads under /providers are
        denied even when the run trigger is allowed."""
        restricted = _mint_restricted_key(client, auth_headers)
        assert client.get(
            "/api/v1/providers/sessions", headers=restricted).status_code == 403

    def test_unrestricted_key_unchanged(self, client, auth_headers_apikey):
        """Legacy keys (capabilities=[]) keep full access."""
        response = client.get("/api/v1/providers", headers=auth_headers_apikey)
        assert response.status_code == 200

    def test_jwt_is_sovereign(self, client, auth_headers):
        """The gate keys off api-key capabilities only; JWT is untouched."""
        response = client.get("/api/v1/providers", headers=auth_headers)
        assert response.status_code == 200
