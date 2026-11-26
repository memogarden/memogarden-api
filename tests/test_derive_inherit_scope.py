"""RFC-011 §4.6 / SG-9 — a derived fact inherits its source's scope labels.

A fact that declares `metadata.derived_from` copies the source's CURRENT
labels at creation. Default-on: MEMOGARDEN_DERIVE_INHERIT_SCOPE=off is the
operator's escape hatch. Without the rule, text extracted from a scoped
document lands under the provider policy's default — triage, or unscoped —
and a triage pass can file it to Public: a plaintext side-channel around
the whole model.

Two lanes, one rule. The provider lane copies verbatim (providers are not
principals, SG-7). The API lane narrows the labels to what the requester
holds grants for under enforcement, so inheritance can never write into a
scope the requester cannot enter.
"""

import time

import pytest

from utils import uid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plain(prefixed: str) -> str:
    """Fact labels store bare uuids; /mg entity uuids carry core_."""
    return prefixed.split("core_", 1)[-1]


def _triage(client, auth_headers) -> str:
    """The triage scope — a label like any other to SG-9. The test database
    does not seed the builtins, so mint it under its real label."""
    return _plain(_make_scope(client, auth_headers, "Triage / Untrusted"))


def _make_scope(client, auth_headers, label):
    response = client.post(
        "/mg",
        json={"op": "create", "type": "Scope", "data": {"label": label}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.get_json()["result"]["uuid"]


@pytest.fixture
def scope_uuid(client, auth_headers):
    return _make_scope(client, auth_headers, "Finances")


@pytest.fixture
def enforcement(monkeypatch):
    """RFC-011 §8: flip the enforcement flag for the requesting test."""
    monkeypatch.setenv("MEMOGARDEN_SCOPE_ENFORCEMENT", "1")


def _add(client, headers, data, metadata=None, _type="Note"):
    payload = {"op": "add", "type": _type, "data": data}
    if metadata is not None:
        payload["metadata"] = metadata
    response = client.post("/mg", json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]


def _document(client, auth_headers, labels=()):
    """A source document carrying the given labels. (A baseline type: the
    add verb does not take Attachment — providers create those.)"""
    metadata = {"scopes": list(labels)} if labels else None
    return _add(
        client, auth_headers,
        {"subject": "statement.pdf", "description": "page text"},
        metadata, _type="Email",
    )


def _extract(client, headers, source_uuid, metadata=None):
    """A fact derived from `source_uuid` — the extraction shape."""
    merged = {"derived_from": source_uuid}
    merged.update(metadata or {})
    return _add(
        client, headers,
        {"title": "extracted text", "description": "page 1"},
        merged,
    )


def _labels(fact) -> list | None:
    return (fact.get("metadata") or {}).get("scopes")


@pytest.fixture
def agent_key(client, auth_headers):
    """A bound agent key — a genuinely separate principal (RFC-010)."""
    agent_response = client.post(
        "/agents", json={"label": "extractor-agent"}, headers=auth_headers
    )
    assert agent_response.status_code == 201
    agent_id = agent_response.get_json()["agent_id"]
    key_response = client.post(
        "/api-keys/",
        json={"name": "extractor-key", "agent_id": agent_id},
        headers=auth_headers,
    )
    key = key_response.get_json()
    return {
        "headers": {"X-API-Key": key["key"], "Content-Type": "application/json"},
        "agent_id": agent_id,
    }


def _grant_and_enter(client, auth_headers, agent_key, scope_uuid):
    granted = client.post(
        "/grants",
        json={"agent_id": agent_key["agent_id"], "scope_uuid": scope_uuid},
        headers=auth_headers,
    )
    assert granted.status_code == 201, granted.get_json()
    entered = client.post(
        "/mg", json={"op": "enter", "scope": scope_uuid},
        headers=agent_key["headers"],
    )
    assert entered.status_code == 200, entered.get_json()


# ============================================================================
# API lane
# ============================================================================

class TestDerivedInheritsApiLane:
    """`add` with metadata.derived_from — advisory mode (flag off)."""

    def test_derived_fact_copies_the_source_labels(
            self, client, auth_headers, scope_uuid):
        source = _document(client, auth_headers, [scope_uuid])
        derived = _extract(client, auth_headers, source["uuid"])
        assert _labels(derived) == [_plain(scope_uuid)]

    def test_bare_and_prefixed_references_both_resolve(
            self, client, auth_headers, scope_uuid):
        source = _document(client, auth_headers, [scope_uuid])
        bare = source["uuid"].split("soil_", 1)[-1]
        for ref in (bare, f"soil_{bare}"):
            derived = _extract(client, auth_headers, ref)
            assert _labels(derived) == [_plain(scope_uuid)], ref

    def test_explicit_labels_win_over_inheritance(self, client, auth_headers):
        source_scope = _make_scope(client, auth_headers, "Finances")
        target_scope = _make_scope(client, auth_headers, "Household")
        source = _document(client, auth_headers, [source_scope])
        derived = _extract(client, auth_headers, source["uuid"],
                           metadata={"scopes": [target_scope]})
        assert _labels(derived) == [_plain(target_scope)]

    def test_unlabeled_source_falls_through_to_the_creation_default(
            self, client, auth_headers, scope_uuid):
        unscoped_document = _document(client, auth_headers)
        # creator unframed → unscoped, like its source
        derived = _extract(client, auth_headers, unscoped_document["uuid"])
        assert _labels(derived) is None

        # creator inside a scope → the ordinary §4.6 default applies
        assert client.post(
            "/mg", json={"op": "enter", "scope": scope_uuid}, headers=auth_headers
        ).status_code == 200
        derived = _extract(client, auth_headers, unscoped_document["uuid"])
        assert _labels(derived) == [_plain(scope_uuid)]

    def test_superseded_source_resolves_to_the_live_decision(
            self, client, auth_headers):
        """A relabel is a new fact version. An extractor still holding the
        OLD uuid must land on the operator's decision, not the stale label
        the old row keeps."""
        old_scope = _make_scope(client, auth_headers, "Triage")
        new_scope = _make_scope(client, auth_headers, "Finances")
        source = _document(client, auth_headers, [old_scope])
        amended = client.post(
            "/mg",
            json={"op": "amend", "target": source["uuid"],
                  "data": source["data"],
                  "metadata": {"scopes": [new_scope]}},
            headers=auth_headers,
        )
        assert amended.status_code == 200, amended.get_json()
        assert amended.get_json()["result"]["uuid"] != source["uuid"]

        derived = _extract(client, auth_headers, source["uuid"])
        assert _labels(derived) == [_plain(new_scope)]

    def test_missing_source_is_ignored(self, client, auth_headers):
        derived = _extract(client, auth_headers, f"soil_{uid.generate_uuid()}")
        assert _labels(derived) is None

    def test_switch_off_is_the_escape_hatch(
            self, client, auth_headers, scope_uuid, monkeypatch):
        monkeypatch.setenv("MEMOGARDEN_DERIVE_INHERIT_SCOPE", "off")
        source = _document(client, auth_headers, [scope_uuid])
        derived = _extract(client, auth_headers, source["uuid"])
        assert _labels(derived) is None


# ============================================================================
# API lane under enforcement
# ============================================================================

class TestDerivedInheritsUnderEnforcement:
    """Inheritance narrows to the requester's grants (RFC-011 D2) — it must
    never become a way to write into a scope the requester cannot enter."""

    def test_member_inherits_the_source_scope_not_its_own_frame(
            self, client, auth_headers, agent_key, scope_uuid, enforcement):
        """Granted on both, working inside Household: a fact derived from a
        Finances document lands in Finances — not in the agent's own frame,
        which is what the plain creation default would have given."""
        frame = _make_scope(client, auth_headers, "Household")
        source = _document(client, auth_headers, [scope_uuid])
        _grant_and_enter(client, auth_headers, agent_key, scope_uuid)
        _grant_and_enter(client, auth_headers, agent_key, frame)  # ends here
        derived = _extract(client, agent_key["headers"], source["uuid"])
        assert _labels(derived) == [_plain(scope_uuid)]

    def test_inheritance_narrows_to_the_requesters_grants(
            self, client, auth_headers, agent_key, scope_uuid, enforcement):
        frame = _make_scope(client, auth_headers, "Household")
        ungranted = _make_scope(client, auth_headers, "Personal")
        source = _document(client, auth_headers, [scope_uuid, ungranted])
        _grant_and_enter(client, auth_headers, agent_key, scope_uuid)
        _grant_and_enter(client, auth_headers, agent_key, frame)  # ends here
        derived = _extract(client, agent_key["headers"], source["uuid"])
        # the granted source label only: neither the ungranted one nor the
        # agent's own frame
        assert _labels(derived) == [_plain(scope_uuid)]

    def test_no_write_into_an_ungranted_scope(
            self, client, auth_headers, agent_key, scope_uuid, enforcement):
        source = _document(client, auth_headers, [scope_uuid])
        # unframed: nothing to inherit that it may write
        derived = _extract(client, agent_key["headers"], source["uuid"])
        assert _labels(derived) is None

        # inside a DIFFERENT scope: the creation default, never the source's
        other = _make_scope(client, auth_headers, "Household")
        _grant_and_enter(client, auth_headers, agent_key, other)
        derived = _extract(client, agent_key["headers"], source["uuid"])
        assert _labels(derived) == [_plain(other)]

    def test_sovereign_inherits_every_label(
            self, client, auth_headers, scope_uuid, enforcement):
        other = _make_scope(client, auth_headers, "Household")
        source = _document(client, auth_headers, [scope_uuid, other])
        derived = _extract(client, auth_headers, source["uuid"])
        assert _labels(derived) == [_plain(scope_uuid), _plain(other)]

    def test_an_outsider_cannot_read_the_derived_text(
            self, client, auth_headers, agent_key, scope_uuid, enforcement):
        """The point of the rule, end to end: text derived from a private
        document is itself private — a key with no grant on the source's
        scope is denied the derived fact exactly as it is the source."""
        source = _document(client, auth_headers, [scope_uuid])
        derived = _extract(client, auth_headers, source["uuid"])
        assert _labels(derived) == [_plain(scope_uuid)]

        for target in (source["uuid"], derived["uuid"]):
            denied = client.post(
                "/mg", json={"op": "get", "target": target},
                headers=agent_key["headers"],
            )
            assert denied.status_code == 403, (target, denied.get_json())


# ============================================================================
# Provider lane
# ============================================================================

EXTRACTOR_MODULE_TEMPLATE = '''
from system.soil import get_soil
from system.soil.fact import Fact, generate_soil_uuid
from utils import isodatetime


class {class_name}:
    """Stores a source document in __init__, then yields a fact derived
    from it — the shape an extraction provider has."""

    PLATFORMS = ["test"]

    def __init__(self, config=None, account=None):
        self.config = config or {{}}
        now = isodatetime.now()
        labels = self.config.get("source_scopes") or []
        self.parent_uuid = None
        with get_soil() as soil:
            fact = Fact(
                uuid=generate_soil_uuid(), _type="Attachment",
                realized_at=now, canonical_at=now,
                data={{"filename": "statement.pdf", "description": "page text"}},
                metadata={{"scopes": labels}} if labels else None,
                integrity_hash=None, fidelity="full",
            )
            self.source_uuid = soil.create_fact(fact)
            parent_scopes = self.config.get("parent_scopes")
            if parent_scopes is not None:
                parent = Fact(
                    uuid=generate_soil_uuid(), _type="Email",
                    realized_at=now, canonical_at=now,
                    data={{"subject": "the covering email"}},
                    metadata={{"scopes": parent_scopes}} if parent_scopes else None,
                    integrity_hash=None, fidelity="full",
                )
                self.parent_uuid = soil.create_fact(parent)

    def sync(self, since=None):
        now = isodatetime.now()
        metadata = {{"derived_from": self.source_uuid}}
        if self.parent_uuid:
            metadata["email_uuid"] = self.parent_uuid
        metadata.update(self.config.get("derived_metadata") or {{}})
        yield {{
            "type": "Note",
            "data": {{"title": "extracted text", "description": "page 1"}},
            "metadata": metadata,
            "canonical_at": now,
            "realized_at": now,
        }}
'''


@pytest.fixture
def extractor_module(tmp_path, monkeypatch):
    module_name = f"extractor_provider_{uid.generate_uuid().replace('-', '')[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        EXTRACTOR_MODULE_TEMPLATE.format(class_name="ExtractorProvider")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _policy_default(client, auth_headers, scope_uuid):
    """Route the provider's unattributed facts to `scope_uuid`."""
    response = client.put(
        "/api/v1/providers/extractor/scope-policy",
        json={"rules": [], "default": _plain(scope_uuid)},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.get_json()


def _run_extractor(client, auth_headers, config, policy_scope=None):
    """Register the extractor, optionally give it a catch-all policy, run
    it once, and return the one fact it derived."""
    registered = client.post(
        "/api/v1/providers",
        json={"name": "extractor", "module": config.pop("module"),
              "class": "ExtractorProvider", "interval": 3600, "config": config},
        headers=auth_headers,
    )
    assert registered.status_code == 201, registered.get_json()
    if policy_scope is not None:
        _policy_default(client, auth_headers, policy_scope)
    assert client.post(
        "/api/v1/providers/extractor/run", headers=auth_headers
    ).status_code == 202
    deadline = time.time() + 5.0
    while time.time() < deadline:
        body = client.get(
            "/api/v1/providers/extractor", headers=auth_headers
        ).get_json()
        if body.get("last_status") in ("success", "failed"):
            assert body["last_status"] == "success", body.get("last_error")
            break
        time.sleep(0.05)
    else:
        raise AssertionError("provider run did not finish within 5s")

    query = client.post(
        "/mg", json={"op": "query", "target_type": "fact", "type": "Note"},
        headers=auth_headers,
    )
    assert query.status_code == 200, query.get_json()
    extracted = [r for r in query.get_json()["result"]["results"]
                 if r["data"].get("title") == "extracted text"]
    assert len(extracted) == 1, extracted
    return extracted[0]


class TestDerivedInheritsProviderLane:
    """Providers are not principals (SG-7): the source's labels apply
    verbatim, ahead of both the parent rule and the scope policy."""

    def test_extraction_inherits_the_source_ahead_of_the_policy(
            self, client, auth_headers, extractor_module):
        source_scope = _make_scope(client, auth_headers, "Finances")
        policy_scope = _make_scope(client, auth_headers, "Triage")

        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [_plain(source_scope)],
        }, policy_scope=policy_scope)
        assert _labels(extracted) == [_plain(source_scope)]

    def test_the_source_outranks_the_parent_rule(
            self, client, auth_headers, extractor_module):
        """A fact carrying both a source and a parent reference takes the
        SOURCE's labels. The content came out of the source; the parent
        (a covering email, say) may have been filed elsewhere."""
        source_scope = _make_scope(client, auth_headers, "Finances")
        parent_scope = _make_scope(client, auth_headers, "Household")
        policy_scope = _make_scope(client, auth_headers, "Triage")

        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [_plain(source_scope)],
            "parent_scopes": [_plain(parent_scope)],
        }, policy_scope=policy_scope)
        assert _labels(extracted) == [_plain(source_scope)]

    def test_unlabeled_source_falls_through_to_the_policy(
            self, client, auth_headers, extractor_module, scope_uuid):
        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module, "source_scopes": [],
        }, policy_scope=scope_uuid)
        assert _labels(extracted) == [_plain(scope_uuid)]

    def test_provider_set_labels_still_win(
            self, client, auth_headers, extractor_module):
        source_scope = _make_scope(client, auth_headers, "Finances")
        own_scope = _make_scope(client, auth_headers, "Household")
        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [_plain(source_scope)],
            "derived_metadata": {"scopes": [_plain(own_scope)]},
        })
        assert _labels(extracted) == [_plain(own_scope)]

    def test_switch_off_is_the_escape_hatch(
            self, client, auth_headers, extractor_module, monkeypatch):
        source_scope = _make_scope(client, auth_headers, "Finances")
        policy_scope = _make_scope(client, auth_headers, "Triage")
        monkeypatch.setenv("MEMOGARDEN_DERIVE_INHERIT_SCOPE", "off")

        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [_plain(source_scope)],
        }, policy_scope=policy_scope)
        assert _labels(extracted) == [_plain(policy_scope)]

    def test_extraction_inherits_the_source_under_enforcement(
            self, client, auth_headers, extractor_module, enforcement):
        """Providers are not principals (SG-7): enforcement narrows nothing
        in this lane — the source's labels apply verbatim."""
        source_scope = _make_scope(client, auth_headers, "Finances")
        policy_scope = _make_scope(client, auth_headers, "Household")
        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [_plain(source_scope)],
        }, policy_scope=policy_scope)
        assert _labels(extracted) == [_plain(source_scope)]

    def test_a_triage_labeled_source_is_inherited_like_any_other(
            self, client, auth_headers, extractor_module):
        """A document still waiting in triage is restricted to triage
        members, so its extracted text is too — even when the covering
        email was already filed elsewhere. A derived fact is never more
        visible than its source; the parent rule does not get a say."""
        parent_scope = _make_scope(client, auth_headers, "Household")
        policy_scope = _make_scope(client, auth_headers, "Finances")
        triage = _triage(client, auth_headers)
        extracted = _run_extractor(client, auth_headers, {
            "module": extractor_module,
            "source_scopes": [triage],
            "parent_scopes": [_plain(parent_scope)],
        }, policy_scope=policy_scope)
        assert _labels(extracted) == [triage]
