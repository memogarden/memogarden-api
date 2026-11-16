"""mark-occurred endpoint tests (RFC-014 §6): authz, idempotency, facts."""

import json

import pytest


DEFAULT_EVENT_DATA = {
    "source_id": "evt_1",
    "title": "Weekly sync",
    "starts_at": "2026-11-16T09:00:00+00:00",
    "ends_at": "2026-11-16T10:00:00+00:00",
    "location": "Room 1",
    "participants": ["a@x.com", "b@x.com"],
    "organizer": "me@gmail.com",
    "status": "confirmed",
    "source": "google_calendar",
    "account": "me@gmail.com",
    "calendar_id": "me@gmail.com",
    "event_id": "evt_1",
    "etag": '"123"',
}


@pytest.fixture
def event_uuid():
    from system.core import get_core

    with get_core() as core:
        plain = core.entity.create("Event", data=dict(DEFAULT_EVENT_DATA))
    return plain


def _mark(client, auth_headers, uuid, body=None):
    return client.post(
        f"/api/v1/events/{uuid}/mark-occurred",
        json=body or {},
        headers=auth_headers,
    )


def _entities_of_type(etype):
    from system.core import get_core

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT * FROM entity WHERE type = ?", (etype,)
        ).fetchall()
    out = {}
    for row in rows:
        entity = dict(row)
        entity["data"] = json.loads(entity["data"]) if entity["data"] else {}
        out[entity["uuid"]] = entity
    return out


def _facts(fact_type):
    from system.soil import get_soil

    with get_soil() as soil:
        rows = soil._get_connection().execute(
            "SELECT * FROM item WHERE _type = ?", (fact_type,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item["data"]) if item["data"] else {}
        item["metadata"] = json.loads(item["metadata"]) if item.get("metadata") else {}
        out.append(item)
    return out


def _agent_bound_headers(client, auth_headers):
    """Mint an Agent entity + an agent-bound API key via the real RFC-010
    endpoints; return headers authenticating as the agent."""
    agent_resp = client.post(
        "/agents",
        json={"label": "test-agent"},
        headers=auth_headers,
    )
    assert agent_resp.status_code == 201, agent_resp.get_json()
    agent_id = agent_resp.get_json()["agent_id"]

    key_resp = client.post(
        "/api-keys/",
        json={"name": "agent-key", "agent_id": agent_id},
        headers=auth_headers,
    )
    assert key_resp.status_code == 201, key_resp.get_json()
    body = key_resp.get_json()
    raw = body.get("key") or body.get("raw_key") or body.get("api_key")
    assert raw, f"no raw key in creation response: {body}"
    return {"X-API-Key": raw, "Content-Type": "application/json"}, agent_id


class TestMarkOccurred:
    def test_first_mark_creates_occurrence_and_action(
        self, client, auth_headers, event_uuid
    ):
        response = _mark(client, auth_headers, event_uuid)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["idempotent"] is False
        assert body["event_uuid"].startswith("core_")

        # The entity now carries occurred_at.
        stored = _entities_of_type("Event")[event_uuid]
        assert stored["data"]["occurred_at"] == body["occurred_at"]

        # The EventOccurrence fact: linkage, ground truth, snapshot.
        occurrences = _facts("EventOccurrence")
        assert len(occurrences) == 1
        occ = occurrences[0]["data"]
        assert occ["event_uuid"] == body["event_uuid"]
        assert occ["occurred_at"] == body["occurred_at"]
        assert occ["snapshot"]["title"] == "Weekly sync"
        assert occ["snapshot"]["participants"] == ["a@x.com", "b@x.com"]
        assert occ["snapshot"]["status"] == "confirmed"
        # Provenance rides the occurrence metadata (review P2 gap).
        assert occurrences[0]["metadata"]["source"] == "google_calendar"
        assert occurrences[0]["metadata"]["account"] == "me@gmail.com"
        assert occurrences[0]["metadata"]["calendar_id"] == "me@gmail.com"
        assert occurrences[0]["metadata"]["source_id"] == "evt_1"
        # canonical_at = occurred_at (user-meaning time).
        assert occurrences[0]["canonical_at"] == body["occurred_at"]

        # The Action fact: attributable marking.
        actions = _facts("Action")
        assert len(actions) == 1
        action = actions[0]["data"]
        assert action["operation"] == "mark_occurred"
        assert action["actor"] == "testuser"
        assert action["actor_type"] == "operator"

    def test_idempotent_remark_returns_existing(
        self, client, auth_headers, event_uuid
    ):
        assert _mark(client, auth_headers, event_uuid).status_code == 201
        second = _mark(client, auth_headers, event_uuid)
        assert second.status_code == 200
        assert second.get_json()["idempotent"] is True

        assert len(_facts("EventOccurrence")) == 1
        assert len(_facts("Action")) == 1

    def test_prefixed_uuid_accepted(self, client, auth_headers, event_uuid):
        response = _mark(client, auth_headers, f"core_{event_uuid}")
        assert response.status_code == 201
        assert response.get_json()["event_uuid"] == f"core_{event_uuid}"

    def test_explicit_occurred_at_honored(self, client, auth_headers, event_uuid):
        stamp = "2026-11-15T10:30:00+00:00"
        response = _mark(client, auth_headers, event_uuid, {"occurred_at": stamp})
        assert response.status_code == 201
        assert response.get_json()["occurred_at"] == stamp
        stored = _entities_of_type("Event")[event_uuid]
        assert stored["data"]["occurred_at"] == stamp
        assert _facts("EventOccurrence")[0]["canonical_at"] == stamp

    def test_future_occurred_at_rejected(self, client, auth_headers, event_uuid):
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        response = _mark(client, auth_headers, event_uuid, {"occurred_at": future})
        assert response.status_code == 400
        assert _facts("EventOccurrence") == []

    def test_naive_occurred_at_rejected(self, client, auth_headers, event_uuid):
        response = _mark(
            client, auth_headers, event_uuid, {"occurred_at": "2026-11-15T10:30:00"}
        )
        assert response.status_code == 400

    def test_unparseable_occurred_at_rejected(self, client, auth_headers, event_uuid):
        response = _mark(
            client, auth_headers, event_uuid, {"occurred_at": "not-a-date"}
        )
        assert response.status_code == 400
        assert _facts("EventOccurrence") == []

    def test_offset_normalized_to_utc(self, client, auth_headers, event_uuid):
        stamp = "2026-11-15T05:30:00-05:00"  # == 10:30Z
        response = _mark(client, auth_headers, event_uuid, {"occurred_at": stamp})
        assert response.status_code == 201
        assert response.get_json()["occurred_at"] == "2026-11-15T10:30:00+00:00"

    def test_unknown_event_404(self, client, auth_headers):
        response = _mark(client, auth_headers, "00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_non_event_entity_rejected(self, client, auth_headers):
        from system.core import get_core

        with get_core() as core:
            scope_uuid = core.entity.create(
                "Scope", data={"label": "Not an event"}
            )
        response = _mark(client, auth_headers, scope_uuid)
        assert response.status_code == 400
        assert "not an Event" in response.get_json()["error"]["message"]

    def test_repair_after_partial_mark(self, client, auth_headers, event_uuid):
        # Crash window (core-first ordering): entity marked, facts lost.
        # Re-marking repairs the facts without overwriting ground truth.
        from system.core import get_core

        stamp = "2026-11-15T08:00:00+00:00"
        with get_core() as core:
            entity = core.entity.get_by_id(event_uuid)
            core.entity.update_data(
                event_uuid, {**entity["data"], "occurred_at": stamp}
            )
            core._conn.commit()

        response = _mark(client, auth_headers, event_uuid)
        assert response.status_code == 201
        body = response.get_json()
        assert body["repaired"] is True
        assert body["occurred_at"] == stamp  # NOT overwritten by "now"
        assert len(_facts("EventOccurrence")) == 1
        assert _facts("EventOccurrence")[0]["data"]["occurred_at"] == stamp

    def test_unbound_api_key_forbidden(
        self, client, auth_headers, auth_headers_apikey, event_uuid
    ):
        response = _mark(client, auth_headers_apikey, event_uuid)
        assert response.status_code == 403
        assert _facts("EventOccurrence") == []
        assert _facts("Action") == []

    def test_unauthenticated_401(self, client, event_uuid):
        response = client.post(
            f"/api/v1/events/{event_uuid}/mark-occurred", json={}
        )
        assert response.status_code == 401

    def test_agent_bound_key_can_mark(self, client, auth_headers, event_uuid):
        agent_headers, agent_id = _agent_bound_headers(client, auth_headers)
        response = _mark(client, agent_headers, event_uuid)
        assert response.status_code == 201, response.get_json()

        actions = _facts("Action")
        # Two Actions exist: the RFC-010 identity-mint audit plus ours —
        # filter to the marking.
        marking_actions = [
            a for a in actions if a["data"]["operation"] == "mark_occurred"
        ]
        assert len(marking_actions) == 1
        action = marking_actions[0]["data"]
        assert action["actor"] == agent_id  # the Agent entity (prefixed)
        assert action["actor_type"] == "agent"
        # The occurrence's idempotency anchor works for agent markings too.
        again = _mark(client, agent_headers, event_uuid)
        assert again.status_code == 200
        assert again.get_json()["idempotent"] is True
