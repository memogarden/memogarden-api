"""The lane's server touch (#29 + capability refresh):

- Task enters BASELINE_ENTITY_TYPES → agents file requests via /mg create
- Event enters BASELINE_ITEM_TYPES → agents record occurrences via /mg add
- PATCH refresh_capabilities re-derives from the module's CURRENT on-disk
  source (cache-bypassed) — provider code evolves, module identity doesn't.
"""

import uuid

import pytest


class TestBaselines:
    def test_mg_creates_task_entity(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Task",
                  "data": {"source": "tada", "source_id": "t-1",
                           "title": "file the request", "status": "confirmed"}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        result = response.get_json()["result"]
        assert result["type"] == "Task"
        assert result["uuid"].startswith("core_")

    def test_mg_adds_event_fact(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "add", "type": "Event",
                  "data": {"source_id": "e-1", "title": "it happened",
                           "kind": "task"},
                  "metadata": {"source": "tada", "status": "completed",
                               "resolved_at": "2026-11-19T08:00:00+00:00"}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        result = response.get_json()["result"]
        assert result["uuid"].startswith("soil_")

    def test_recurrence_createable_via_baseline(self, client, auth_headers):
        # Recurrence was already whitelisted (the finance type) — the shared
        # schema generalizes it; confirm the /mg path keeps working.
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Recurrence",
                  "data": {"source": "tada", "source_id": "tmpl-1",
                           "title": "Water plants",
                           "rrule": "FREQ=DAILY;INTERVAL=2"}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()


MODULE_V1 = '''
class RefreshProvider:
    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        return
        yield
'''


MODULE_V2 = '''
class RefreshProvider:
    ENTITY_UPSERTS = True
    ENTITY_RETIRES = True

    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        return
        yield
'''


@pytest.fixture
def refresh_module(tmp_path, monkeypatch):
    module_name = f"refresh_provider_{uuid.uuid4().hex[:8]}"
    module_file = tmp_path / f"{module_name}.py"
    module_file.write_text(MODULE_V1)
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"module": module_name, "file": module_file}


class TestCapabilityRefresh:
    def _register(self, client, auth_headers, refresh_module):
        response = client.post(
            "/api/v1/providers",
            json={"name": "refreshprov", "module": refresh_module["module"],
                  "class": "RefreshProvider", "interval": 3600, "config": {}},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()

    def test_refresh_rederives_from_current_source(
        self, client, auth_headers, refresh_module
    ):
        manifest = self._register(client, auth_headers, refresh_module)
        assert manifest["capabilities"] == ["sync"]

        # Provider code evolves on disk (v2 declares the entity lane).
        refresh_module["file"].write_text(MODULE_V2)

        patched = client.patch(
            "/api/v1/providers/refreshprov",
            json={"refresh_capabilities": True},
            headers=auth_headers,
        )
        assert patched.status_code == 200, patched.get_json()
        assert sorted(patched.get_json()["capabilities"]) == [
            "retire_entities", "sync", "upsert_entities",
        ]

        # Idempotent: another refresh keeps the derived set (no grants).
        patched = client.patch(
            "/api/v1/providers/refreshprov",
            json={"refresh_capabilities": True},
            headers=auth_headers,
        )
        assert sorted(patched.get_json()["capabilities"]) == [
            "retire_entities", "sync", "upsert_entities",
        ]

    def test_refresh_cannot_grant_undeclared_capabilities(
        self, client, auth_headers, refresh_module
    ):
        self._register(client, auth_headers, refresh_module)
        # v1 declares nothing — a refresh must derive exactly ["sync"],
        # never more (same Protocol validation registration ran).
        patched = client.patch(
            "/api/v1/providers/refreshprov",
            json={"refresh_capabilities": True},
            headers=auth_headers,
        )
        assert patched.get_json()["capabilities"] == ["sync"]
