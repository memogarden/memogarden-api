"""Entity-upsert provider contract tests (RFC-014 §3).

Mirrors the test_providers.py pattern: provider modules are generated into
tmp_path and registered in-process; event payloads live in a JSON file the
test rewrites between runs to drive create → noop → update sequences.
"""

import json
import time
import uuid

import pytest


ENTITY_MODULE_TEMPLATE = '''
import json


class UpsertProvider:
    ENTITY_UPSERTS = True

    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        with open(self.config["events_file"]) as fh:
            events = json.load(fh)
        for event in events:
            yield {
                "op": "upsert_entity",
                "type": "Event",
                "key": {
                    "source": "google_calendar",
                    "account": "me@gmail.com",
                    "calendar_id": "cal1",
                    "source_id": event["source_id"],
                },
                "data": dict(event.get("data") or {}),
                "metadata": dict(event.get("metadata") or {}),
            }
'''


class UndeclaredUpsertProvider:
    pass


UNDECLARED_MODULE_TEMPLATE = '''
class UndeclaredUpsertProvider:
    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        yield {
            "op": "upsert_entity",
            "type": "Event",
            "key": {"source": "s", "account": "a", "calendar_id": "c", "source_id": "x"},
            "data": {"source_id": "x"},
            "metadata": {"source": "s", "account": "a", "calendar_id": "c", "etag": "e"},
        }
'''


@pytest.fixture
def entity_module(tmp_path, monkeypatch):
    module_name = f"entity_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(ENTITY_MODULE_TEMPLATE)
    events_file = tmp_path / "events.json"
    events_file.write_text("[]")
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"module": module_name, "events_file": str(events_file)}


@pytest.fixture
def undeclared_module(tmp_path, monkeypatch):
    module_name = f"undeclared_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(UNDECLARED_MODULE_TEMPLATE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _payload(module, entity_module_info=None, **overrides):
    payload = {
        "name": "testprov",
        "module": module,
        "class": "UpsertProvider",
        "interval": 3600,
        "config": {},
    }
    if entity_module_info:
        payload["config"]["events_file"] = entity_module_info["events_file"]
    payload.update(overrides)
    return payload


def _register(client, auth_headers, payload):
    return client.post("/api/v1/providers", json=payload, headers=auth_headers)


def _run_and_wait(client, auth_headers, name, timeout=5.0):
    """Trigger a run and wait for THAT run to finish.

    Polls last_run_id (stamped only by finish_run for the matching run) —
    last_status alone is stale across back-to-back runs, since the
    previous run's 'success' is still in the row while the next executes.
    """
    started = client.post(f"/api/v1/providers/{name}/run", headers=auth_headers)
    assert started.status_code == 202, started.get_json()
    run_id = started.get_json()["run_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/providers/{name}", headers=auth_headers).get_json()
        if body.get("last_run_id") == run_id and body.get("last_status") in (
            "success",
            "failed",
        ):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} for {name} did not finish within {timeout}s")


def _set_events(entity_module_info, events):
    with open(entity_module_info["events_file"], "w") as fh:
        json.dump(events, fh)


def _event(source_id, etag, **extra_data):
    data = {
        "source_id": source_id,
        "title": "Standup",
        "starts_at": "2026-11-16T09:00:00+00:00",
        "status": "confirmed",
    }
    data.update(extra_data)
    return {
        "source_id": source_id,
        "data": data,
        "metadata": {
            "source": "google_calendar",
            "account": "me@gmail.com",
            "calendar_id": "cal1",
            "etag": etag,
        },
    }


def _tombstone(source_id, etag):
    return {
        "source_id": source_id,
        "data": {},
        "metadata": {
            "source": "google_calendar",
            "account": "me@gmail.com",
            "calendar_id": "cal1",
            "etag": etag,
            "source_status": "cancelled",
            "removed_at_source": "2026-11-16T12:00:00+00:00",
            "detection_source": "sync_tombstone",
        },
    }


def _entities():
    from system.core import get_core

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT * FROM entity WHERE type = 'Event'"
        ).fetchall()
    out = {}
    for row in rows:
        entity = dict(row)
        entity["data"] = json.loads(entity["data"]) if entity["data"] else {}
        out[entity["data"].get("source_id")] = entity
    return out


def _deltas():
    from system.soil import get_soil

    with get_soil() as soil:
        rows = soil._get_connection().execute(
            "SELECT * FROM item WHERE _type = 'EntityDelta'"
        ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def triage_uuid(client, auth_headers):
    """Same shape as test_providers.py's fixture: mint the builtin triage
    scope by label via the semantic API."""
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope", "data": {"label": "Triage / Untrusted"}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


class TestEntityUpsertContract:
    def test_registration_derives_upsert_entities_capability(
        self, client, auth_headers, entity_module
    ):
        response = _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        )
        assert response.status_code == 201
        assert "upsert_entities" in response.get_json()["capabilities"]

    def test_first_run_creates_entities_with_triage_scope(
        self, client, auth_headers, entity_module, triage_uuid
    ):
        _set_events(entity_module, [_event("e1", "etag-1"), _event("e2", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        entities = _entities()
        assert len(entities) == 2
        # Storage mapping: merged data+metadata lands flat in entity.data.
        assert entities["e1"]["data"]["etag"] == "etag-1"
        assert entities["e1"]["data"]["source"] == "google_calendar"
        # Default policy (no policy configured): fail-closed triage label
        # rides data.scopes — the entity analogue of fact metadata.scopes.
        plain_triage = triage_uuid.split("core_", 1)[-1]
        assert entities["e1"]["data"]["scopes"] == [plain_triage]

    def test_unchanged_etag_is_noop(self, client, auth_headers, entity_module):
        _set_events(entity_module, [_event("e1", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        assert len(_entities()) == 1
        assert len(_deltas()) == 0

    def test_changed_etag_updates_and_writes_entitydelta(
        self, client, auth_headers, entity_module, triage_uuid
    ):
        _set_events(entity_module, [_event("e1", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"
        pre_hash = _entities()["e1"]["hash"]

        _set_events(
            entity_module, [_event("e1", "etag-2", title="Standup (moved)")]
        )
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        entities = _entities()
        assert entities["e1"]["data"]["title"] == "Standup (moved)"
        assert entities["e1"]["data"]["etag"] == "etag-2"
        assert len(_entities()) == 1  # update, not a second entity

        deltas = _deltas()
        assert len(deltas) == 1
        delta_data = json.loads(deltas[0]["data"])
        assert delta_data["entity_type"] == "Event"
        assert delta_data["entity_uuid"].startswith("core_")
        assert delta_data["entity_id"] == entities["e1"]["uuid"]
        assert delta_data["commit"] == entities["e1"]["hash"]
        assert delta_data["parent"] == pre_hash  # pre-update entity hash
        changed = delta_data["ops"]["changed"]
        assert changed["title"] == {
            "from": "Standup",
            "to": "Standup (moved)",
        }
        assert changed["etag"]["to"] == "etag-2"

    def test_explicit_null_clears_stored_field(
        self, client, auth_headers, entity_module
    ):
        # RFC-014 §3.2 merge semantics: an explicit null in the payload
        # clears the stored value; absent keys would have kept it.
        _set_events(entity_module, [_event("e1", "etag-1", location="Room 1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        _set_events(entity_module, [_event("e1", "etag-2", location=None)])
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        stored = _entities()["e1"]
        assert stored["data"]["location"] is None
        assert stored["data"]["title"] == "Standup"  # untouched fields survive

    def test_tombstone_merge_preserves_belief_fields(
        self, client, auth_headers, entity_module
    ):
        _set_events(entity_module, [_event("e1", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        _set_events(entity_module, [_tombstone("e1", "etag-1")])
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        stored = _entities()["e1"]
        # Stamps landed…
        assert stored["data"]["source_status"] == "cancelled"
        assert stored["data"]["detection_source"] == "sync_tombstone"
        # …and the tombstone's stamp-only payload did not clobber beliefs.
        assert stored["data"]["title"] == "Standup"
        assert stored["data"]["starts_at"] == "2026-11-16T09:00:00+00:00"

    def test_reyielded_tombstone_is_noop(self, client, auth_headers, entity_module):
        _set_events(entity_module, [_event("e1", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"
        _set_events(entity_module, [_tombstone("e1", "etag-1")])
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        # Audit re-delivery of the same stamp: etag AND source_status both
        # unchanged → no new delta.
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"
        assert len(_deltas()) == 1

    def test_data_metadata_collision_fails_run(
        self, client, auth_headers, entity_module
    ):
        bad = _event("e1", "etag-1")
        bad["data"]["etag"] = "collision"
        _set_events(entity_module, [bad])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "failed"

    def test_null_key_field_fails_run(self, client, auth_headers, entity_module):
        # json_extract = NULL never matches — a null key field would
        # create a duplicate entity every run; the seam refuses it.
        bad = _event("e1", "etag-1")
        bad["metadata"]["calendar_id"] = None
        _set_events(entity_module, [bad])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "failed"

    def test_entity_yield_without_declaration_fails_run(
        self, client, auth_headers, undeclared_module
    ):
        payload = {
            "name": "testprov",
            "module": undeclared_module,
            "class": "UndeclaredUpsertProvider",
            "interval": 3600,
            "config": {},
        }
        assert _register(client, auth_headers, payload).status_code == 201
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "failed"

    def test_null_default_policy_delivers_unscoped_entities(
        self, client, auth_headers, entity_module
    ):
        _set_events(entity_module, [_event("e1", "etag-1")])
        assert _register(
            client, auth_headers, _payload(entity_module["module"], entity_module)
        ).status_code == 201
        assert client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={"default": None},
            headers=auth_headers,
        ).status_code == 200
        assert _run_and_wait(client, auth_headers, "testprov")["last_status"] == "success"

        stored = _entities()["e1"]
        assert "scopes" not in stored["data"]