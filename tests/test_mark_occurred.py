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

        # The Event fact (#31 convergence): linkage, ground truth, flat
        # belief projection.
        occurrences = _facts("Event")
        assert len(occurrences) == 1
        occ = occurrences[0]["data"]
        assert occ["event_uuid"] == body["event_uuid"]
        assert occ["title"] == "Weekly sync"
        assert occ["participants"] == ["a@x.com", "b@x.com"]
        assert occ["start_at"] == "2026-11-16T09:00:00+00:00"
        assert occ["ends_at"] == "2026-11-16T10:00:00+00:00"
        assert occ["location"] == "Room 1"
        assert occ["organizer"] == "me@gmail.com"
        # The belief status stays on the entity — the fact's verdict is
        # metadata.status, not the schedule belief.
        assert "status" not in occ
        # Provenance + verdict ride the metadata.
        m = occurrences[0]["metadata"]
        assert m["source"] == "google_calendar"
        assert m["account"] == "me@gmail.com"
        assert m["calendar_id"] == "me@gmail.com"
        assert occ["source_id"] == "evt_1"
        assert m["status"] == "occurred"
        assert m["resolved_at"] == body["occurred_at"]
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

        assert len(_facts("Event")) == 1
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
        assert _facts("Event")[0]["canonical_at"] == stamp

    def test_future_occurred_at_rejected(self, client, auth_headers, event_uuid):
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        response = _mark(client, auth_headers, event_uuid, {"occurred_at": future})
        assert response.status_code == 400
        assert _facts("Event") == []

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
        assert _facts("Event") == []

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
        assert len(_facts("Event")) == 1
        assert _facts("Event")[0]["metadata"]["resolved_at"] == stamp

    def test_unbound_api_key_forbidden(
        self, client, auth_headers, auth_headers_apikey, event_uuid
    ):
        response = _mark(client, auth_headers_apikey, event_uuid)
        assert response.status_code == 403
        assert _facts("Event") == []
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


# ============================================================================
# Dismiss (RFC-014 amendment 2026-11-26, api#57) — the did-not-occur verdict
# ============================================================================

def _dismiss(client, auth_headers, uuid, body=None):
    return client.post(
        f"/api/v1/events/{uuid}/dismiss",
        json=body or {},
        headers=auth_headers,
    )


class TestDismiss:
    def test_first_dismiss_creates_verdict_and_action(
        self, client, auth_headers, event_uuid
    ):
        response = _dismiss(client, auth_headers, event_uuid)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["idempotent"] is False
        assert body["event_uuid"].startswith("core_")

        # The entity carries the amendment's exact stamps.
        stored = _entities_of_type("Event")[event_uuid]
        assert stored["data"]["did_not_occur"] is True
        assert stored["data"]["dismissed_at"] == body["dismissed_at"]

        # The verdict fact: same flat projection, did_not_occur verdict.
        dismissals = [
            f for f in _facts("Event")
            if f["metadata"].get("status") == "did_not_occur"
        ]
        assert len(dismissals) == 1
        data = dismissals[0]["data"]
        assert data["event_uuid"] == body["event_uuid"]
        assert data["title"] == "Weekly sync"
        assert data["start_at"] == "2026-11-16T09:00:00+00:00"
        assert dismissals[0]["metadata"]["resolved_at"] == body["dismissed_at"]
        # canonical_at follows the cancelled honesty rule: the missed slot.
        assert dismissals[0]["canonical_at"] == "2026-11-16T09:00:00+00:00"

        # The Action fact: attributable dismissal.
        actions = [
            a for a in _facts("Action")
            if a["data"]["operation"] == "dismiss_event"
        ]
        assert len(actions) == 1
        assert actions[0]["data"]["actor_type"] == "operator"

    def test_reason_rides_verdict_metadata_only(
        self, client, auth_headers, event_uuid
    ):
        response = _dismiss(
            client, auth_headers, event_uuid, {"reason": "cancelled last week"}
        )
        assert response.status_code == 201, response.get_json()
        dismissals = [
            f for f in _facts("Event")
            if f["metadata"].get("status") == "did_not_occur"
        ]
        assert dismissals[0]["metadata"]["reason"] == "cancelled last week"
        # The entity stamps stay exactly as the amendment ruled — no
        # reason key on the entity.
        stored = _entities_of_type("Event")[event_uuid]
        assert "dismissal_reason" not in stored["data"]
        assert "reason" not in stored["data"]

    def test_idempotent_redismiss_returns_existing(
        self, client, auth_headers, event_uuid
    ):
        assert _dismiss(client, auth_headers, event_uuid).status_code == 201
        second = _dismiss(client, auth_headers, event_uuid, {"reason": "different body"})
        assert second.status_code == 200
        assert second.get_json()["idempotent"] is True
        # Verdicts are immutable — the differing body changes nothing.
        dismissals = [
            f for f in _facts("Event")
            if f["metadata"].get("status") == "did_not_occur"
        ]
        assert len(dismissals) == 1
        assert "reason" not in dismissals[0]["metadata"]

    def test_dismiss_after_mark_409(self, client, auth_headers, event_uuid):
        assert _mark(client, auth_headers, event_uuid).status_code == 201
        response = _dismiss(client, auth_headers, event_uuid)
        assert response.status_code == 409
        error = response.get_json()["error"]
        assert error["type"] == "ConflictError"
        assert error["details"]["code"] == "review_terminal"
        # No dismissal verdict landed.
        assert [
            f for f in _facts("Event")
            if f["metadata"].get("status") == "did_not_occur"
        ] == []

    def test_mark_after_dismiss_409(self, client, auth_headers, event_uuid):
        assert _dismiss(client, auth_headers, event_uuid).status_code == 201
        response = _mark(client, auth_headers, event_uuid)
        assert response.status_code == 409
        assert response.get_json()["error"]["details"]["code"] == "review_terminal"
        # No occurrence verdict landed.
        assert [
            f for f in _facts("Event")
            if f["metadata"].get("status") == "occurred"
        ] == []

    def test_future_dismissed_at_accepted(self, client, auth_headers, event_uuid):
        """The amendment's asymmetry: dismissing a future slot is
        legitimate — the slot will not happen."""
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        response = _dismiss(client, auth_headers, event_uuid, {"dismissed_at": future})
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["dismissed_at"] == future

    def test_naive_dismissed_at_rejected(self, client, auth_headers, event_uuid):
        response = _dismiss(
            client, auth_headers, event_uuid, {"dismissed_at": "2026-11-15T10:30:00"}
        )
        assert response.status_code == 400
        assert _facts("Event") == []

    def test_unparseable_dismissed_at_rejected(self, client, auth_headers, event_uuid):
        response = _dismiss(
            client, auth_headers, event_uuid, {"dismissed_at": "not-a-date"}
        )
        assert response.status_code == 400

    def test_unknown_event_404(self, client, auth_headers):
        response = _dismiss(client, auth_headers, "00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_non_event_entity_rejected(self, client, auth_headers):
        from system.core import get_core

        with get_core() as core:
            scope_uuid = core.entity.create(
                "Scope", data={"label": "Not an event either"}
            )
        response = _dismiss(client, auth_headers, scope_uuid)
        assert response.status_code == 400
        assert "not an Event" in response.get_json()["error"]["message"]

    def test_unbound_api_key_forbidden(
        self, client, auth_headers, auth_headers_apikey, event_uuid
    ):
        response = _dismiss(client, auth_headers_apikey, event_uuid)
        assert response.status_code == 403
        assert _facts("Event") == []

    def test_unauthenticated_401(self, client, event_uuid):
        response = client.post(f"/api/v1/events/{event_uuid}/dismiss", json={})
        assert response.status_code == 401

    def test_agent_bound_key_can_dismiss(self, client, auth_headers, event_uuid):
        agent_headers, agent_id = _agent_bound_headers(client, auth_headers)
        response = _dismiss(client, agent_headers, event_uuid)
        assert response.status_code == 201, response.get_json()
        actions = [
            a for a in _facts("Action")
            if a["data"]["operation"] == "dismiss_event"
        ]
        assert len(actions) == 1
        assert actions[0]["data"]["actor"] == agent_id
        assert actions[0]["data"]["actor_type"] == "agent"

    def test_raced_double_mark_converges_idempotent(
        self, client, auth_headers, event_uuid, monkeypatch
    ):
        """system#5 integration: when the verdict identity index rejects
        the insert (a cross-process verdict landed between our probe and
        our write), the mark converges idempotently on the verdict that
        landed first instead of erroring or duplicating."""
        import api.v1.events as events_module
        from system.soil import Fact, generate_soil_uuid, get_soil

        # The competing verdict, already in soil — written in the SAME
        # form the mark path uses (core_-prefixed), which is the identity
        # the verdict index binds.
        from utils import uid as uid_mod

        with get_soil() as soil:
            soil.create_fact(Fact(
                uuid=generate_soil_uuid(),
                _type="Event",
                realized_at="2026-11-27T00:00:00+00:00",
                canonical_at="2026-11-16T09:00:00+00:00",
                data={"event_uuid": uid_mod.add_core_prefix(event_uuid),
                      "source_id": "evt_1", "kind": "event"},
                metadata={"source": "google_calendar", "status": "occurred",
                          "resolved_at": "2026-11-16T09:30:00+00:00"},
                integrity_hash=None,
                fidelity="full",
            ))

        # Simulate the race window: the two probes miss (the competing
        # verdict is invisible to them), the insert hits the index, and
        # the post-rollback convergence probe sees it.
        real = events_module._existing_occurrence
        calls = {"n": 0}

        def flaky_probe(soil, plain):
            calls["n"] += 1
            return None if calls["n"] <= 2 else real(soil, plain)

        monkeypatch.setattr(events_module, "_existing_occurrence", flaky_probe)

        response = _mark(client, auth_headers, event_uuid)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["idempotent"] is True
        # Still exactly one occurrence verdict.
        assert len([
            f for f in _facts("Event")
            if f["metadata"].get("status") == "occurred"
        ]) == 1
