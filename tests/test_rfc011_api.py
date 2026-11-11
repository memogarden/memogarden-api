"""Tests for RFC-011 scope grants + enforcement at the API layer.

Covers:
- Grant-gated enter (SG-4): key without grant -> 403; with grant -> 200;
  JWT operator bypasses (SG-11)
- Grants management endpoints (POST/GET/DELETE /grants, JWT-only)
- Agent grants: applied to bound keys, inherited by future bindings
- Enforcement flag: query/get hide scoped items from principals outside
  the scope; unscoped items stay global (SG-1/SG-5/SG-10)
- Creation auto-labeling (§4.6)
- /auth/me reports grants + active_scope
"""

import pytest

from system.core import get_core
from utils import uid


@pytest.fixture
def scope_uuid(client, auth_headers):
    """Create a real Scope entity via the Semantic API."""
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope", "data": {"label": "Finances"}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


@pytest.fixture
def key_id(client, auth_headers, auth_headers_apikey):
    """The id of the API key behind auth_headers_apikey."""
    response = client.get("/api-keys/", headers=auth_headers)
    keys = response.get_json()
    assert len(keys) >= 1
    return keys[-1]["id"]


def _grant(client, auth_headers, key_id, scope_uuid):
    return client.post(
        "/grants",
        json={"key_id": key_id, "scope_uuid": scope_uuid},
        headers=auth_headers,
    )


def _plain(prefixed: str) -> str:
    """Grant entries store plain uuids; create responses return core_-prefixed."""
    return prefixed.split("core_", 1)[-1]


@pytest.fixture
def enforcement(monkeypatch):
    """RFC-011 §8: flip the enforcement flag for the requesting test."""
    monkeypatch.setenv("MEMOGARDEN_SCOPE_ENFORCEMENT", "1")


class TestGrantGating:
    """RFC-011 §4.4: entering requires a grant (SG-4) — under enforcement."""

    def test_enter_without_grant_denied(self, client, auth_headers_apikey, scope_uuid, enforcement):
        """A key with no grant gets 403 on enter (PermissionDenied)."""
        response = client.post(
            "/mg",
            json={"op": "enter", "scope": scope_uuid},
            headers=auth_headers_apikey,
        )

        assert response.status_code == 403
        data = response.get_json()
        assert data["ok"] is False
        assert data["error"]["type"] == "PermissionDenied"
        assert data["error"]["details"]["code"] == "scope_grant_required"

    def test_enter_with_grant_allowed(self, client, auth_headers, auth_headers_apikey, key_id, scope_uuid, enforcement):
        """With a grant, the same key enters successfully."""
        response = _grant(client, auth_headers, key_id, scope_uuid)
        assert response.status_code == 201

        response = client.post(
            "/mg",
            json={"op": "enter", "scope": scope_uuid},
            headers=auth_headers_apikey,
        )

        assert response.status_code == 200
        assert response.get_json()["result"]["active_scope"] == scope_uuid

    def test_jwt_operator_bypasses_grants(self, client, auth_headers, scope_uuid):
        """SG-11: operator JWT sessions enter without grants."""
        response = client.post(
            "/mg",
            json={"op": "enter", "scope": scope_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 200

    def test_revoke_then_enter_denied(self, client, auth_headers, auth_headers_apikey, key_id, scope_uuid, enforcement):
        """Revocation gates the next enter (grants re-read per request)."""
        assert _grant(client, auth_headers, key_id, scope_uuid).status_code == 201
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers_apikey
        ).status_code == 200

        grants = client.get(f"/grants?key_id={key_id}", headers=auth_headers).get_json()
        grant_id = grants[0]["grant_id"]

        revoke = client.delete(f"/grants/{grant_id}", headers=auth_headers)
        assert revoke.status_code == 200

        response = client.post(
            "/mg",
            json={"op": "enter", "scope": scope_uuid},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 403


class TestGrantsEndpoints:
    """RFC-011 §6: operator-gated grant management."""

    def test_grant_requires_jwt(self, client, auth_headers_apikey, scope_uuid, key_id):
        """API keys cannot mint grants — not even their own."""
        response = client.post(
            "/grants",
            json={"key_id": key_id, "scope_uuid": scope_uuid},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 401

    def test_grant_creates_entry(self, client, auth_headers, key_id, scope_uuid):
        response = _grant(client, auth_headers, key_id, scope_uuid)
        assert response.status_code == 201
        grants = response.get_json()["grants"]
        assert len(grants) == 1
        assert grants[0]["grant_id"]
        assert grants[0]["key_id"] == key_id

    def test_grant_is_idempotent_per_scope(self, client, auth_headers, key_id, scope_uuid):
        assert _grant(client, auth_headers, key_id, scope_uuid).status_code == 201
        response = _grant(client, auth_headers, key_id, scope_uuid)
        assert response.status_code == 201
        assert len(response.get_json()["grants"]) == 1  # existing entry returned

    def test_grant_validates_scope_exists(self, client, auth_headers, key_id):
        response = client.post(
            "/grants",
            json={"key_id": key_id, "scope_uuid": "core_00000000-0000-4000-8000-000000000000"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_grant_requires_exactly_one_target(self, client, auth_headers, scope_uuid):
        response = client.post(
            "/grants",
            json={"key_id": "k", "agent_id": "a", "scope_uuid": scope_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 400

        response = client.post(
            "/grants",
            json={"scope_uuid": scope_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_list_grants_for_key(self, client, auth_headers, key_id, scope_uuid):
        _grant(client, auth_headers, key_id, scope_uuid)
        grants = client.get(f"/grants?key_id={key_id}", headers=auth_headers).get_json()
        assert len(grants) == 1
        assert grants[0]["scope_uuid"] == _plain(scope_uuid)
        assert grants[0]["key_id"] == key_id

    def test_auth_me_reports_grants_and_active_scope(
        self, client, auth_headers, auth_headers_apikey, key_id, scope_uuid
    ):
        _grant(client, auth_headers, key_id, scope_uuid)
        client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers_apikey
        )

        me = client.get("/auth/me", headers=auth_headers_apikey).get_json()
        assert len(me["grants"]) == 1
        assert me["grants"][0]["scope_uuid"] == _plain(scope_uuid)
        assert me["active_scope"] == scope_uuid

    def test_auth_me_jwt_reports_sovereign_grants(self, client, auth_headers):
        me = client.get("/auth/me", headers=auth_headers).get_json()
        assert me["grants"] is None  # SG-11: implicit all-grants
        assert "active_scope" in me


class TestAgentGrants:
    """RFC-011 §6: agent grants apply to bound keys; bindings inherit."""

    @pytest.fixture
    def agent_and_key(self, client, auth_headers):
        agent_response = client.post(
            "/agents", json={"label": "test-agent"}, headers=auth_headers
        )
        assert agent_response.status_code == 201
        agent_id = agent_response.get_json()["agent_id"]

        key_response = client.post(
            "/api-keys/",
            json={"name": "agent-key", "agent_id": agent_id},
            headers=auth_headers,
        )
        assert key_response.status_code in (200, 201)
        key = key_response.get_json()
        return agent_id, key["id"]

    def test_grant_via_agent_applies_to_bound_key(
        self, client, auth_headers, scope_uuid, agent_and_key
    ):
        agent_id, key_id = agent_and_key

        response = client.post(
            "/grants",
            json={"agent_id": agent_id, "scope_uuid": scope_uuid},
            headers=auth_headers,
        )
        assert response.status_code == 201

        grants = client.get(f"/grants?agent_id={agent_id}", headers=auth_headers).get_json()
        assert len(grants) == 1
        assert grants[0]["key_id"] == key_id

    def test_future_binding_inherits_envelope(
        self, client, auth_headers, scope_uuid, agent_and_key
    ):
        agent_id, first_key_id = agent_and_key

        assert client.post(
            "/grants",
            json={"agent_id": agent_id, "scope_uuid": scope_uuid},
            headers=auth_headers,
        ).status_code == 201

        # Bind a second key to the same agent — it inherits the envelope
        second = client.post(
            "/api-keys/",
            json={"name": "agent-key-2", "agent_id": agent_id},
            headers=auth_headers,
        )
        second_key_id = second.get_json()["id"]

        grants = client.get(f"/grants?key_id={second_key_id}", headers=auth_headers).get_json()
        assert [g["scope_uuid"] for g in grants] == [_plain(scope_uuid)]


class TestEnforcement:
    """RFC-011 §4.5 predicate + §8 flag (MEMOGARDEN_SCOPE_ENFORCEMENT=1).

    The outside principal is an AGENT-BOUND key: an unbound key attributes
    to the owning username (RFC-010), which shares the operator's frame —
    bound keys are the genuinely separate principal for these tests.
    """

    @pytest.fixture
    def agent_key(self, client, auth_headers):
        """A bound agent key: headers, key id, agent id."""
        agent_response = client.post(
            "/agents", json={"label": "enforcement-agent"}, headers=auth_headers
        )
        assert agent_response.status_code == 201
        agent_id = agent_response.get_json()["agent_id"]

        key_response = client.post(
            "/api-keys/",
            json={"name": "enforcement-key", "agent_id": agent_id},
            headers=auth_headers,
        )
        key = key_response.get_json()

        headers = {"X-API-Key": key["key"], "Content-Type": "application/json"}
        return {"headers": headers, "key_id": key["id"], "agent_id": agent_id}

    def _add_note(self, client, headers, name, metadata=None):
        payload = {"op": "add", "type": "Note", "data": {"title": name, "description": name}}
        if metadata is not None:
            payload["metadata"] = metadata
        return client.post("/mg", json=payload, headers=headers)

    def test_scoped_item_hidden_from_outside_principal(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        # Operator enters the scope and creates a note (auto-labeled §4.6)
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200
        add = self._add_note(client, auth_headers, "secret note")
        assert add.status_code == 200
        note_uuid = add.get_json()["result"]["uuid"]

        # Out-of-scope agent key: query excludes it...
        query = client.post(
            "/mg",
            json={"op": "query", "target_type": "fact", "type": "Note"},
            headers=agent_key["headers"],
        )
        assert query.status_code == 200, query.get_json()
        assert all(
            r["uuid"] != note_uuid
            for r in query.get_json()["result"]["results"]
        )

        # ...and get is denied
        get = client.post(
            "/mg", json={"op": "get", "target": note_uuid}, headers=agent_key["headers"]
        )
        assert get.status_code == 403, get.get_json()

    def test_scoped_item_visible_from_inside_scope(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200
        add = self._add_note(client, auth_headers, "secret note")
        note_uuid = add.get_json()["result"]["uuid"]

        # Grant the agent; the agent key enters and can now query and get it
        assert client.post(
            "/grants",
            json={"agent_id": agent_key["agent_id"], "scope_uuid": scope_uuid},
            headers=auth_headers,
        ).status_code == 201
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=agent_key["headers"]
        ).status_code == 200

        query = client.post(
            "/mg",
            json={"op": "query", "target_type": "fact", "type": "Note"},
            headers=agent_key["headers"],
        )
        assert query.status_code == 200, query.get_json()
        assert any(
            r["uuid"] == note_uuid
            for r in query.get_json()["result"]["results"]
        )

        get = client.post(
            "/mg", json={"op": "get", "target": note_uuid}, headers=agent_key["headers"]
        )
        assert get.status_code == 200

    def test_unscoped_items_stay_global(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        # Agent key (unscoped) creates a note — no label applied
        add = self._add_note(client, agent_key["headers"], "public note")
        assert add.status_code == 200
        assert add.get_json()["result"]["metadata"].get("scopes") in (None, [])
        note_uuid = add.get_json()["result"]["uuid"]

        # A scoped note exists too
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200
        self._add_note(client, auth_headers, "secret note")

        # The unscoped note remains visible to the unscoped key
        query = client.post(
            "/mg",
            json={"op": "query", "target_type": "fact", "type": "Note"},
            headers=agent_key["headers"],
        )
        assert query.status_code == 200, query.get_json()
        assert any(
            r["uuid"] == note_uuid
            for r in query.get_json()["result"]["results"]
        )

    def test_creation_auto_labels_active_scope(
        self, client, auth_headers, scope_uuid, enforcement
    ):
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200

        add = self._add_note(client, auth_headers, "labeled note")
        metadata = add.get_json()["result"]["metadata"]
        # Labels store the plain uuid (DB convention); scope_uuid is prefixed
        assert metadata["scopes"] == [scope_uuid.split("core_", 1)[-1]]

    def test_explicit_scope_labels_require_grant(
        self, client, agent_key, scope_uuid, enforcement
    ):
        """Labeling an item INTO a scope requires a grant (§4.5 relabel rule)."""
        response = self._add_note(
            client, agent_key["headers"], "smuggled note", metadata={"scopes": [scope_uuid]}
        )
        assert response.status_code == 403


class TestScopeMinting:
    """RFC-011 v1.2 (D1): scope creation is operator-gated (unconditional —
    issuance governance, not an enforcement-flag activity)."""

    def test_key_cannot_mint_scope(self, client, auth_headers_apikey):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Smuggled"}},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_mint_denied"

    def test_operator_mints_scope_via_create(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Operator Scope"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert "uuid" in response.get_json()["result"]


class TestAdvisoryMode:
    """RFC-011 §8 step 2: flag OFF = advisory — entry and labeling are
    unchecked (grants gate only under enforcement), but labels are still
    written so the corpus is correct before the flag flips."""

    def test_advisory_enter_without_grant_allowed(self, client, auth_headers_apikey, scope_uuid):
        response = client.post(
            "/mg",
            json={"op": "enter", "scope": scope_uuid},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 200
        assert response.get_json()["result"]["active_scope"] == scope_uuid

    def test_advisory_explicit_labels_accepted_without_grant(
        self, client, auth_headers_apikey, scope_uuid
    ):
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"title": "provisioned", "description": "x"},
                "metadata": {"scopes": [scope_uuid]},
            },
            headers=auth_headers_apikey,
        )
        assert response.status_code == 200
        # labels normalize to plain uuids at the API boundary
        assert response.get_json()["result"]["metadata"]["scopes"] == [_plain(scope_uuid)]

    def test_advisory_auto_label_still_writes(self, client, auth_headers, scope_uuid):
        """Inheritance runs advisory too — no silently-global corpus."""
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200

        add = client.post(
            "/mg",
            json={"op": "add", "type": "Note",
                  "data": {"title": "labeled", "description": "x"}},
            headers=auth_headers,
        )
        assert add.get_json()["result"]["metadata"]["scopes"] == [_plain(scope_uuid)]


class TestRelabelGovernance:
    """RFC-011 v1.2 (D2): symmetric label grants on amend — the label
    surface is grant-gated in BOTH directions under enforcement."""

    @pytest.fixture
    def two_sorters(self, client, auth_headers):
        """Two bound agent keys: sorter-a holds {triage, finances} grants,
        sorter-b holds {triage} only. Both are inside triage. The conftest
        wipes entities per test, so the fixture mints its own scopes."""
        triage_response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Sort Triage"}},
            headers=auth_headers,
        )
        assert triage_response.status_code == 200
        triage = triage_response.get_json()["result"]["uuid"]

        finances_response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Finances"}},
            headers=auth_headers,
        )
        assert finances_response.status_code == 200
        finances_uuid = finances_response.get_json()["result"]["uuid"]

        keys = {}
        for name, grants in (("sorter-a", {triage, _plain(finances_uuid)}),
                             ("sorter-b", {triage})):
            agent_response = client.post(
                "/agents", json={"label": name}, headers=auth_headers
            )
            agent_id = agent_response.get_json()["agent_id"]
            key_response = client.post(
                "/api-keys/",
                json={"name": name, "agent_id": agent_id},
                headers=auth_headers,
            )
            key = key_response.get_json()
            headers = {"X-API-Key": key["key"], "Content-Type": "application/json"}
            for scope_plain in grants:
                assert client.post(
                    "/grants",
                    json={"agent_id": agent_id, "scope_uuid": scope_plain},
                    headers=auth_headers,
                ).status_code == 201
            assert client.post(
                "/mg", json={"op": "enter", "scope": triage},
                headers=headers,
            ).status_code == 200
            keys[name] = headers
        return {"triage": triage, "finances": finances_uuid, "keys": keys}

    def _labeled_item(self, client, auth_headers, triage, finances):
        """Operator creates an item labeled into both scopes (sovereign)."""
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"title": "statement", "description": "x"},
                "metadata": {"scopes": [triage, finances]},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        return response.get_json()["result"]["uuid"]

    def test_holder_can_remove_label(self, client, auth_headers, two_sorters, enforcement):
        """sorter-a (triage+finances grants, inside triage) re-files the
        item out of finances — removal requires holding finances ✓."""
        item = self._labeled_item(client, auth_headers, two_sorters["triage"],
                                  two_sorters["finances"])
        response = client.post(
            "/mg",
            json={"op": "amend", "target": item,
                  "data": {"title": "statement", "description": "x"},
                  "metadata": {"scopes": [two_sorters["triage"]]}},
            headers=two_sorters["keys"]["sorter-a"],
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["result"]["metadata"]["scopes"] == [_plain(two_sorters["triage"])]

    def test_without_target_grant_removal_denied(self, client, auth_headers, two_sorters, enforcement):
        """sorter-b (triage grant only) cannot strip the finances label —
        D2: removal requires the removed scope's grant."""
        item = self._labeled_item(client, auth_headers, two_sorters["triage"],
                                  two_sorters["finances"])
        response = client.post(
            "/mg",
            json={"op": "amend", "target": item,
                  "data": {"title": "statement", "description": "x"},
                  "metadata": {"scopes": [two_sorters["triage"]]}},
            headers=two_sorters["keys"]["sorter-b"],
        )
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["details"]["code"] == "scope_grant_required"

    def test_adding_label_via_amend_also_gated(self, client, auth_headers, two_sorters, enforcement):
        """Symmetry: adding a label through amend needs its grant too."""
        item = self._labeled_item(client, auth_headers, two_sorters["triage"],
                                  two_sorters["finances"])
        # sorter-a first strips finances so the item is {triage}-only;
        # the amend supersedes the fact, so sorter-b must target the successor
        strip = client.post(
            "/mg",
            json={"op": "amend", "target": item,
                  "data": {"title": "statement", "description": "x"},
                  "metadata": {"scopes": [two_sorters["triage"]]}},
            headers=two_sorters["keys"]["sorter-a"],
        )
        item = strip.get_json()["result"]["uuid"]
        # sorter-b (no finances grant) tries to add finances back
        response = client.post(
            "/mg",
            json={"op": "amend", "target": item,
                  "data": {"title": "statement", "description": "x"},
                  "metadata": {"scopes": [two_sorters["triage"], two_sorters["finances"]]}},
            headers=two_sorters["keys"]["sorter-b"],
        )
        assert response.status_code == 403
