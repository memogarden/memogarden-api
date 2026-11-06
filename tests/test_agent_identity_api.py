"""RFC-010 agent identity: API integration tests.

Covers the invariants and flows specified in RFC-010:
- Issuance gating: /agents and PATCH /api-keys/<id> are JWT-only (INV-1)
- Binding validation: unknown/foreign agents, revoked keys, unbind
- Attribution matrix: Action facts + envelope carry (actor, actor_type)
  per auth state — agent / api_key / operator (INV-3, INV-4, INV-5)
- Introspection: /auth/me returns identity per auth state, with no
  agent-field leakage on unbound keys
"""

import pytest

from system.soil import get_soil


def _mint_agent(client, auth_headers, label="claude-code", description=None):
    """Mint an agent via the API; returns the response JSON."""
    payload = {"label": label}
    if description is not None:
        payload["description"] = description
    return client.post("/agents", json=payload, headers=auth_headers)


def _create_bound_key(client, auth_headers, agent_id, name="bound-key"):
    """Create an API key bound to an agent; returns the full response JSON
    (including the one-time full key)."""
    return client.post(
        "/api-keys/",
        json={"name": name, "agent_id": agent_id},
        headers=auth_headers,
    )


def _bound_key_headers(key_response):
    """Auth headers for a freshly created key (response object or dict)."""
    data = key_response.get_json() if hasattr(key_response, "get_json") else key_response
    return {
        "X-API-Key": data["key"],
        "Content-Type": "application/json",
    }


def _action_facts():
    with get_soil() as soil:
        return [i for i in soil.list_items() if i._type == "Action"]


def _semantic_action_facts():
    """Action facts from /mg operations only (excludes the RFC-010
    register/bind facts written by identity management endpoints)."""
    return [a for a in _action_facts() if a.data["operation"] not in ("register", "bind")]


def _post_mg(client, headers):
    return client.post(
        "/mg",
        json={
            "op": "create",
            "type": "Transaction",
            "data": {
                "date": "2026-11-06",
                "amount": 1.00,
                "currency": "USD",
                "account": "Test Account",
                "category": "Test Category",
            },
        },
        headers=headers,
    )


class TestAgentIssuance:
    """Agent identity issuance is explicit and operator-gated (RFC-010 §6)."""

    def test_create_agent_with_api_key_is_rejected(self, client, auth_headers_apikey):
        """INV-1: key holders cannot mint identity."""
        response = _mint_agent(client, auth_headers_apikey)
        assert response.status_code == 401

    def test_list_agents_with_api_key_is_rejected(self, client, auth_headers_apikey):
        response = client.get("/agents", headers=auth_headers_apikey)
        assert response.status_code == 401

    def test_create_agent_with_jwt(self, client, auth_headers):
        response = _mint_agent(client, auth_headers, description="ZCode sessions")
        assert response.status_code == 201
        data = response.get_json()
        assert data["agent_id"].startswith("core_")
        assert data["label"] == "claude-code"
        assert data["description"] == "ZCode sessions"
        assert data["owner"]

    def test_agent_label_is_unique(self, client, auth_headers):
        first = _mint_agent(client, auth_headers, label="dup")
        assert first.status_code == 201
        second = _mint_agent(client, auth_headers, label="dup")
        assert second.status_code == 409

    def test_list_agents_empty(self, client, auth_headers):
        response = client.get("/agents", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json() == []


class TestKeyBinding:
    """Explicit operator-gated key binding (RFC-010 §5.3, §6.3, §6.4)."""

    def test_create_key_bound_to_agent(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        response = _create_bound_key(client, auth_headers, agent["agent_id"])
        assert response.status_code == 201
        data = response.get_json()
        assert data["agent_id"] == agent["agent_id"]
        assert data["key"]  # full key shown once

    def test_create_key_with_unknown_agent_404(self, client, auth_headers):
        response = client.post(
            "/api-keys/",
            json={"name": "k", "agent_id": "core_does-not-exist"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_patch_binds_existing_key(self, client, auth_headers, auth_headers_apikey):
        """The migration path: bind a key that predates agent identity."""
        agent = _mint_agent(client, auth_headers).get_json()

        # Find the unbound key created by the fixture
        keys = client.get("/api-keys/", headers=auth_headers).get_json()
        key_id = keys[0]["id"]
        assert keys[0]["agent_id"] is None

        response = client.patch(
            f"/api-keys/{key_id}",
            json={"agent_id": agent["agent_id"]},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["agent_id"] == agent["agent_id"]

    def test_patch_unbind(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        key = _create_bound_key(client, auth_headers, agent["agent_id"]).get_json()

        response = client.patch(
            f"/api-keys/{key['id']}",
            json={"agent_id": None},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["agent_id"] is None

    def test_patch_without_agent_id_field_rejected(self, client, auth_headers):
        response = client.patch(
            f"/api-keys/{'x' * 8}",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_patch_with_api_key_is_rejected(self, client, auth_headers, auth_headers_apikey):
        keys = client.get("/api-keys/", headers=auth_headers).get_json()
        response = client.patch(
            f"/api-keys/{keys[0]['id']}",
            json={"agent_id": None},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 401

    def test_bind_to_foreign_agent_rejected(self, client, auth_headers, auth_headers_apikey):
        """Second user's agents are not bindable — ownership is enforced.

        The test DB has a single user in most fixtures, so mint as admin
        via a second identity is not available; instead verify a
        non-Agent entity uuid is rejected as a binding target.
        """
        agent = _mint_agent(client, auth_headers).get_json()

        # Create an unbound key, then try binding a non-existent uuid
        keys = client.get("/api-keys/", headers=auth_headers).get_json()
        response = client.patch(
            f"/api-keys/{keys[0]['id']}",
            json={"agent_id": "core_not-an-agent"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_bind_revoked_key_rejected(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        key = _create_bound_key(client, auth_headers, agent["agent_id"]).get_json()

        # Revoke it
        revoke = client.delete(f"/api-keys/{key['id']}", headers=auth_headers)
        assert revoke.status_code == 200

        # Unbind attempt on a revoked key must be refused (409)
        response = client.patch(
            f"/api-keys/{key['id']}",
            json={"agent_id": None},
            headers=auth_headers,
        )
        assert response.status_code == 409


class TestAttributionMatrix:
    """Action facts + envelope carry (actor, actor_type) per auth state."""

    def test_bound_key_attributes_to_agent(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        key = _create_bound_key(client, auth_headers, agent["agent_id"]).get_json()

        response = _post_mg(client, _bound_key_headers(key))
        assert response.status_code == 200
        assert response.get_json()["actor"] == agent["agent_id"]

        actions = _semantic_action_facts()
        assert len(actions) == 1
        assert actions[0].data["actor"] == agent["agent_id"]
        assert actions[0].data["actor_type"] == "agent"

    def test_unbound_key_attributes_to_username_with_api_key_type(
        self, client, auth_headers, auth_headers_apikey
    ):
        """INV-4: unbound keys behave exactly as before, plus the
        actor_type attribution-gap marker."""
        response = _post_mg(client, auth_headers_apikey)
        assert response.status_code == 200
        username = response.get_json()["actor"]

        actions = _semantic_action_facts()
        assert len(actions) == 1
        assert actions[0].data["actor"] == username
        assert actions[0].data["actor_type"] == "api_key"

    def test_jwt_attributes_to_username_with_operator_type(self, client, auth_headers):
        response = _post_mg(client, auth_headers)
        assert response.status_code == 200
        username = response.get_json()["actor"]

        actions = _semantic_action_facts()
        assert len(actions) == 1
        assert actions[0].data["actor"] == username
        assert actions[0].data["actor_type"] == "operator"

    def test_rebind_changes_only_future_attribution(self, client, auth_headers):
        """INV-3: past facts are immutable; new binding applies forward."""
        agent_a = _mint_agent(client, auth_headers, label="agent-a").get_json()
        agent_b = _mint_agent(client, auth_headers, label="agent-b").get_json()
        key = _create_bound_key(client, auth_headers, agent_a["agent_id"]).get_json()

        _post_mg(client, _bound_key_headers(key))

        client.patch(
            f"/api-keys/{key['id']}",
            json={"agent_id": agent_b["agent_id"]},
            headers=auth_headers,
        )
        response = _post_mg(client, _bound_key_headers(key))
        assert response.get_json()["actor"] == agent_b["agent_id"]

        actions = _semantic_action_facts()
        assert len(actions) == 2
        # list_items() ordering is not chronological; check the set
        assert {a.data["actor"] for a in actions} == {
            agent_a["agent_id"],
            agent_b["agent_id"],
        }
        assert all(a.data["actor_type"] == "agent" for a in actions)


class TestIntrospection:
    """/auth/me returns identity per auth state (RFC-010 §7.3)."""

    def test_jwt_me_includes_operator_attribution(self, client, auth_headers):
        response = client.get("/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data["actor_type"] == "operator"
        assert data["actor"] == data["username"]
        assert data["is_admin"] is not None

    def test_unbound_key_me_has_no_agent_fields(self, client, auth_headers_apikey):
        response = client.get("/auth/me", headers=auth_headers_apikey)
        assert response.status_code == 200
        data = response.get_json()
        assert data["actor_type"] == "api_key"
        assert data["actor"] == data["username"]
        assert data["key_prefix"].startswith("mg_sk_agent_")
        assert "label" not in data
        assert "agent_id" not in data

    def test_bound_key_me_returns_agent_identity(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        key = _create_bound_key(client, auth_headers, agent["agent_id"]).get_json()

        response = client.get("/auth/me", headers=_bound_key_headers(key))
        assert response.status_code == 200
        data = response.get_json()
        assert data["actor_type"] == "agent"
        assert data["actor"] == agent["agent_id"]
        assert data["label"] == "claude-code"
        assert data["username"]


class TestIdentityTypeBlocking:
    """RFC-010 INV-1: the Semantic API cannot mint or mutate identity.

    Agent/Operator were listed in BASELINE_ENTITY_TYPES historically; the
    create/edit/forget verbs must reject them so identity issuance stays
    operator-gated.
    """

    def test_semantic_create_agent_rejected(self, client, auth_headers_apikey):
        """A key holder cannot mint an Agent entity via POST /mg."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Agent",
                "data": {"label": "rogue", "owner": "usr_sneaky"},
            },
            headers=auth_headers_apikey,
        )
        assert response.status_code == 403
        assert response.get_json()["ok"] is False

    def test_semantic_create_agent_rejected_with_jwt(self, client, auth_headers):
        """Even JWT sessions cannot mint identity via the Semantic API —
        issuance is POST /agents only."""
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Agent", "data": {"label": "x"}},
            headers=auth_headers,
        )
        assert response.status_code == 403

    def test_semantic_edit_agent_rejected(self, client, auth_headers):
        """Editing an Agent's data (e.g. owner/label) via edit is blocked."""
        agent = _mint_agent(client, auth_headers).get_json()
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": agent["agent_id"],
                "set": {"label": "hijacked", "owner": "usr_elsewhere"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 403

    def test_semantic_forget_agent_rejected(self, client, auth_headers):
        """Identity entities are never deleted via forget (RFC-010 §4.3)."""
        agent = _mint_agent(client, auth_headers).get_json()
        response = client.post(
            "/mg",
            json={"op": "forget", "target": agent["agent_id"]},
            headers=auth_headers,
        )
        assert response.status_code == 403

    def test_no_identity_rows_written_by_data_requests(self, client, auth_headers_apikey):
        """INV-1 row-count test (RFC-010 §13.5): an authenticated data
        request must not create or modify identity rows."""
        from system.core import _create_connection

        conn = _create_connection()
        try:
            identity_types = ("Agent", "ApiKey", "Operator")

            def count_identity():
                placeholders = ",".join("?" for _ in identity_types)
                return conn.execute(
                    f"SELECT COUNT(*) FROM entity WHERE type IN ({placeholders})"
                    , identity_types
                ).fetchone()[0] + conn.execute(
                    "SELECT COUNT(*) FROM api_keys"
                ).fetchone()[0]

            before = count_identity()

            response = _post_mg(client, auth_headers_apikey)
            assert response.status_code == 200

            assert count_identity() == before
        finally:
            conn.close()


class TestIssuanceAuditTrail:
    """RFC-010 INV-2: mint/bind are traceable operator Action facts."""

    def test_mint_writes_operator_action_fact(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()

        actions = _action_facts()
        assert len(actions) == 1
        data = actions[0].data
        assert data["operation"] == "register"
        assert data["actor_type"] == "operator"
        assert data["params"]["resource"] == "agent"
        assert data["params"]["agent_id"] == agent["agent_id"]
        assert data["params"]["label"] == agent["label"]

    def test_bind_writes_operator_action_fact(self, client, auth_headers, auth_headers_apikey):
        agent = _mint_agent(client, auth_headers).get_json()
        keys = client.get("/api-keys/", headers=auth_headers).get_json()
        key_id = keys[0]["id"]

        client.patch(
            f"/api-keys/{key_id}",
            json={"agent_id": agent["agent_id"]},
            headers=auth_headers,
        )

        bind_actions = [a for a in _action_facts() if a.data["operation"] == "bind"]
        assert len(bind_actions) == 1
        data = bind_actions[0].data
        assert data["actor_type"] == "operator"
        assert data["params"] == {"api_key_id": key_id, "agent_id": agent["agent_id"]}


class TestTraceabilityQueries:
    """RFC-010 §7.2: fact-data equality filters make the PRD trace query
    executable (query verb, target_type fact)."""

    def test_query_actions_by_actor(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        key = _create_bound_key(client, auth_headers, agent["agent_id"]).get_json()
        _post_mg(client, _bound_key_headers(key))
        _post_mg(client, auth_headers)  # an operator action too

        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "type": "Action",
                "filters": {"actor": agent["agent_id"]},
                "count": 50,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        results = response.get_json()["result"]["results"]
        assert results
        assert all(r["data"]["actor"] == agent["agent_id"] for r in results)
        assert all(r["data"]["actor_type"] == "agent" for r in results)

    def test_query_attribution_gaps_by_actor_type(self, client, auth_headers, auth_headers_apikey):
        _post_mg(client, auth_headers_apikey)  # unbound key → api_key gap
        _post_mg(client, auth_headers)  # operator

        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "type": "Action",
                "filters": {"actor_type": "api_key"},
                "count": 50,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        results = response.get_json()["result"]["results"]
        assert len(results) == 1
        assert results[0]["data"]["actor_type"] == "api_key"

    def test_query_rejects_malformed_filter_field(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "type": "Action",
                "filters": {"actor') OR 1=1 --": "x"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 400


class TestAgentListingAndOwnership:
    """GET /agents binding detail + ownership enforcement (RFC-010 §6.2)."""

    def test_list_agents_shows_bound_keys(self, client, auth_headers):
        agent = _mint_agent(client, auth_headers).get_json()
        _create_bound_key(client, auth_headers, agent["agent_id"], name="k1")

        response = client.get("/agents", headers=auth_headers)
        assert response.status_code == 200
        listed = response.get_json()
        assert len(listed) == 1
        assert listed[0]["agent_id"] == agent["agent_id"]
        assert listed[0]["bound_key_count"] == 1
        assert len(listed[0]["key_prefixes"]) == 1
        assert listed[0]["key_prefixes"][0].startswith("mg_sk_agent_")

    def test_binding_to_foreign_agent_rejected_403(self, client, auth_headers):
        """An agent owned by another user cannot be bound (403)."""
        import json
        import uuid as uuid_mod

        from system.core import _create_connection
        from utils import hash_chain, isodatetime, uid

        # Mint a foreign-owned Agent entity directly (only one user exists
        # in the test DB, so the API cannot produce this state)
        conn = _create_connection()
        foreign_user = str(uuid_mod.uuid4())
        agent_uuid = uid.add_core_prefix(str(uuid_mod.uuid4()))
        now = isodatetime.now()
        try:
            conn.execute(
                """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
                VALUES (?, 'Agent', ?, 1, ?, ?, ?)""",
                (
                    agent_uuid,
                    hash_chain.compute_entity_hash(
                        entity_type="Agent", created_at=now, updated_at=now, previous_hash=None
                    ),
                    now,
                    now,
                    json.dumps({"label": "foreign", "owner": foreign_user}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Create a key to bind (fresh DB per test; fixture keys don't exist here)
        created = client.post(
            "/api-keys/",
            json={"name": "bind-me"},
            headers=auth_headers,
        )
        assert created.status_code == 201

        response = client.patch(
            f"/api-keys/{created.get_json()['id']}",
            json={"agent_id": agent_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 403
