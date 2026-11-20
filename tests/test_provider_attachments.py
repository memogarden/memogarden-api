"""Attachment facts through the provider runtime (RFC-012 §4.9).

Since 2026-11-20 gmail's attachments flow through sync() as runtime dicts
(the rss/sessions pattern), so they pass _to_fact → _apply_scope_policy →
create_fact like every other fact — this pins that seam: an Attachment
yield is stored, scope-stamped per the effective (account-over-provider)
policy, and linked contained_by via the provider's create_relations hook.
"""

import time
import uuid

import pytest


ATTACHY_MODULE_TEMPLATE = '''
from datetime import datetime, timezone

from system.soil import get_soil
from system.soil.fact import Fact, generate_soil_uuid


class {class_name}:
    """Yields an Attachment for a parent fact it creates in __init__ —
    mirroring gmail, whose extractor reads the already-stored parent email's
    uuid from Soil."""

    PLATFORMS = ["google"]

    def __init__(self, config=None, account=None):
        self.config = config or {{}}
        self.account_ctx = account
        now = datetime.now(timezone.utc).isoformat()
        owner = account["subject"] if account else (config or {{}}).get("account")
        with get_soil() as soil:
            fact = Fact(
                uuid=generate_soil_uuid(), _type="Note",
                realized_at=now, canonical_at=now,
                data={{"description": "parent of the attachment"}},
                metadata={{"account": owner}}, integrity_hash=None,
                fidelity="full",
            )
            self.parent_uuid = soil.create_fact(fact)

    def sync(self, since=None):
        now = datetime.now(timezone.utc).isoformat()
        yield {{
            "type": "Attachment",
            "data": {{
                "filename": "report.pdf", "mime_type": "application/pdf",
                "size": 3, "sha256": "abc", "title": "report.pdf",
                "content_path": "ab/cd/ef.pdf",
            }},
            "metadata": {{
                "email_uuid": self.parent_uuid,
                "account": (self.account_ctx or {{}}).get("subject"),
            }},
            "canonical_at": now,
            "realized_at": now,
        }}

    def create_relations(self, fact):
        if (getattr(fact, "_type", "") or "") != "Attachment":
            return []
        parent = (fact.metadata or {{}}).get("email_uuid")
        if not parent:
            return []
        from system.soil import generate_soil_uuid
        from system.soil.relation import SystemRelation
        return [SystemRelation(
            uuid=generate_soil_uuid(), kind="contained_by",
            source=fact.uuid, source_type="item", target=parent,
            target_type="item", created_at=2000, evidence=None, metadata=None,
        )]
'''


@pytest.fixture
def attachy_module(tmp_path, monkeypatch):
    module_name = f"attachy_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        ATTACHY_MODULE_TEMPLATE.format(class_name="AttachyProvider")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _register(client, auth_headers, payload):
    return client.post("/api/v1/providers", json=payload, headers=auth_headers)


def _payload(module, **overrides):
    payload = {
        "name": "testprov",
        "module": module,
        "class": "AttachyProvider",
        "interval": 3600,
        "config": {},
    }
    payload.update(overrides)
    return payload


def _run_and_wait(client, auth_headers, name):
    assert client.post(f"/api/v1/providers/{name}/run", headers=auth_headers).status_code == 202
    deadline = time.time() + 5.0
    while time.time() < deadline:
        body = client.get(f"/api/v1/providers/{name}", headers=auth_headers).get_json()
        if body.get("last_status") in ("success", "failed"):
            assert body["last_status"] == "success", body.get("last_error")
            return body
        time.sleep(0.05)
    raise AssertionError(f"provider run for {name} did not finish within 5s")


@pytest.fixture
def triage_scoped(client, auth_headers):
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope", "data": {"label": "Triage / Untrusted"}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    # fact metadata stores bare uuids; the /mg entity uuid carries core_
    return response.get_json()["result"]["uuid"].split("core_", 1)[-1]


def _attachments(client, auth_headers):
    query = client.post(
        "/mg",
        json={"op": "query", "target_type": "fact", "type": "Attachment"},
        headers=auth_headers,
    )
    assert query.status_code == 200, query.get_json()
    results = query.get_json()["result"]["results"]
    assert len(results) == 1
    return results[0]


class TestAttachmentsThroughRuntime:
    def test_attachment_stored_and_contained_by_parent(
        self, client, auth_headers, attachy_module
    ):
        assert _register(
            client, auth_headers, _payload(attachy_module)
        ).status_code == 201
        _run_and_wait(client, auth_headers, "testprov")

        attachment = _attachments(client, auth_headers)
        assert attachment["data"]["filename"] == "report.pdf"
        parent = attachment["metadata"]["email_uuid"]
        assert parent

        from system.soil import get_soil

        with get_soil() as soil:
            rows = soil._get_connection().execute(
                "SELECT source, target FROM system_relation WHERE kind = 'contained_by'"
            ).fetchall()
        bare = attachment["uuid"]
        assert [(r["source"], r["target"]) for r in rows] == [(bare, parent)]

    def test_attachment_gets_provider_scope_policy_stamp(
        self, client, auth_headers, attachy_module, triage_scoped
    ):
        assert _register(
            client, auth_headers, _payload(attachy_module)
        ).status_code == 201
        assert client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={"rules": [], "default": "triage"},
            headers=auth_headers,
        ).status_code == 200
        _run_and_wait(client, auth_headers, "testprov")

        attachment = _attachments(client, auth_headers)
        assert attachment["metadata"]["scopes"] == [triage_scoped]

    def test_account_policy_replaces_provider_stamp_for_attachments(
        self, client, auth_headers, attachy_module
    ):
        """The NYJC case at the seam level: the account override stamps the
        account's scope; the provider-level triage default does not apply."""
        assert _register(
            client, auth_headers, _payload(attachy_module)
        ).status_code == 201
        put = client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={"rules": [], "default": "triage"},
            headers=auth_headers,
        )
        assert put.status_code == 200

        scope = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "NYJC"}},
            headers=auth_headers,
        ).get_json()["result"]["uuid"]

        account = client.post(
            "/api/v1/providers/testprov/accounts",
            json={"platform": "google", "subject": "a@gmail.com"},
            headers=auth_headers,
        ).get_json()
        assert client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"scope_policy": {"rules": [], "default": scope}},
            headers=auth_headers,
        ).status_code == 200

        run = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/run",
            headers=auth_headers,
        )
        assert run.status_code == 202
        deadline = time.time() + 5.0
        while time.time() < deadline:
            body = client.get(
                f"/api/v1/providers/testprov/accounts/{account['id']}",
                headers=auth_headers,
            ).get_json()
            if body.get("last_status") in ("success", "failed"):
                assert body["last_status"] == "success", body.get("last_error")
                break
            time.sleep(0.05)
        else:
            raise AssertionError("account run did not finish")

        attachment = _attachments(client, auth_headers)
        assert attachment["metadata"]["scopes"] == [scope.split("core_", 1)[-1]]
