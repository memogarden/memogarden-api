"""Entity-retire provider contract tests (#30 — the Task→Event conversion op).

Mirrors test_provider_entity_upserts.py: the provider module is generated
into tmp_path and registered in-process; ops live in a JSON file the test
rewrites between runs to drive upsert → retire → replay sequences.

Contract under test:
- {op: "retire_entity", type, key} soft-deletes via Tombstone supersession
  (the /mg forget mechanism minus the identity guard),
- is a success no-op on unknown/already-retired keys (conversion replays
  converge),
- writes an EntityDelta fact with ops {"retired": ...} for the audit trail,
- fails the run when the provider does not declare ENTITY_RETIRES,
- interops with plain fact yields and upserts in one sync (per-yield
  commits keep the conversion order: fact before retire).
"""

import json
import time
import uuid

import pytest


RETIRE_MODULE_TEMPLATE = '''
import json


class RetireProvider:
    ENTITY_UPSERTS = True
    ENTITY_RETIRES = True

    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        with open(self.config["ops_file"]) as fh:
            ops = json.load(fh)
        for op in ops:
            if op["kind"] == "upsert":
                yield {
                    "op": "upsert_entity",
                    "type": "Task",
                    "key": {"source": "tada", "source_id": op["source_id"]},
                    "data": {"source_id": op["source_id"], "title": op["title"]},
                    "metadata": {"source": "tada", "etag": op["etag"]},
                }
            elif op["kind"] == "retire":
                yield {
                    "op": "retire_entity",
                    "type": "Task",
                    "key": {"source": "tada", "source_id": op["source_id"]},
                }
            elif op["kind"] == "fact":
                yield {
                    "type": "Event",
                    "data": {"source_id": op["source_id"], "title": op["title"]},
                    "metadata": {"source": "tada", "status": op["status"]},
                }
'''


UNDECLARED_RETIRE_MODULE_TEMPLATE = '''
import json


class UndeclaredRetireProvider:
    ENTITY_UPSERTS = True

    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        yield {
            "op": "retire_entity",
            "type": "Task",
            "key": {"source": "tada", "source_id": "t1"},
        }
'''


@pytest.fixture
def retire_module(tmp_path, monkeypatch):
    module_name = f"retire_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(RETIRE_MODULE_TEMPLATE)
    ops_file = tmp_path / "ops.json"
    ops_file.write_text("[]")
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"module": module_name, "ops_file": str(ops_file)}


@pytest.fixture
def undeclared_retire_module(tmp_path, monkeypatch):
    module_name = f"undeclared_retire_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(UNDECLARED_RETIRE_MODULE_TEMPLATE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _payload(module, retire_module_info=None, **overrides):
    payload = {
        "name": "retireprov",
        "module": module,
        "class": "RetireProvider",
        "interval": 3600,
        "config": {},
    }
    if retire_module_info:
        payload["config"]["ops_file"] = retire_module_info["ops_file"]
    payload.update(overrides)
    return payload


def _register(client, auth_headers, payload):
    return client.post("/api/v1/providers", json=payload, headers=auth_headers)


def _run_and_wait(client, auth_headers, name, timeout=5.0):
    """Trigger a run and wait for THAT run to finish (polls last_run_id —
    last_status alone is stale across back-to-back runs)."""
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


def _set_ops(retire_module_info, ops):
    with open(retire_module_info["ops_file"], "w") as fh:
        json.dump(ops, fh)


def _tasks():
    from system.core import get_core

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT * FROM entity WHERE type = 'Task'"
        ).fetchall()
    out = {}
    for row in rows:
        entity = dict(row)
        entity["data"] = json.loads(entity["data"]) if entity["data"] else {}
        out[entity["data"].get("source_id")] = entity
    return out


def _tombstones():
    from system.core import get_core

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT * FROM entity WHERE type = 'Tombstone'"
        ).fetchall()
    return [dict(r) for r in rows]


def _events():
    from system.soil import get_soil

    with get_soil() as soil:
        rows = soil._get_connection().execute(
            "SELECT * FROM item WHERE _type = 'Event'"
        ).fetchall()
    return [dict(r) for r in rows]


def _deltas():
    from system.soil import get_soil

    with get_soil() as soil:
        rows = soil._get_connection().execute(
            "SELECT * FROM item WHERE _type = 'EntityDelta'"
        ).fetchall()
    return [dict(r) for r in rows]


class TestRetireRegistration:
    def test_registration_derives_retire_entities_capability(
        self, client, auth_headers, retire_module
    ):
        response = _register(
            client, auth_headers, _payload(retire_module["module"], retire_module)
        )
        assert response.status_code == 201
        capabilities = response.get_json()["capabilities"]
        assert "retire_entities" in capabilities
        assert "upsert_entities" in capabilities


class TestRetireContract:
    def test_retire_supersedes_and_writes_entitydelta(
        self, client, auth_headers, retire_module
    ):
        assert _register(
            client, auth_headers, _payload(retire_module["module"], retire_module)
        ).status_code == 201
        _set_ops(retire_module, [
            {"kind": "upsert", "source_id": "t1", "title": "File taxes", "etag": "e1"},
            {"kind": "retire", "source_id": "t1"},
        ])
        body = _run_and_wait(client, auth_headers, "retireprov")
        assert body["last_status"] == "success"

        tasks = _tasks()
        assert "t1" in tasks
        assert tasks["t1"]["superseded_by"] is not None
        # find_by_data excludes superseded rows — a replayed retire's key
        # must resolve to nothing (the idempotency foundation).
        from system.core import get_core

        with get_core() as core:
            assert core.entity.find_by_data(
                "Task", {"source": "tada", "source_id": "t1"}
            ) is None

        tombstones = _tombstones()
        assert len(tombstones) == 1
        assert tombstones[0]["uuid"] == tasks["t1"]["superseded_by"]

        deltas = _deltas()
        assert len(deltas) == 1
        delta_data = json.loads(deltas[0]["data"])
        assert delta_data["entity_type"] == "Task"
        assert delta_data["ops"]["retired"]["provider"] == "retireprov"
        assert delta_data["tombstone"] == f"core_{tasks['t1']['superseded_by']}"

    def test_retire_unknown_key_is_noop(self, client, auth_headers, retire_module):
        assert _register(
            client, auth_headers, _payload(retire_module["module"], retire_module)
        ).status_code == 201
        _set_ops(retire_module, [{"kind": "retire", "source_id": "ghost"}])
        body = _run_and_wait(client, auth_headers, "retireprov")
        assert body["last_status"] == "success"
        assert _tasks() == {}
        assert _tombstones() == []
        assert _deltas() == []

    def test_retire_replay_is_idempotent(self, client, auth_headers, retire_module):
        assert _register(
            client, auth_headers, _payload(retire_module["module"], retire_module)
        ).status_code == 201

        _set_ops(retire_module, [
            {"kind": "upsert", "source_id": "t1", "title": "File taxes", "etag": "e1"},
            {"kind": "retire", "source_id": "t1"},
        ])
        body = _run_and_wait(client, auth_headers, "retireprov")
        assert body["last_status"] == "success"

        # Replay: only the retire — success noop, no second tombstone,
        # no second delta.
        _set_ops(retire_module, [{"kind": "retire", "source_id": "t1"}]
        )
        body = _run_and_wait(client, auth_headers, "retireprov")
        assert body["last_status"] == "success"
        assert len(_tombstones()) == 1
        assert len(_deltas()) == 1

    def test_conversion_ordering_fact_upsert_retire(
        self, client, auth_headers, retire_module
    ):
        """One sync may freely interleave facts, upserts, and retires; the
        conversion order (fact before retire) holds on disk."""
        assert _register(
            client, auth_headers, _payload(retire_module["module"], retire_module)
        ).status_code == 201
        _set_ops(retire_module, [
            {"kind": "upsert", "source_id": "t1", "title": "File taxes", "etag": "e1"},
            {"kind": "fact", "source_id": "t1", "title": "File taxes", "status": "completed"},
            {"kind": "retire", "source_id": "t1"},
        ])
        body = _run_and_wait(client, auth_headers, "retireprov")
        assert body["last_status"] == "success"

        events = _events()
        assert len(events) == 1
        assert json.loads(events[0]["metadata"])["status"] == "completed"
        tasks = _tasks()
        assert tasks["t1"]["superseded_by"] is not None
        assert len(_deltas()) == 1

    def test_retire_without_declaration_fails_run(
        self, client, auth_headers, undeclared_retire_module
    ):
        response = _register(
            client, auth_headers,
            {
                "name": "undeclared-retire",
                "module": undeclared_retire_module,
                "class": "UndeclaredRetireProvider",
                "interval": 3600,
                "config": {},
            },
        )
        assert response.status_code == 201
        assert "retire_entities" not in response.get_json()["capabilities"]

        body = _run_and_wait(client, auth_headers, "undeclared-retire")
        assert body["last_status"] == "failed"
