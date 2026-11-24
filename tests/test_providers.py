"""Tests for the provider registry API and runtime (RFC-004 §12.2).

Provider modules under test are generated into tmp_path and made importable
via sys.path — the registration endpoint imports them in-process, exactly as
it would a real provider in the server environment.
"""

import json
import time
import uuid

import pytest


DUMMY_MODULE_TEMPLATE = '''
class {class_name}:
    def __init__(self, config=None):
        self.config = config or {{}}
        self._count = int(self.config.get("fact_count", 2))

    def sync(self, since=None):
        for i in range(self._count):
            yield {{"type": "Note", "data": {{"description": "dummy fact %d" % i}}, "metadata": {{}}}}
'''


@pytest.fixture
def dummy_module(tmp_path, monkeypatch):
    """A valid provider module yielding fact dicts; unique name per test."""
    module_name = f"dummy_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        DUMMY_MODULE_TEMPLATE.format(class_name="DummyProvider")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


@pytest.fixture
def slow_module(tmp_path, monkeypatch):
    """A provider whose sync sleeps, creating a real in-flight window."""
    module_name = f"slow_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        "import time\n"
        "class SlowProvider:\n"
        "    def __init__(self, config=None):\n"
        "        self.config = config or {}\n"
        "    def sync(self, since=None):\n"
        "        time.sleep(0.4)\n"
        "        yield {\"type\": \"Note\", \"data\": {\"description\": \"slow\"}, \"metadata\": {}}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _payload(module, **overrides):
    payload = {
        "name": "testprov",
        "module": module,
        "class": "DummyProvider",
        "interval": 3600,
        "config": {"fact_count": 2},
    }
    payload.update(overrides)
    return payload


def _register(client, auth_headers, payload):
    return client.post("/api/v1/providers", json=payload, headers=auth_headers)


def _wait_for_finish(client, auth_headers, name, timeout=5.0):
    """Poll the manifest until last_status leaves the unset/running state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/providers/{name}", headers=auth_headers).get_json()
        if body.get("last_status") in ("success", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"provider run for {name} did not finish within {timeout}s")


class TestRegistration:
    def test_register_success(self, client, auth_headers, dummy_module):
        response = _register(client, auth_headers, _payload(dummy_module))
        assert response.status_code == 201
        manifest = response.get_json()
        assert manifest["name"] == "testprov"
        assert manifest["module"] == dummy_module
        assert manifest["class"] == "DummyProvider"
        assert manifest["capabilities"] == ["sync"]
        assert manifest["enabled"] is True
        assert manifest["config"] == {"fact_count": 2}
        assert manifest["registered_at"]
        assert manifest["next_due_at"] is None  # NULL = due now

    def test_register_duplicate_conflict(self, client, auth_headers, dummy_module):
        assert _register(client, auth_headers, _payload(dummy_module)).status_code == 201
        response = _register(client, auth_headers, _payload(dummy_module))
        assert response.status_code == 409
        assert response.get_json()["error"]["type"] == "ConflictError"

    def test_register_requires_jwt(
        self, client, auth_headers, auth_headers_apikey, dummy_module
    ):
        """Registering ingestion code is a trust act (RFC-015 §2.1 #6):
        JWT-only, mirroring PUT /<name>/scope-policy and account
        registration — API keys keep read + run-trigger access (#58)."""
        denied = _register(client, auth_headers_apikey, _payload(dummy_module))
        assert denied.status_code == 401

        allowed = _register(client, auth_headers, _payload(dummy_module))
        assert allowed.status_code == 201

    def test_register_rejects_reserved_name_run(self, client, auth_headers, dummy_module):
        response = _register(client, auth_headers, _payload(dummy_module, name="run"))
        assert response.status_code == 400

    def test_register_rejects_invalid_schema(self, client, auth_headers, dummy_module):
        response = _register(client, auth_headers, _payload(dummy_module, name="Not-A-Slug"))
        assert response.status_code == 400
        response = _register(client, auth_headers, _payload(dummy_module, interval=30))
        assert response.status_code == 400

    def test_register_rejects_unimportable_module(self, client, auth_headers):
        response = _register(
            client, auth_headers, _payload("no_such_module_anywhere_12345")
        )
        assert response.status_code == 400
        error = response.get_json()["error"]
        assert error["type"] == "ValidationError"
        assert "not importable" in error["message"]

    def test_register_rejects_missing_class(self, client, auth_headers, dummy_module):
        response = _register(client, auth_headers, _payload(dummy_module, **{"class": "Missing"}))
        assert response.status_code == 400

    def test_register_rejects_class_without_sync(self, client, auth_headers, tmp_path, monkeypatch):
        module_name = f"nosync_provider_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{module_name}.py").write_text("class NotAProvider:\n    pass\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        response = _register(client, auth_headers, _payload(module_name, **{"class": "NotAProvider"}))
        assert response.status_code == 400
        assert "Provider Protocol" in response.get_json()["error"]["message"]

    def test_register_derives_create_relations_capability(self, client, auth_headers, tmp_path, monkeypatch):
        module_name = f"rel_provider_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{module_name}.py").write_text(
            "class RelProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        return iter(())\n"
            "    def create_relations(self, fact):\n"
            "        return []\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        response = _register(client, auth_headers, _payload(module_name, **{"class": "RelProvider"}))
        assert response.status_code == 201
        assert response.get_json()["capabilities"] == ["sync", "create_relations"]

    def test_run_relations_resolve_same_run_facts(self, client, auth_headers, tmp_path, monkeypatch):
        """The runtime commits each fact before create_relations — a provider
        resolving a fact stored earlier in the SAME run through its own Soil
        connection (separate transaction) must find it (tada note→task,
        gmail reply→parent)."""
        module_name = f"same_run_rel_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{module_name}.py").write_text(
            "class SameRunRelProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        yield {\"type\": \"Task\", \"data\": {\"source_id\": \"t1\", \"title\": \"x\"}, \"metadata\": {}}\n"
            "        yield {\"type\": \"Note\", \"data\": {\"description\": \"n\"}, \"metadata\": {\"task_source_id\": \"t1\"}}\n"
            "    def create_relations(self, fact):\n"
            "        if getattr(fact, \"_type\", None) != \"Note\":\n"
            "            return []\n"
            "        from system.soil import generate_soil_uuid, get_soil\n"
            "        from system.soil.relation import SystemRelation\n"
            "        with get_soil() as soil:\n"
            "            row = soil._get_connection().execute(\n"
            "                \"SELECT uuid FROM item WHERE _type='Task' AND json_extract(data,'$.source_id')='t1'\"\n"
            "            ).fetchone()\n"
            "        if row is None:\n"
            "            return []\n"
            "        return [SystemRelation(\n"
            "            uuid=generate_soil_uuid(), kind=\"contained_by\", source=fact.uuid,\n"
            "            source_type=\"item\", target=row[\"uuid\"], target_type=\"item\",\n"
            "            created_at=0, evidence=None, metadata=None)]\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        body = _register(client, auth_headers, _payload(module_name, **{"class": "SameRunRelProvider"}))
        assert body.status_code == 201
        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        manifest = _wait_for_finish(client, auth_headers, "testprov")
        assert manifest["last_status"] == "success"
        # The relation row itself is the proof of cross-connection visibility.
        from system.soil import get_soil

        with get_soil() as soil:
            (relations,) = soil._get_connection().execute(
                "SELECT COUNT(*) FROM system_relation"
            ).fetchone()
        assert relations == 1


class TestListGetPatch:
    def test_list_empty(self, client, auth_headers):
        response = client.get("/api/v1/providers", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json() == {"providers": [], "count": 0}

    def test_get_unknown_404(self, client, auth_headers):
        response = client.get("/api/v1/providers/nope", headers=auth_headers)
        assert response.status_code == 404
        assert response.get_json()["error"]["type"] == "ResourceNotFound"

    def test_patch_updates_fields(self, client, auth_headers, dummy_module):
        _register(client, auth_headers, _payload(dummy_module))
        response = client.patch(
            "/api/v1/providers/testprov",
            json={"enabled": False, "interval": 120, "config": {"fact_count": 5}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        manifest = response.get_json()
        assert manifest["enabled"] is False
        assert manifest["interval"] == 120
        assert manifest["config"] == {"fact_count": 5}
        assert manifest["capabilities"] == ["sync"]  # untouched

    def test_patch_unknown_404(self, client, auth_headers):
        response = client.patch(
            "/api/v1/providers/nope", json={"enabled": False}, headers=auth_headers
        )
        assert response.status_code == 404


class TestRuns:
    def test_manual_run_executes_and_records(self, client, auth_headers, dummy_module):
        _register(client, auth_headers, _payload(dummy_module))
        response = client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert response.status_code == 202
        accepted = response.get_json()
        assert accepted["status"] == "running"
        assert accepted["run_id"]

        manifest = _wait_for_finish(client, auth_headers, "testprov")
        assert manifest["last_status"] == "success"
        assert manifest["last_run_id"] == accepted["run_id"]
        assert manifest["last_run_at"]
        assert manifest["next_due_at"]  # scheduled for finished_at + interval

    def test_run_yields_facts_into_soil(self, client, auth_headers, dummy_module):
        _register(client, auth_headers, _payload(dummy_module))
        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        manifest = _wait_for_finish(client, auth_headers, "testprov")

        from system.core import _create_connection

        conn = _create_connection()
        try:
            row = conn.execute(
                "SELECT status, detail FROM provider_runs WHERE id = ?",
                (manifest["last_run_id"],),
            ).fetchone()
            assert row["status"] == "success"
            assert json.loads(row["detail"])["facts_inserted"] == 2
        finally:
            conn.close()

    def test_run_unknown_provider_404(self, client, auth_headers):
        response = client.post("/api/v1/providers/nope/run", headers=auth_headers)
        assert response.status_code == 404

    def test_run_pings_embed_daemon(self, client, auth_headers, dummy_module,
                                    ping_server):
        """Completed run wakes the semantic index (embed-on-ingest ping).

        The ping lands on the run thread just after finish_run, so the
        test polls the recording server rather than the manifest (no
        race). See tests/test_provider_embed_ping.py for the ping
        contract itself.
        """
        _register(client, auth_headers, _payload(dummy_module))
        response = client.post(
            "/api/v1/providers/testprov/run", headers=auth_headers
        )
        assert response.status_code == 202
        assert json.loads(ping_server.wait_for_ping()) == {"op": "ping"}

    def test_run_single_flight_conflict(self, client, auth_headers, slow_module):
        _register(client, auth_headers, _payload(slow_module, **{"class": "SlowProvider"}))
        first = client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert first.status_code == 202

        second = client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert second.status_code == 409
        assert second.get_json()["error"]["type"] == "ConflictError"

        manifest = _wait_for_finish(client, auth_headers, "testprov")
        assert manifest["last_status"] == "success"

    def test_run_all_due_skips_disabled(self, client, auth_headers, dummy_module):
        _register(client, auth_headers, _payload(dummy_module))
        _register(
            client, auth_headers, _payload(dummy_module, name="offprov", enabled=False)
        )

        response = client.post("/api/v1/providers/run", headers=auth_headers)
        assert response.status_code == 202
        body = response.get_json()
        assert [t["name"] for t in body["triggered"]] == ["testprov"]

        _wait_for_finish(client, auth_headers, "testprov")

    def test_failed_provider_marks_failure(self, client, auth_headers, tmp_path, monkeypatch):
        module_name = f"fail_provider_{uuid.uuid4().hex[:8]}"
        (tmp_path / f"{module_name}.py").write_text(
            "class FailingProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        raise RuntimeError('sync exploded')\n"
            "        yield  # pragma: no cover - makes this a generator\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        _register(client, auth_headers, _payload(module_name, **{"class": "FailingProvider"}))

        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        manifest = _wait_for_finish(client, auth_headers, "testprov")
        assert manifest["last_status"] == "failed"

        from system.core import _create_connection

        conn = _create_connection()
        try:
            row = conn.execute(
                "SELECT detail FROM provider_runs WHERE id = ?",
                (manifest["last_run_id"],),
            ).fetchone()
            assert "sync exploded" in json.loads(row["detail"])["error"]
        finally:
            conn.close()


class TestOrphanCleanup:
    def test_cleanup_marks_stale_running_rows_failed(self, client, auth_headers, dummy_module):
        from api.provider_registry import cleanup_orphaned_runs
        from system.core import _create_connection

        _register(client, auth_headers, _payload(dummy_module))
        conn = _create_connection()
        try:
            conn.execute(
                """INSERT INTO provider_runs (id, provider_name, status, started_at, triggered_by)
                   VALUES ('orphan-1', 'testprov', 'running', '2026-11-01T00:00:00Z', 'heartbeat')"""
            )
            conn.commit()
        finally:
            conn.close()

        assert cleanup_orphaned_runs() >= 1

        conn = _create_connection()
        try:
            row = conn.execute(
                "SELECT status FROM provider_runs WHERE id = 'orphan-1'"
            ).fetchone()
            assert row["status"] == "failed"
        finally:
            conn.close()


class TestOriginPin:
    """Origin pin: registration only activates operator-placed code."""

    @pytest.fixture
    def pinned_roots(self, tmp_path, monkeypatch):
        """A provider root dir with MEMOGARDEN_PROVIDER_ROOTS pointed at it."""
        root = tmp_path / "providers"
        root.mkdir()
        monkeypatch.setenv("MEMOGARDEN_PROVIDER_ROOTS", str(root))
        return root

    def test_unpinned_allows_any_importable(self, client, auth_headers, dummy_module, monkeypatch):
        monkeypatch.delenv("MEMOGARDEN_PROVIDER_ROOTS", raising=False)
        monkeypatch.delenv("MEMOGARDEN_DATA_DIR", raising=False)
        response = _register(client, auth_headers, _payload(dummy_module))
        assert response.status_code == 201

    def test_file_in_root_registers_by_bare_name(self, client, auth_headers, pinned_roots):
        (pinned_roots / "plugprov.py").write_text(
            "class PlugProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        return iter(())\n"
        )
        response = _register(
            client, auth_headers,
            _payload("plugprov", **{"class": "PlugProvider"}),
        )
        assert response.status_code == 201
        assert response.get_json()["capabilities"] == ["sync"]

    def test_package_dir_in_root_registers(self, client, auth_headers, pinned_roots):
        pkg = pinned_roots / "pkgprov"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "class PkgProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        return iter(())\n"
        )
        response = _register(
            client, auth_headers,
            _payload("pkgprov", **{"class": "PkgProvider"}),
        )
        assert response.status_code == 201

    def test_origin_rejects_module_outside_roots(self, client, auth_headers, pinned_roots, tmp_path, monkeypatch):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        module_name = f"outside_provider_{uuid.uuid4().hex[:8]}"
        (outside / f"{module_name}.py").write_text(
            "class OutsideProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        return iter(())\n"
        )
        monkeypatch.syspath_prepend(str(outside))
        response = _register(
            client, auth_headers,
            _payload(module_name, **{"class": "OutsideProvider"}),
        )
        assert response.status_code == 400
        error = response.get_json()["error"]
        assert "provider roots" in error["message"]
        assert "origin" in error["details"]

    def test_symlinked_origin_resolves_under_root(self, client, auth_headers, pinned_roots, tmp_path, monkeypatch):
        real = tmp_path / "real-checkout"
        real.mkdir()
        module_name = "linkprov"
        (real / f"{module_name}.py").write_text(
            "class LinkProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def sync(self, since=None):\n"
            "        return iter(())\n"
        )
        (pinned_roots / f"{module_name}.py").symlink_to(real / f"{module_name}.py")
        response = _register(
            client, auth_headers,
            _payload(module_name, **{"class": "LinkProvider"}),
        )
        assert response.status_code == 400  # realpath origin is outside the root

    def test_runtime_recheck_marks_run_failed(self, client, auth_headers, dummy_module, tmp_path, monkeypatch):
        # Register while unpinned, then tighten the roots: the run must refuse.
        monkeypatch.delenv("MEMOGARDEN_PROVIDER_ROOTS", raising=False)
        monkeypatch.delenv("MEMOGARDEN_DATA_DIR", raising=False)
        _register(client, auth_headers, _payload(dummy_module))

        roots = tmp_path / "tightened-providers"
        roots.mkdir()
        monkeypatch.setenv("MEMOGARDEN_PROVIDER_ROOTS", str(roots))

        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        manifest = _wait_for_finish(client, auth_headers, "testprov")
        assert manifest["last_status"] == "failed"

        from system.core import _create_connection

        conn = _create_connection()
        try:
            row = conn.execute(
                "SELECT detail FROM provider_runs WHERE id = ?",
                (manifest["last_run_id"],),
            ).fetchone()
            assert "provider roots" in json.loads(row["detail"])["error"]
        finally:
            conn.close()


class TestScopePolicy:
    """RFC-011 §4.8 (item 4): providers label at creation per the
    operator's scope policy — rules-based, default catch-all → triage."""

    @pytest.fixture
    def triage_uuid(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Triage / Untrusted"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        return response.get_json()["result"]["uuid"]

    @pytest.fixture
    def finances_uuid(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Finances"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        return response.get_json()["result"]["uuid"]

    def _plain(self, prefixed):
        return prefixed.split("core_", 1)[-1]

    def _notes(self, client, auth_headers):
        query = client.post(
            "/mg",
            json={"op": "query", "target_type": "fact", "type": "Note"},
            headers=auth_headers,
        )
        assert query.status_code == 200, query.get_json()
        return {r["data"]["description"]: r for r in query.get_json()["result"]["results"]}

    def test_default_policy_routes_to_triage(
        self, client, auth_headers, dummy_module, triage_uuid
    ):
        """No policy configured → the ratified default: everything → triage."""
        assert _register(client, auth_headers, _payload(dummy_module)).status_code == 201
        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert _wait_for_finish(client, auth_headers, "testprov")["last_status"] == "success"

        notes = self._notes(client, auth_headers)
        assert len(notes) == 2
        for note in notes.values():
            assert note["metadata"]["scopes"] == [self._plain(triage_uuid)]

    def test_rule_routes_match_and_default_rest(
        self, client, auth_headers, dummy_module, triage_uuid, finances_uuid
    ):
        assert _register(client, auth_headers, _payload(dummy_module)).status_code == 201
        put = client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={
                "rules": [
                    {"match": {"description": "dummy fact 0"},
                     "scope": finances_uuid}
                ],
                "default": "triage",
            },
            headers=auth_headers,
        )
        assert put.status_code == 200
        assert put.get_json()["scope_policy"]["rules"][0]["scope"] == finances_uuid

        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert _wait_for_finish(client, auth_headers, "testprov")["last_status"] == "success"

        notes = self._notes(client, auth_headers)
        assert notes["dummy fact 0"]["metadata"]["scopes"] == [self._plain(finances_uuid)]
        assert notes["dummy fact 1"]["metadata"]["scopes"] == [self._plain(triage_uuid)]

    def test_null_scope_and_null_default_deliver_unscoped(
        self, client, auth_headers, dummy_module
    ):
        assert _register(client, auth_headers, _payload(dummy_module)).status_code == 201
        assert client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={"rules": [{"match": {"description": "dummy fact 0"}, "scope": None}],
                  "default": None},
            headers=auth_headers,
        ).status_code == 200

        client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert _wait_for_finish(client, auth_headers, "testprov")["last_status"] == "success"

        notes = self._notes(client, auth_headers)
        assert all("scopes" not in n["metadata"] or not n["metadata"]["scopes"]
                   for n in notes.values())

    def test_policy_endpoint_requires_jwt(self, client, auth_headers, auth_headers_apikey, dummy_module):
        assert _register(client, auth_headers, _payload(dummy_module)).status_code == 201
        response = client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={"default": "triage"},
            headers=auth_headers_apikey,
        )
        assert response.status_code == 401

    def test_unit_provider_set_scopes_win_and_rules_apply(self):
        from api.provider_runtime import _apply_scope_policy
        from system.soil import Fact

        def fact(metadata=None, data=None):
            return Fact(uuid="u", _type="Note", realized_at="t", canonical_at="t",
                        data=data or {}, metadata=metadata or {}, fidelity="full")

        # Provider-set label wins over the policy
        f = fact(metadata={"scopes": ["preset"]})
        _apply_scope_policy(f, {"default": "triage"}, lambda ref: "resolved")
        assert f.metadata["scopes"] == ["preset"]

        # Null default = unscoped (metadata untouched)
        f = fact()
        _apply_scope_policy(f, {"default": None}, lambda ref: "resolved")
        assert "scopes" not in f.metadata

        # Default resolves through the resolver
        f = fact()
        _apply_scope_policy(f, {"default": "triage"}, lambda ref: "triage-uuid")
        assert f.metadata["scopes"] == ["triage-uuid"]

        # Rule match on a data field wins over the default
        f = fact(data={"from": "bank@example.com"})
        policy = {"rules": [{"match": {"from": "bank@example.com"},
                             "scope": "finances-uuid"}],
                  "default": "triage"}
        _apply_scope_policy(f, policy, lambda ref: {"finances-uuid": "finances-uuid",
                                                    "triage": "triage-uuid"}[ref])
        assert f.metadata["scopes"] == ["finances-uuid"]

    def test_unit_inherit_parent_scopes(self, client):
        # `client` boots the app so the fixture data dir has initialized
        # soil/core tables — this unit test reads them directly.
        """RFC-012 §4.9 addendum (2026-11-21 operator decision): yielded
        facts carrying metadata.email_uuid inherit the parent email's live
        scopes at extraction — a late-extracted attachment of an
        already-filed email lands filed instead of stranding in triage as
        an orphan. Undecided/unscoped parents fall through to the scope
        policy (fail-closed unchanged); provider labels still win."""
        from api.provider_runtime import _inherit_parent_scopes
        from system.soil import Fact, generate_soil_uuid, get_soil

        def attachment(metadata):
            return Fact(uuid="u", _type="Attachment", realized_at="t", canonical_at="t",
                        data={"filename": "x.pdf"}, metadata=dict(metadata), fidelity="full")

        with get_soil() as soil:
            conn = soil._get_connection()
            parent = soil.create_fact(Fact(
                uuid=generate_soil_uuid(), _type="Email", realized_at="t", canonical_at="t",
                data={"title": "filed email"}, metadata={"scopes": ["finances-uuid"]},
                fidelity="full"))
            conn.commit()

            # Parent's scopes inherited (prefixed email_uuid ref)
            f = attachment({"email_uuid": parent})
            _inherit_parent_scopes(f, soil)
            assert f.metadata["scopes"] == ["finances-uuid"]

            # Bare parent ref resolves too (uuid form defense, RFC-010 gotcha)
            f = attachment({"email_uuid": parent.removeprefix("soil_")})
            _inherit_parent_scopes(f, soil)
            assert f.metadata["scopes"] == ["finances-uuid"]

            # Provider-set label wins over inheritance
            f = attachment({"email_uuid": parent, "scopes": ["preset"]})
            _inherit_parent_scopes(f, soil)
            assert f.metadata["scopes"] == ["preset"]

            # Parent without scopes -> untouched; the policy applies next
            unscoped = soil.create_fact(Fact(
                uuid=generate_soil_uuid(), _type="Email", realized_at="t", canonical_at="t",
                data={"title": "unscoped email"}, metadata={"scopes": []},
                fidelity="full"))
            conn.commit()
            f = attachment({"email_uuid": unscoped})
            _inherit_parent_scopes(f, soil)
            assert "scopes" not in f.metadata

            # Missing parent row -> no-op
            f = attachment({"email_uuid": "soil_does_not_exist"})
            _inherit_parent_scopes(f, soil)
            assert "scopes" not in f.metadata
