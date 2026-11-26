"""Tests for RFC-011 item 5 — §4.9 read-path completion.

Covers:
- Entity reads enforce §4.5 (get 403, query filtered) — §4.9(2)
- Entity writes enforce (edit frame gate + D2 symmetric label grants,
  forget gate) — §4.9(2) with the amend precedent
- Relation traversal: anchor gate + unresolved-reference node stubs,
  truthful count, edges unfiltered — §4.9(4)
- Search PRE-MATCH: scoped items never become matches (no displacement),
  convenience `scope` refinement — §4.9(3) / RFC-005 v8
- Visibility short-circuit (visibility addendum): public-visibility
  labels relax READ gates from any frame; writes stay frame-gated

The bound agent key (agent_key fixture) has its own ContextFrame — a
true out-of-frame principal, unlike unbound keys which share the
operator's username frame.
"""

import pytest


@pytest.fixture
def enforcement(monkeypatch):
    """RFC-011 §8: flip the enforcement flag for the requesting test."""
    monkeypatch.setenv("MEMOGARDEN_SCOPE_ENFORCEMENT", "1")


@pytest.fixture
def scope_uuid(client, auth_headers):
    """A regular (private-visibility) Scope via the Semantic API."""
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope", "data": {"label": "Finances"}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


@pytest.fixture
def public_scope_uuid(client, auth_headers):
    """A public-visibility Scope (visibility addendum) — operator-governed."""
    response = client.post(
        "/mg",
        json={
            "op": "create",
            "type": "Scope",
            "data": {"label": "Household", "visibility": "public"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


@pytest.fixture
def agent_key(client, auth_headers):
    """A bound agent key: headers, key id, agent id (own frame)."""
    agent_response = client.post(
        "/agents", json={"label": "item5-agent"}, headers=auth_headers
    )
    assert agent_response.status_code == 201
    agent_id = agent_response.get_json()["agent_id"]

    key_response = client.post(
        "/api-keys/",
        json={"name": "item5-key", "agent_id": agent_id},
        headers=auth_headers,
    )
    key = key_response.get_json()
    return {
        "headers": {"X-API-Key": key["key"], "Content-Type": "application/json"},
        "key_id": key["id"],
        "agent_id": agent_id,
    }


def _plain(prefixed: str) -> str:
    """Entity data.scopes store plain uuids; create responses are prefixed."""
    return prefixed.split("core_", 1)[-1]


def _grant_agent(client, auth_headers, agent_key, scope_uuid):
    return client.post(
        "/grants",
        json={"agent_id": agent_key["agent_id"], "scope_uuid": scope_uuid},
        headers=auth_headers,
    )


def _enter(client, headers, scope_uuid):
    return client.post("/mg", json={"op": "enter", "scope": scope_uuid}, headers=headers)


def _scoped_entity(client, auth_headers, scope_uuid, title="scoped task"):
    """Operator creates an entity labeled into the scope."""
    response = client.post(
        "/mg",
        json={
            "op": "create",
            "type": "Task",
            "data": {"title": title, "scopes": [_plain(scope_uuid)]},
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]["uuid"]


def _unscoped_entity(client, auth_headers, title="open task"):
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Task", "data": {"title": title}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


class TestEntityReadGate:
    """§4.9(2): entity get enforces §4.5 like fact get."""

    def test_get_denied_outside_frame(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        response = client.post(
            "/mg", json={"op": "get", "target": entity_uuid},
            headers=agent_key["headers"],
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_read_denied"

    def test_get_allowed_inside_frame(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200
        response = client.post(
            "/mg", json={"op": "get", "target": entity_uuid},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200

    def test_get_public_label_relaxes_read(
        self, client, auth_headers, agent_key, public_scope_uuid, enforcement
    ):
        """Visibility addendum: a public-visibility label is readable from
        ANY frame — no grant, no entry."""
        entity_uuid = _scoped_entity(client, auth_headers, public_scope_uuid)
        response = client.post(
            "/mg", json={"op": "get", "target": entity_uuid},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200

    def test_jwt_sovereign_without_frame(
        self, client, auth_headers, scope_uuid, enforcement
    ):
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        response = client.post(
            "/mg", json={"op": "get", "target": entity_uuid}, headers=auth_headers
        )
        assert response.status_code == 200


class TestEntityQueryGate:
    """§4.9(2): entity query results are visibility-filtered."""

    def test_query_hides_out_of_frame_entities(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        scoped = _scoped_entity(client, auth_headers, scope_uuid)
        open_entity = _unscoped_entity(client, auth_headers)

        response = client.post(
            "/mg",
            json={"op": "query", "target_type": "entity", "type": "Task"},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200
        body = response.get_json()["result"]
        uuids = {r["uuid"] for r in body["results"]}
        assert scoped not in uuids
        assert open_entity in uuids
        assert body["count"] == len(body["results"])

    def test_query_shows_public_labeled_entity(
        self, client, auth_headers, agent_key, public_scope_uuid, enforcement
    ):
        scoped = _scoped_entity(client, auth_headers, public_scope_uuid)
        response = client.post(
            "/mg",
            json={"op": "query", "target_type": "entity", "type": "Task"},
            headers=agent_key["headers"],
        )
        uuids = {r["uuid"] for r in response.get_json()["result"]["results"]}
        assert scoped in uuids


class TestEntityWriteGate:
    """§4.9(2): entity edit/forget enforce like fact writes (amend
    precedent — strict frame gate, no public short-circuit on writes)."""

    def test_edit_denied_outside_frame(
        self, client, auth_headers, agent_key, public_scope_uuid, enforcement
    ):
        """Even a PUBLIC-labeled entity is not writable from outside the
        frame — visibility relaxes reads only (ratified 2026-11-19)."""
        entity_uuid = _scoped_entity(client, auth_headers, public_scope_uuid)
        response = client.post(
            "/mg",
            json={"op": "edit", "target": entity_uuid, "set": {"title": "hijack"}},
            headers=agent_key["headers"],
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_write_denied"

    def test_edit_allowed_inside_frame(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200
        response = client.post(
            "/mg",
            json={"op": "edit", "target": entity_uuid, "set": {"title": "updated"}},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200

    def test_edit_label_add_requires_d2_grant(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        other_scope = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Work"}},
            headers=auth_headers,
        ).get_json()["result"]["uuid"]
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200

        # Co-labeling into Work is the disclosure act — grant-gated (D2).
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"scopes": [_plain(scope_uuid), _plain(other_scope)]},
            },
            headers=agent_key["headers"],
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_grant_required"

        # With the grant, the publish succeeds.
        assert _grant_agent(client, auth_headers, agent_key, other_scope).status_code == 201
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"scopes": [_plain(scope_uuid), _plain(other_scope)]},
            },
            headers=agent_key["headers"],
        )
        assert response.status_code == 200

    def test_edit_label_removal_requires_d2_grant(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        other_scope = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Work"}},
            headers=auth_headers,
        ).get_json()["result"]["uuid"]
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        # Operator co-labels [Finances, Work]; agent holds only Finances.
        assert client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"scopes": [_plain(scope_uuid), _plain(other_scope)]},
            },
            headers=auth_headers,
        ).status_code == 200
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200

        # De-scoping Work (exposure-widening) requires the Work grant.
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"scopes": [_plain(scope_uuid)]},
            },
            headers=agent_key["headers"],
        )
        assert response.status_code == 403

    def test_forget_denied_outside_frame(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        entity_uuid = _scoped_entity(client, auth_headers, scope_uuid)
        response = client.post(
            "/mg", json={"op": "forget", "target": entity_uuid},
            headers=agent_key["headers"],
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_write_denied"


class TestExploreFiltering:
    """§4.9(4): anchor gated; inaccessible nodes presented as unresolved
    references (not omitted); count truthful; edges unfiltered."""

    def _link(self, client, auth_headers, source, target):
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": source,
                "source_type": "entity",
                "target": target,
                "target_type": "entity",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()

    def test_inaccessible_node_is_unresolved_reference(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        open_entity = _unscoped_entity(client, auth_headers, "hub")
        scoped = _scoped_entity(client, auth_headers, scope_uuid, "secret")
        self._link(client, auth_headers, open_entity, scoped)

        response = client.post(
            "/mg",
            json={"op": "explore", "anchor": open_entity, "radius": 1},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200
        body = response.get_json()["result"]

        by_uuid = {n["uuid"]: n for n in body["nodes"]}
        assert body["count"] == len(body["nodes"]) == 2  # truthful count
        assert by_uuid[open_entity].get("type") == "Task"  # readable node
        secret_node = by_uuid[scoped]
        assert secret_node.get("unresolved") is True
        assert "type" not in secret_node  # no content disclosure
        # Edge structure stays listed (containerizing-not-hiding)
        assert len(body["edges"]) == 1

    def test_anchor_denied_outside_frame(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        scoped = _scoped_entity(client, auth_headers, scope_uuid)
        response = client.post(
            "/mg",
            json={"op": "explore", "anchor": scoped, "radius": 1},
            headers=agent_key["headers"],
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_read_denied"

    def test_inside_frame_resolves_normally(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        open_entity = _unscoped_entity(client, auth_headers, "hub")
        scoped = _scoped_entity(client, auth_headers, scope_uuid, "secret")
        self._link(client, auth_headers, open_entity, scoped)
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200

        response = client.post(
            "/mg",
            json={"op": "explore", "anchor": open_entity, "radius": 1},
            headers=agent_key["headers"],
        )
        by_uuid = {
            n["uuid"]: n for n in response.get_json()["result"]["nodes"]
        }
        assert by_uuid[scoped].get("type") == "Task"
        assert by_uuid[scoped].get("unresolved") is None

    def test_flag_off_resolves_all_nodes(
        self, client, auth_headers, agent_key, scope_uuid
    ):
        """Advisory mode: explore is byte-identical to pre-item-5 — every
        node resolves fully, labeled or not (regression pin for the P0)."""
        open_entity = _unscoped_entity(client, auth_headers, "hub")
        scoped = _scoped_entity(client, auth_headers, scope_uuid, "secret")
        self._link(client, auth_headers, open_entity, scoped)

        response = client.post(
            "/mg",
            json={"op": "explore", "anchor": open_entity, "radius": 1},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200
        by_uuid = {
            n["uuid"]: n for n in response.get_json()["result"]["nodes"]
        }
        assert by_uuid[scoped].get("type") == "Task"
        assert by_uuid[scoped].get("unresolved") is None
        assert by_uuid[open_entity].get("type") == "Task"

    def test_operator_jwt_resolves_all_nodes(
        self, client, auth_headers, scope_uuid, enforcement
    ):
        """SG-11: the sovereign operator sees every node resolved, even
        with no frame entered."""
        open_entity = _unscoped_entity(client, auth_headers, "hub")
        scoped = _scoped_entity(client, auth_headers, scope_uuid, "secret")
        self._link(client, auth_headers, open_entity, scoped)

        response = client.post(
            "/mg",
            json={"op": "explore", "anchor": open_entity, "radius": 1},
            headers=auth_headers,
        )
        by_uuid = {
            n["uuid"]: n for n in response.get_json()["result"]["nodes"]
        }
        assert by_uuid[scoped].get("type") == "Task"
        assert by_uuid[scoped].get("unresolved") is None


class TestFactReadGate:
    """§4.9(1): the fact get gate honors the visibility short-circuit."""

    def test_fact_get_public_label_relaxes_read(
        self, client, auth_headers, agent_key, public_scope_uuid, enforcement
    ):
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"title": "public fact"},
                "metadata": {"scopes": [_plain(public_scope_uuid)]},
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        fact_uuid = response.get_json()["result"]["uuid"]

        response = client.post(
            "/mg", json={"op": "get", "target": fact_uuid},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200


class TestSearchPreMatch:
    """§4.9(3): scoped items excluded BEFORE matching — a scoped hit can
    never displace a real one; convenience `scope` narrows independently.

    Each test searches a unique token: audit-trail Action facts embed
    request text in their data, so a common query word would match them
    too and pollute the expected result sets.
    """

    def _tok(self) -> str:
        import uuid as uuid_mod

        return f"ledgertok-{uuid_mod.uuid4().hex[:10]}"

    def _add_note(self, client, headers, title, token, scopes=None):
        payload = {
            "op": "add",
            "type": "Note",
            "data": {"title": title, "description": token},
        }
        if scopes is not None:
            payload["metadata"] = {"scopes": scopes}
        response = client.post("/mg", json=payload, headers=headers)
        assert response.status_code == 200, response.get_json()
        return response.get_json()["result"]["uuid"]

    def test_scoped_hit_never_displaces_real_match(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        # Newer scoped fact would win the realized_at DESC ordering —
        # under the old post-LIMIT filter the visible page came back short.
        token = self._tok()
        scoped_uuid = self._add_note(
            client, auth_headers, "scoped newest", token, [_plain(scope_uuid)]
        )
        open_uuid = self._add_note(client, auth_headers, "open oldest", token)

        response = client.post(
            "/mg",
            json={"op": "search", "query": token, "target_type": "fact"},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200
        # Audit Action facts also embed the token; the assertion is about
        # Notes: pre-match means the scoped one never became a match.
        notes = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert [r["uuid"] for r in notes] == [open_uuid]
        assert scoped_uuid not in {r["uuid"] for r in notes}

    def test_inside_frame_sees_scoped_hit_first(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        token = self._tok()
        self._add_note(client, auth_headers, "scoped newest", token, [_plain(scope_uuid)])
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200

        response = client.post(
            "/mg",
            json={"op": "search", "query": token, "target_type": "fact"},
            headers=agent_key["headers"],
        )
        notes = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert [r["data"]["title"] for r in notes] == ["scoped newest"]

    def test_convenience_scope_param_narrows(
        self, client, auth_headers, scope_uuid, public_scope_uuid, enforcement
    ):
        token = self._tok()
        self._add_note(client, auth_headers, "in scope", token, [_plain(scope_uuid)])
        self._add_note(client, auth_headers, "elsewhere", token)

        response = client.post(
            "/mg",
            json={
                "op": "search",
                "query": token,
                "target_type": "fact",
                "scope": scope_uuid,
            },
            headers=auth_headers,
        )
        results = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert {r["data"]["title"] for r in results} == {"in scope"}

    def test_public_labeled_item_reachable_by_search(
        self, client, auth_headers, agent_key, public_scope_uuid, enforcement
    ):
        token = self._tok()
        self._add_note(
            client, auth_headers, "public note", token, [_plain(public_scope_uuid)]
        )
        response = client.post(
            "/mg",
            json={"op": "search", "query": token, "target_type": "fact"},
            headers=agent_key["headers"],
        )
        results = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert {r["data"]["title"] for r in results} == {"public note"}

    def test_unlabeled_corpus_visible_from_nowhere(
        self, client, auth_headers, agent_key, enforcement
    ):
        """Empty allowed set (no frame, no public scopes): only unlabeled
        items surface — the SG-1 floor."""
        token = self._tok()
        self._add_note(client, auth_headers, "open note", token)
        response = client.post(
            "/mg",
            json={"op": "search", "query": token, "target_type": "fact"},
            headers=agent_key["headers"],
        )
        assert response.status_code == 200
        results = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert {r["data"]["title"] for r in results} == {"open note"}

    def test_flag_off_ignores_scopes(
        self, client, auth_headers, agent_key, scope_uuid
    ):
        """Without the flag, nothing is filtered (advisory rollout)."""
        token = self._tok()
        self._add_note(client, auth_headers, "scoped note", token, [_plain(scope_uuid)])
        response = client.post(
            "/mg",
            json={"op": "search", "query": token, "target_type": "fact"},
            headers=agent_key["headers"],
        )
        results = [
            r for r in response.get_json()["result"]["results"]
            if r.get("type") == "Note"
        ]
        assert {r["data"]["title"] for r in results} == {"scoped note"}


class TestSupersededGetInherit:
    """api#97 option (a): the get gate reads a superseded item's LIVE
    successor's labels — the relabel's decision lives on the live
    version; an unlabelled predecessor of a privately-relabeled item is
    NOT the global space."""

    def _add(self, client, headers, title, scopes=None):
        payload = {"op": "add", "type": "Note", "data": {"title": title}}
        if scopes is not None:
            payload["metadata"] = {"scopes": scopes}
        response = client.post("/mg", json=payload, headers=headers)
        assert response.status_code == 200, response.get_json()
        return response.get_json()["result"]["uuid"]

    def _supersede(self, old_uuid, successor_uuid):
        from system.soil import get_soil

        with get_soil() as soil:
            soil._get_connection().execute(
                "UPDATE item SET superseded_by = ? WHERE uuid = ?",
                (successor_uuid, old_uuid),
            )
            soil._get_connection().commit()

    def _get(self, client, headers, uuid):
        return client.post("/mg", json={"op": "get", "target": uuid}, headers=headers)

    def test_unlabeled_predecessor_gated_by_successor(
        self, client, auth_headers, agent_key, scope_uuid, enforcement
    ):
        old = self._add(client, auth_headers, "predecessor note")
        successor = self._add(
            client, auth_headers, "successor note", [_plain(scope_uuid)]
        )
        self._supersede(old, successor)

        # Outside the frame: denied (was 200 before the fix — the leak).
        response = self._get(client, agent_key["headers"], old)
        assert response.status_code == 403
        assert response.get_json()["error"]["details"]["code"] == "scope_read_denied"

        # Inside the frame: history stays readable, the predecessor's own
        # content comes back.
        assert _grant_agent(client, auth_headers, agent_key, scope_uuid).status_code == 201
        assert _enter(client, agent_key["headers"], scope_uuid).status_code == 200
        response = self._get(client, agent_key["headers"], old)
        assert response.status_code == 200
        assert response.get_json()["result"]["data"]["title"] == "predecessor note"

    def test_successor_unlabeled_is_global_history(
        self, client, auth_headers, agent_key, enforcement
    ):
        old = self._add(client, auth_headers, "unscoped history")
        successor = self._add(client, auth_headers, "unscoped live")
        self._supersede(old, successor)
        response = self._get(client, agent_key["headers"], old)
        assert response.status_code == 200

    def test_dangling_successor_denied(
        self, client, auth_headers, agent_key, enforcement
    ):
        old = self._add(client, auth_headers, "dangling history")
        self._supersede(old, "soil_does_not_exist")
        response = self._get(client, agent_key["headers"], old)
        assert response.status_code == 403

    def test_cycle_denied(self, client, auth_headers, agent_key, enforcement):
        """A cyclic supersession chain has no live version to gate by —
        resolve_live raises, the helper returns None, the read is denied
        (same ruling as the search plane's drop-the-row)."""
        a = self._add(client, auth_headers, "cycle a")
        b = self._add(client, auth_headers, "cycle b")
        from system.soil import get_soil

        with get_soil() as soil:
            conn = soil._get_connection()
            conn.execute(
                "UPDATE item SET superseded_by = ? WHERE uuid = ?", (b, a))
            conn.execute(
                "UPDATE item SET superseded_by = ? WHERE uuid = ?", (a, b))
            conn.commit()
        response = self._get(client, agent_key["headers"], a)
        assert response.status_code == 403
