"""Decision-model recommendations in the console (RFC-015 §3.3 extension).

- GET /api/v1/triage attaches `rec` badges from the recommendations sidecar
  (MEMOGARDEN_TRIAGE_RECS override; absence = escalate = no badge)
- GET /api/v1/triage/eval joins operator amend Actions (the decision record)
  with stored recommendations — first n non-escalated decisions
- Both are JWT-only like the rest of the operator console reads
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime

from api.v1 import operator as operator_mod


def _make_scope(core, label: str) -> str:
    uuid = core.entity.create(
        entity_type="Scope",
        data={"label": label, "active_participants": [], "artifact_uuids": []},
    )
    core._conn.commit()
    return uuid


def _labeled_fact(title: str, scope_uuids: list) -> str:
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Note",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={"title": title, "description": None, "summary": None},
            metadata={"scopes": list(scope_uuids)},
            integrity_hash=None,
            fidelity="full",
        ))


def _prefixed(uuid: str) -> str:
    return uuid if uuid.startswith("soil_") else f"soil_{uuid}"


def _operator_amend(target: str, scopes: list) -> None:
    with get_soil() as soil:
        soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Action",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={
                "actor": "admin", "actor_type": "operator",
                "operation": "amend",
                "params": {"target": target, "metadata": {"scopes": scopes}},
            },
            metadata={},
            integrity_hash=None,
            fidelity="full",
        ))


@pytest.fixture()
def recs_env(tmp_path, monkeypatch):
    def _set(recs: dict, generated_at=None) -> str:
        path = tmp_path / "recommendations.json"
        path.write_text(json.dumps({
            "model": "test-model",
            "generated_at": generated_at or (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "tau": 0.7,
            "recs": recs,
        }))
        monkeypatch.setenv("MEMOGARDEN_TRIAGE_RECS", str(path))
        operator_mod._RECS_CACHE.update({"path": None, "mtime": None, "doc": {}})
        return path
    return _set


def test_triage_attaches_rec_badges(client, auth_headers, core, recs_env):
    triage = _make_scope(core, "Triage / Untrusted")
    hh = _make_scope(core, "Household")
    fact = _prefixed(_labeled_fact("rec target", [triage]))
    other = _prefixed(_labeled_fact("no rec", [triage]))
    recs_env({fact: {"pred": "Household", "confidence": 0.93, "runner": "hh:test"}})
    resp = client.get("/api/v1/triage", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    by_uuid = {f["uuid"]: f for f in body["facts"]}
    assert by_uuid[fact]["rec"]["pred"] == "Household"
    assert by_uuid[fact]["rec"]["confidence"] == 0.93
    assert "rec" not in by_uuid[other]  # escalate = absent
    assert body["decision_model"]["model"] == "test-model"


def test_triage_eval_scores_operator_decisions(client, auth_headers, core, recs_env):
    triage = _make_scope(core, "Triage / Untrusted")
    hh = _make_scope(core, "Household")
    ns = _make_scope(core, "NS")
    f_agree = _prefixed(_labeled_fact("agree case", [triage]))
    f_wrong = _prefixed(_labeled_fact("wrong case", [triage]))
    f_escalated = _prefixed(_labeled_fact("escalated case", [triage]))
    recs_env({
        f_agree: {"pred": "Household", "confidence": 0.9, "runner": "t"},
        f_wrong: {"pred": "NS", "confidence": 0.8, "runner": "t"},
        # f_escalated deliberately absent
    })

    f_soft = _prefixed(_labeled_fact("soft case", [triage]))
    recs_env({
        f_agree: {"pred": "Household", "confidence": 0.9, "runner": "t"},
        f_wrong: {"pred": "NS", "confidence": 0.8, "runner": "t"},
        f_soft: {"pred": "Household", "confidence": 0.45, "runner": "t",
                 "soft": True},
    })

    _operator_amend(f_agree, [f"core_{hh}"])   # agree
    _operator_amend(f_wrong, [f"core_{hh}"])   # override to Household -> disagree
    _operator_amend(f_soft, [f"core_{hh}"])    # decided despite soft lean
    _operator_amend(f_escalated, [])           # cleared, no rec -> excluded

    resp = client.get("/api/v1/triage/eval?n=100", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["n"] == 2            # escalated + soft-lean items excluded
    assert body["correct"] == 1
    assert body["agreement"] == 0.5
    entries = {e["uuid"]: e for e in body["decisions"]}
    assert entries[f_agree]["agree"] is True
    assert entries[f_agree]["decided"] == "Household"
    assert entries[f_wrong]["agree"] is False


def test_triage_eval_cleared_counts_as_unclassified(client, auth_headers, core, recs_env):
    triage = _make_scope(core, "Triage / Untrusted")
    hh = _make_scope(core, "Household")
    f = _prefixed(_labeled_fact("cleared case", [triage]))
    recs_env({f: {"pred": "unclassified", "confidence": 0.75, "runner": "t"}})
    _operator_amend(f, [])  # → Public = explicit unscoped decision
    resp = client.get("/api/v1/triage/eval", headers=auth_headers)
    body = resp.get_json()
    assert body["n"] == 1
    assert body["decisions"][0]["decided"] == "unclassified"
    assert body["agreement"] == 1.0


def test_triage_eval_without_sidecar(client, auth_headers, core, recs_env):
    recs_env({}, generated_at=None)
    resp = client.get("/api/v1/triage/eval", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["state"] == "no recommendations file"
    assert body["n"] == 0


def test_triage_without_sidecar_has_no_rec_key(client, auth_headers, core, monkeypatch, tmp_path):
    triage = _make_scope(core, "Triage / Untrusted")
    monkeypatch.setenv("MEMOGARDEN_TRIAGE_RECS", str(tmp_path / "missing.json"))
    operator_mod._RECS_CACHE.update({"path": None, "mtime": None, "doc": {}})
    _labeled_fact("plain", [triage])
    resp = client.get("/api/v1/triage", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert all("rec" not in f for f in body["facts"])
    assert "decision_model" not in body


def test_triage_eval_requires_jwt(client, core, recs_env):
    """The eval endpoint renders decision-model state — JWT-only like /triage."""
    recs_env({"soil_x": {"pred": "Household", "confidence": 0.9, "runner": "t"}})
    resp = client.get("/api/v1/triage/eval")
    assert resp.status_code == 401
