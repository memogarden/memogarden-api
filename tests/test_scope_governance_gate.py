"""
Tests for the Scope-governance gate (RFC-011 D1 extended; RFC-015 review).

Scope minting was already API-key-proof (scope_mint_denied); this pins the
sibling gap found in the RFC-015 review: a Scope's label IS the boundary
vocabulary, so edit and forget require the same operator authority. Both
gates are unconditional (minting's is not flag-gated either).
"""

import json


def _make_scope(core, label="Triage / Untrusted") -> str:
    uuid = core.entity.create(
        entity_type="Scope",
        data={"label": label, "active_participants": [], "artifact_uuids": []},
    )
    core._conn.commit()
    return uuid


def _mg(client, headers, payload):
    return client.post(
        "/mg",
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers,
    )


def test_api_key_cannot_edit_scope_label(client, auth_headers_apikey, core):
    """The review's finding: any key could rewrite a scope's label."""
    scope = _make_scope(core)
    response = _mg(client, {"X-API-Key": auth_headers_apikey["X-API-Key"]}, {
        "op": "edit", "target": "core_" + scope, "set": {"label": "Not Triage"},
    })
    assert response.status_code == 403
    assert response.get_json()["error"]["details"]["code"] == "scope_governance_denied"

    # Label unchanged
    row = core._conn.execute(
        "SELECT json_extract(data, '$.label') AS label FROM entity WHERE uuid = ?",
        (scope,),
    ).fetchone()
    assert row["label"] == "Triage / Untrusted"


def test_operator_can_edit_scope_label(client, auth_headers, core):
    scope = _make_scope(core, "Finances")
    response = _mg(client, auth_headers, {
        "op": "edit", "target": "core_" + scope, "set": {"label": "Finances 2026"},
    })
    assert response.status_code == 200
    row = core._conn.execute(
        "SELECT json_extract(data, '$.label') AS label FROM entity WHERE uuid = ?",
        (scope,),
    ).fetchone()
    assert row["label"] == "Finances 2026"


def test_api_key_cannot_forget_scope(client, auth_headers_apikey, core):
    scope = _make_scope(core)
    response = _mg(client, {"X-API-Key": auth_headers_apikey["X-API-Key"]}, {
        "op": "forget", "target": "core_" + scope,
    })
    assert response.status_code == 403
    assert response.get_json()["error"]["details"]["code"] == "scope_governance_denied"

    row = core._conn.execute(
        "SELECT superseded_by FROM entity WHERE uuid = ?", (scope,)
    ).fetchone()
    assert row["superseded_by"] is None


def test_operator_can_forget_scope(client, auth_headers, core):
    scope = _make_scope(core, "OldScope")
    response = _mg(client, auth_headers, {
        "op": "forget", "target": "core_" + scope,
    })
    assert response.status_code == 200
    row = core._conn.execute(
        "SELECT superseded_by FROM entity WHERE uuid = ?", (scope,)
    ).fetchone()
    assert row["superseded_by"] is not None


def test_non_scope_edits_still_open_to_keys(client, auth_headers_apikey, core):
    """The gate is Scope-scoped — ordinary entities keep working for keys."""
    artifact = core.entity.create(entity_type="Artifact", data={"label": "x"})
    core._conn.commit()
    response = _mg(client, {"X-API-Key": auth_headers_apikey["X-API-Key"]}, {
        "op": "edit", "target": "core_" + artifact, "set": {"label": "y"},
    })
    assert response.status_code == 200
