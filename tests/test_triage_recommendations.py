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
from utils import isodatetime, uid

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


def test_item_inspector(client, auth_headers, core):
    """GET /api/v1/items/<uuid> — live fact + attachment family (facts only)."""
    from system.soil.relation import SystemRelation
    parent = _prefixed(_labeled_fact("inspector parent", []))
    att = _prefixed(_labeled_fact("inspector att", []))
    with get_soil() as soil:
        soil._get_connection().execute(
            "INSERT INTO system_relation (uuid, kind, source, source_type, "
            "target, target_type, created_at) "
            "VALUES (?, 'contained_by', ?, 'item', ?, 'item', 0)",
            (generate_soil_uuid(), att, parent))
        soil._get_connection().commit()
    resp = client.get(f"/api/v1/items/{parent}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["item"]["uuid"] == parent
    assert [a["uuid"] for a in body["attachments"]] == [att]

    resp = client.get("/api/v1/items/core_deadbeef-0000-0000-0000-000000000000",
                      headers=auth_headers)
    assert resp.status_code == 404
    resp = client.get("/api/v1/items/soil_does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_item_inspector_requires_jwt(client, core):
    resp = client.get("/api/v1/items/soil_0012d580-90f5-4436-9453-1be1543ffe0c")
    assert resp.status_code == 401


def _make_scope_entity(core, label):
    from api.v1 import operator as om  # noqa: F401 — same module pattern
    uuid = core.entity.create(entity_type="Scope",
        data={"label": label, "active_participants": [], "artifact_uuids": []})
    core._conn.commit()
    return uuid


def test_items_search_recent_and_filters(client, auth_headers, core):
    """GET /items/search — recent browse mode, type + scope filters (no q)."""
    hh = _make_scope(core, "Household")
    triage = _make_scope(core, "Triage / Untrusted")
    with get_soil() as soil:
        soil.create_fact(Fact(uuid=generate_soil_uuid(), _type="Email",
            realized_at="2026-11-22T09:00:00+00:00",
            canonical_at="2026-11-22T09:00:00+00:00",
            data={"title": "alpha email", "description": None, "summary": None},
            metadata={"scopes": [triage]}, integrity_hash=None, fidelity="full"))
        soil.create_fact(Fact(uuid=generate_soil_uuid(), _type="Attachment",
            realized_at="2026-11-22T08:00:00+00:00",
            canonical_at="2026-11-22T08:00:00+00:00",
            data={"filename": "beta file"}, metadata={"scopes": [hh]},
            integrity_hash=None, fidelity="full"))
    resp = client.get("/api/v1/items/search", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "recent"
    assert [r["title"] for r in body["results"]][:2] == ["alpha email", "beta file"]
    resp = client.get("/api/v1/items/search?type=Attachment", headers=auth_headers)
    assert all(r["type"] == "Attachment" for r in resp.get_json()["results"])
    resp = client.get(f"/api/v1/items/search?scope={hh}", headers=auth_headers)
    assert all("beta" in r["title"] for r in resp.get_json()["results"])
    resp = client.get("/api/v1/items/search?scope=Household", headers=auth_headers)
    assert all("beta" in r["title"] for r in resp.get_json()["results"])


def test_items_search_lexical_when_daemon_absent(client, auth_headers, core, monkeypatch, tmp_path):
    """Semantic mode degrades to the lexical FTS plane (BM25) when the
    daemon socket is dead (api#95 Phase 1)."""
    triage = _make_scope(core, "Triage / Untrusted")
    with get_soil() as soil:
        soil.create_fact(Fact(uuid=generate_soil_uuid(), _type="Email",
            realized_at="2026-11-22T09:00:00+00:00",
            canonical_at="2026-11-22T09:00:00+00:00",
            data={"title": "xylophone receipt", "description": None, "summary": None},
            metadata={"scopes": [triage]}, integrity_hash=None, fidelity="full"))
    # api#95 Phase 2: the daemon client reads these env vars at call time;
    # pointing both at tmp_path makes the plane unavailable instantly (no
    # model layer to spawn) without touching the real triage-model dir.
    monkeypatch.setenv("MEMOGARDEN_EMBED_SOCK", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("MEMOGARDEN_TRIAGE_MODEL_DIR", str(tmp_path))
    resp = client.get("/api/v1/items/search?q=xylophone", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    # hybrid default degrades honestly to the lexical plane
    assert body["mode"] == "lexical"
    assert body["results"] and body["results"][0]["title"] == "xylophone receipt"


def test_items_search_keyword_fallback_cjk_substring(client, auth_headers, core, monkeypatch, tmp_path):
    """unicode61 does not tokenize CJK runs, so the lexical plane has no
    hits — zero-hit queries fall through to the narrow LIKE path."""
    with get_soil() as soil:
        soil.create_fact(Fact(uuid=generate_soil_uuid(), _type="Email",
            realized_at="2026-11-23T09:00:00+00:00",
            canonical_at="2026-11-23T09:00:00+00:00",
            data={"title": "东京银行对账单", "description": None, "summary": None},
            metadata=None, integrity_hash=None, fidelity="full"))
    monkeypatch.setenv("MEMOGARDEN_EMBED_SOCK", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("MEMOGARDEN_TRIAGE_MODEL_DIR", str(tmp_path))
    resp = client.get("/api/v1/items/search?q=银行", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "keyword_fallback"
    assert body["results"] and body["results"][0]["title"] == "东京银行对账单"


def test_items_search_requires_jwt(client):
    resp = client.get("/api/v1/items/search")
    assert resp.status_code == 401


def test_items_search_hybrid_default_fuses_planes(client, auth_headers,
                                                  fake_daemon_factory):
    """api#95 Phase 2: with a query and no explicit mode, hybrid fuses the
    lexical plane (BM25) with the semantic plane (daemon cosine) — a hit
    known only to the daemon still surfaces, and the badge says hybrid."""
    with get_soil() as soil:
        lex_uuid = soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Note",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={"title": "zephyr glider notes",
                  "description": "zephyr winds and launch checklist",
                  "summary": None},
            metadata=None, integrity_hash=None, fidelity="full"))
        sem_uuid = soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Note",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={"title": "sailplane wing repair",
                  "description": "fiberglass patch on the outer wing",
                  "summary": None},
            metadata=None, integrity_hash=None, fidelity="full"))
    fake_daemon_factory([[sem_uuid, 0.81]])
    resp = client.get("/api/v1/items/search?q=zephyr", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "hybrid"
    uuids = [r["uuid"] for r in body["results"]]
    assert uid.add_soil_prefix(lex_uuid) in uuids, "lexical hit lost"
    assert uid.add_soil_prefix(sem_uuid) in uuids, "semantic-only hit lost"
    scores = {r["uuid"]: r["score"] for r in body["results"]}
    assert scores[uid.add_soil_prefix(lex_uuid)] == pytest.approx(
        1 / 61, abs=1e-3)


def test_items_search_unknown_mode_rejected(client, auth_headers):
    resp = client.get("/api/v1/items/search?q=x&mode=psychic",
                      headers=auth_headers)
    assert resp.status_code == 400


def _triage_fact_at(title: str, triage: str, canonical_at: str) -> str:
    """Triage fact with a pinned canonical time so recent order is known."""
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(), _type="Email",
            realized_at=canonical_at, canonical_at=canonical_at,
            data={"title": title, "description": None, "summary": None},
            metadata={"scopes": [triage]}, integrity_hash=None,
            fidelity="full"))


def test_triage_explore_sort_orders_by_nn_dist(client, auth_headers, core, recs_env):
    """?sort=explore ranks facts farthest-from-decided first (nn_dist desc
    from the sidecar), ties/missing last in canonical-DESC order."""
    triage = _make_scope(core, "Triage / Untrusted")
    old = _prefixed(_triage_fact_at("old lonely", triage, "2026-11-20T08:00:00+00:00"))
    mid = _prefixed(_triage_fact_at("mid lonely", triage, "2026-11-21T08:00:00+00:00"))
    near = _prefixed(_triage_fact_at("near decided", triage, "2026-11-22T08:00:00+00:00"))
    recs_env({
        near: {"pred": "unclassified", "confidence": 0.9, "runner": "t",
               "nn_dist": 0.11},
        old: {"pred": "Household", "confidence": 0.6, "runner": "t",
              "nn_dist": 0.43},
        mid: {"pred": "Household", "confidence": 0.6, "runner": "t",
              "nn_dist": 0.43},  # tie with old -> canonical DESC keeps mid first
    })
    resp = client.get("/api/v1/triage?sort=explore", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sort"] == "explore"
    assert [f["uuid"] for f in body["facts"]] == [mid, old, near]
    # nn_dist rides through on the rec payload for the console d= chip
    assert body["facts"][0]["rec"]["nn_dist"] == 0.43


def test_triage_explore_sort_rows_without_rec_sort_last(client, auth_headers, core, recs_env):
    triage = _make_scope(core, "Triage / Untrusted")
    covered = _prefixed(_triage_fact_at("covered", triage, "2026-11-22T08:00:00+00:00"))
    orphan = _prefixed(_triage_fact_at("no rec yet", triage, "2026-11-23T08:00:00+00:00"))
    recs_env({covered: {"pred": "unclassified", "confidence": 0.8,
                        "runner": "t", "nn_dist": 0.2}})
    resp = client.get("/api/v1/triage?sort=explore", headers=auth_headers)
    body = resp.get_json()
    assert [f["uuid"] for f in body["facts"]] == [covered, orphan]


def test_triage_explore_sort_falls_back_without_signal(client, auth_headers, core, recs_env):
    """A sidecar predating nn_dist must not scramble the queue: explore
    falls back to recent and says so."""
    triage = _make_scope(core, "Triage / Untrusted")
    older = _prefixed(_triage_fact_at("older", triage, "2026-11-20T08:00:00+00:00"))
    newer = _prefixed(_triage_fact_at("newer", triage, "2026-11-23T08:00:00+00:00"))
    recs_env({older: {"pred": "unclassified", "confidence": 0.8, "runner": "t"}})
    resp = client.get("/api/v1/triage?sort=explore", headers=auth_headers)
    body = resp.get_json()
    assert body["sort"] == "recent"
    assert [f["uuid"] for f in body["facts"]] == [newer, older]


def test_triage_recent_is_default_and_unknown_sort_rejected(client, auth_headers, core, recs_env):
    triage = _make_scope(core, "Triage / Untrusted")
    older = _prefixed(_triage_fact_at("older", triage, "2026-11-20T08:00:00+00:00"))
    newer = _prefixed(_triage_fact_at("newer", triage, "2026-11-23T08:00:00+00:00"))
    recs_env({newer: {"pred": "unclassified", "confidence": 0.8,
                      "runner": "t", "nn_dist": 0.05}})
    resp = client.get("/api/v1/triage", headers=auth_headers)
    body = resp.get_json()
    assert body["sort"] == "recent"
    assert [f["uuid"] for f in body["facts"]] == [newer, older]
    resp = client.get("/api/v1/triage?sort=psychic", headers=auth_headers)
    assert resp.status_code == 400
