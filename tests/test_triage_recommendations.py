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

from api.v1 import operator as operator_mod
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime, uid


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


def test_items_search_hybrid_rerank_reorders(client, auth_headers,
                                             fake_daemon_factory):
    """?rerank=1 on the default hybrid mode: the fused candidates get
    cross-encoder scores and the badge gains the +rerank marker."""
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
    fake_daemon_factory([[sem_uuid, 0.81]],
                        rerank_results=[[sem_uuid, 7.0], [lex_uuid, 0.5]])
    resp = client.get("/api/v1/items/search?q=zephyr&rerank=1",
                      headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "hybrid+rerank"
    uuids = [r["uuid"] for r in body["results"]]
    assert uuids[:2] == [uid.add_soil_prefix(sem_uuid),
                         uid.add_soil_prefix(lex_uuid)]
    assert body["results"][0]["score"] == pytest.approx(7.0)


def test_items_search_semantic_rerank_reorders(client, auth_headers,
                                               fake_daemon_factory):
    """semantic mode + ?rerank=1: daemon cosine order is replaced by the
    cross-encoder's, badge gains the marker."""
    a = _triage_fact_at("alpha ledger note", "open", isodatetime.now())
    b = _triage_fact_at("beta ledger note", "open", isodatetime.now())
    fake_daemon_factory([[a, 0.9], [b, 0.5]],
                        rerank_results=[[b, 6.0], [a, -0.5]])
    resp = client.get(
        "/api/v1/items/search?q=ledger&mode=semantic&rerank=1",
        headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["mode"] == "semantic+rerank"
    uuids = [r["uuid"] for r in body["results"]]
    assert uuids == [uid.add_soil_prefix(b), uid.add_soil_prefix(a)]
    assert body["results"][0]["score"] == pytest.approx(6.0)


# --- console paging + live re-check (api#96, api#91) -----------------------

def _search_note(title: str, _type: str = "Note") -> str:
    """A plain fact of the given type, for the search paging/re-check tests."""
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(), _type=_type,
            realized_at=isodatetime.now(), canonical_at=isodatetime.now(),
            data={"title": title, "description": None, "summary": None},
            metadata=None, integrity_hash=None, fidelity="full"))


def _walk_pages(client, auth_headers, query: str, step: int = 20):
    """Page a search the way the console does (offset += rows returned)
    until a short page; returns (uuids in order, per-page sizes)."""
    seen, sizes, offset = [], [], 0
    for _ in range(15):
        body = client.get(
            f"/api/v1/items/search?{query}&limit={step}&offset={offset}",
            headers=auth_headers).get_json()
        uuids = [r["uuid"] for r in body["results"]]
        seen.extend(uuids)
        sizes.append(len(uuids))
        if len(uuids) < step:
            break
        offset += len(uuids)
    return seen, sizes


def _two_channel_corpus():
    """60 lexical-matching notes + 60 daemon-only notes."""
    lex = [_search_note(f"zephyr glider note {i}") for i in range(60)]
    sem = [_search_note(f"sailplane wing repair {i}") for i in range(60)]
    return lex, sem


def test_items_search_hybrid_pages_honor_limit(client, auth_headers,
                                               fake_daemon_factory):
    """api#96: the default hybrid mode pages like every other mode — no page
    longer than `limit`, no row on two pages, the walk ends on a short page.
    It used to return the whole fused set on every call, so the console's
    "Load more" re-appended page one forever."""
    lex, sem = _two_channel_corpus()
    fake_daemon_factory([[u, 0.81 - i * 0.001] for i, u in enumerate(sem)])
    seen, sizes = _walk_pages(client, auth_headers, "q=zephyr")
    assert max(sizes) == 20
    assert sizes[-1] < 20
    assert len(seen) == len(set(seen)), "a row came back on two pages"
    # every daemon hit plus the lexical plane's fetch floor (50 of 60)
    reachable = {uid.add_soil_prefix(u) for u in sem + lex}
    assert len(seen) == 110 and set(seen) <= reachable
    assert {uid.add_soil_prefix(u) for u in sem} <= set(seen)


def test_items_search_hybrid_rerank_still_pages(client, auth_headers,
                                                fake_daemon_factory):
    """?rerank=1 reorders the candidate head, then the same paging applies."""
    lex, sem = _two_channel_corpus()
    fake_daemon_factory([[u, 0.5] for u in sem],
                        rerank_results=[[sem[3], 9.0], [lex[0], 1.0]])
    first = client.get("/api/v1/items/search?q=zephyr&limit=20&rerank=1",
                       headers=auth_headers).get_json()
    assert first["mode"] == "hybrid+rerank"
    assert len(first["results"]) == 20
    assert first["results"][0]["uuid"] == uid.add_soil_prefix(sem[3])
    seen, sizes = _walk_pages(client, auth_headers, "q=zephyr&rerank=1")
    assert max(sizes) == 20 and sizes[-1] < 20
    assert len(seen) == len(set(seen))
    assert len(seen) == 110


def test_items_search_drops_hits_of_the_wrong_live_type(client, auth_headers,
                                                        fake_daemon_factory):
    """api#91: daemon hits are re-checked against live rows for TYPE as well
    as scope — the index's build-time type is only a hint, so a row the
    daemon returns under a type filter it no longer satisfies is dropped."""
    keep = _search_note("ledger note alpha", _type="Note")
    stale = _search_note("ledger mail beta", _type="Email")
    fake_daemon_factory([[stale, 0.9], [keep, 0.5]])
    for mode in ("semantic", "hybrid"):
        body = client.get(
            f"/api/v1/items/search?q=ledger&mode={mode}&type=Note",
            headers=auth_headers).get_json()
        assert [r["uuid"] for r in body["results"]] == [
            uid.add_soil_prefix(keep)], mode


def test_items_search_drops_hits_relabelled_out_of_the_scope_filter(
        client, auth_headers, fake_daemon_factory, core):
    """api#91: scope is re-checked against live rows — an item relabelled
    out of the requested scope no longer surfaces under it, even though the
    daemon's build-time metadata still ranks it there."""
    hh = _make_scope(core, "Household")
    ns = _make_scope(core, "NS")
    relabelled = _labeled_fact("insurance claim doc", [ns])
    kept = _labeled_fact("insurance renewal home", [hh])
    fake_daemon_factory([[relabelled, 0.9], [kept, 0.5]])
    body = client.get(
        f"/api/v1/items/search?q=insurance&mode=semantic"
        f"&scope={uid.add_core_prefix(hh)}",
        headers=auth_headers).get_json()
    assert [r["uuid"] for r in body["results"]] == [
        uid.add_soil_prefix(kept)]


def _get_search(client, auth_headers, query):
    return client.get(f"/api/v1/items/search?{query}",
                      headers=auth_headers).get_json()


def test_items_search_reports_how_far_the_index_is_behind(
        client, auth_headers, fake_daemon_factory, tmp_path, monkeypatch):
    """api#91: a page the semantic plane shaped says how many indexed-type
    items changed since the index's last catch-up (it only catches up when
    the embed daemon wakes). Audit facts never count — the index does not
    cover them — and pages no semantic plane touched carry no figure."""
    target = _search_note("ledger note alpha")
    fake_daemon_factory([[target, 0.9]])
    cache = tmp_path / "idx"
    cache.mkdir()
    monkeypatch.setenv("MEMOGARDEN_SEARCH_CACHE", str(cache))
    state = cache / "index_state.json"

    # no index on disk → nothing to report
    assert "index_lag" not in _get_search(client, auth_headers, "q=ledger&mode=semantic")

    # an index caught up to the change feed is current
    with get_soil() as soil:
        state.write_text(json.dumps({"watermark": soil.max_change_seq()}))
    shaped = [_get_search(client, auth_headers, "q=ledger&mode=semantic"),
              _get_search(client, auth_headers, "q=ledger")]
    assert [b["mode"] for b in shaped] == ["semantic", "hybrid"]
    assert [b["index_lag"] for b in shaped] == [0, 0]

    # an audited request writes Action facts — not indexed types, ignored
    audited = client.post(
        "/mg", json={"op": "query", "target_type": "fact", "type": "Note"},
        headers=auth_headers)
    assert audited.status_code == 200
    assert _get_search(client, auth_headers, "q=ledger&mode=semantic")["index_lag"] == 0

    # an indexed-type item changes after the watermark → one behind
    _search_note("later ledger note")
    assert _get_search(client, auth_headers, "q=ledger&mode=semantic")["index_lag"] == 1

    for plain in ("limit=5", "q=ledger&mode=keyword", "q=ledger&mode=lexical"):
        assert "index_lag" not in _get_search(client, auth_headers, plain), plain


def test_items_search_tells_the_console_where_the_next_page_starts(
        client, auth_headers):
    """Every mode answers with `next_offset` and `has_more`. The SQL-paged
    modes advance by the rows they returned."""
    for i in range(5):
        _search_note(f"ledger note {i}")
    first = _get_search(client, auth_headers, "limit=2")
    assert (len(first["results"]), first["has_more"], first["next_offset"]) == (2, True, 2)
    second = _get_search(client, auth_headers, "limit=2&offset=2")
    assert (second["next_offset"], second["has_more"]) == (4, True)
    last = _get_search(client, auth_headers, "limit=2&offset=4")
    assert (len(last["results"]), last["next_offset"], last["has_more"]) == (1, 5, False)


def _stale_and_live(client, auth_headers):
    """40 live notes plus 12 the index still ranks first but which have
    been superseded since it was built — the normal state after any edit,
    until the embed daemon wakes."""
    live = [_search_note(f"sailplane repair note {i}") for i in range(40)]
    stale = [_search_note(f"sailplane repair old {i}") for i in range(12)]
    for old in stale:
        amended = client.post(
            "/mg",
            json={"op": "amend", "target": old,
                  "data": {"title": "renamed away", "description": None,
                           "summary": None}},
            headers=auth_headers,
        )
        assert amended.status_code == 200, amended.get_json()
    return live, stale


def test_items_search_fills_a_page_past_stale_hits(
        client, auth_headers, fake_daemon_factory):
    """api#91: a candidate the live re-check drops must not shorten a page
    — the console reads a short page as the end of results. The walk keeps
    going until the page is full; `offset`/`next_offset` are positions in
    the candidate list, not counts of returned rows."""
    live, stale = _stale_and_live(client, auth_headers)
    fake_daemon_factory(
        [[u, 0.9 - i * 0.001] for i, u in enumerate(stale + live)])

    pages, offset, more = [], 0, True
    while more and len(pages) < 10:
        body = _get_search(
            client, auth_headers,
            f"q=sailplane&mode=semantic&limit=20&offset={offset}")
        pages.append(body)
        offset, more = body["next_offset"], body["has_more"]

    assert [p["mode"] for p in pages] == ["semantic"] * 2
    assert [len(p["results"]) for p in pages] == [20, 20]
    assert [p["next_offset"] for p in pages] == [32, 52]
    assert [p["has_more"] for p in pages] == [True, False]
    seen = [r["uuid"] for p in pages for r in p["results"]]
    assert seen == live, "every live row exactly once, stale ones never"


def test_items_search_hybrid_pages_are_never_shortened_by_stale_hits(
        client, auth_headers, fake_daemon_factory):
    """The default hybrid mode under the same conditions: every page but
    the last is full, and the walk yields each live row exactly once."""
    live, stale = _stale_and_live(client, auth_headers)
    fake_daemon_factory([[u, 0.5] for u in stale + live])

    sizes, seen, offset, more = [], [], 0, True
    while more and len(sizes) < 10:
        body = _get_search(
            client, auth_headers, f"q=sailplane&limit=20&offset={offset}")
        assert body["mode"] == "hybrid"
        sizes.append(len(body["results"]))
        seen.extend(r["uuid"] for r in body["results"])
        offset, more = body["next_offset"], body["has_more"]

    assert sizes[:-1] == [20] * (len(sizes) - 1) and sizes[-1] <= 20
    assert len(seen) == len(set(seen)) and set(seen) == set(live)


def test_items_search_hybrid_degraded_to_semantic_still_pages(
        client, auth_headers, fake_daemon_factory):
    """No lexical hit for the query, so a hybrid request degrades to the
    semantic plane alone — and that page honors `offset` too."""
    notes = [_search_note(f"sailplane repair {i}") for i in range(60)]
    fake_daemon_factory([[u, 0.8 - i * 0.001] for i, u in enumerate(notes)])

    pages = [_get_search(client, auth_headers,
                         f"q=zephyr&limit=20&offset={o}") for o in (0, 20, 40)]
    assert {p["mode"] for p in pages} == {"semantic"}
    assert [len(p["results"]) for p in pages] == [20, 20, 20]
    assert [p["next_offset"] for p in pages] == [20, 40, 60]
    assert [p["has_more"] for p in pages] == [True, True, False]
    seen = [r["uuid"] for p in pages for r in p["results"]]
    assert seen == notes


def test_items_search_rerank_skips_pages_beyond_the_candidate_head(
        client, auth_headers, fake_daemon_factory):
    """+rerank only permutes the first _RERANK_CANDIDATES. A page wholly
    beyond them is served without the cross-encoder (about a second of CPU
    on the single sync worker) and in exactly the un-reranked order."""
    lex, sem = _two_channel_corpus()
    fake_daemon_factory([[u, 0.5] for u in sem],
                        rerank_results=[[sem[3], 9.0], [lex[0], 1.0]])

    def rerank_calls():
        return sum(1 for req in fake_daemon_factory.seen_requests
                   if req.get("op") == "rerank")

    head = _get_search(client, auth_headers, "q=zephyr&limit=20&rerank=1")
    assert head["mode"] == "hybrid+rerank" and rerank_calls() == 1

    fake_daemon_factory.seen_requests.clear()
    tail = _get_search(client, auth_headers,
                       "q=zephyr&limit=20&offset=30&rerank=1")
    assert tail["mode"] == "hybrid" and rerank_calls() == 0
    plain = _get_search(client, auth_headers, "q=zephyr&limit=20&offset=30")
    assert [r["uuid"] for r in tail["results"]] == [r["uuid"] for r in plain["results"]]


def test_items_search_clamps_an_oversized_offset(client, auth_headers):
    """A huge offset used to overflow SQLite in the SQL-paged modes (a
    500); it now clamps to an empty page."""
    for i in range(3):
        _search_note(f"ledger note {i}")
    for query in ("limit=5", "q=ledger&mode=keyword", "q=ledger&mode=lexical",
                  "q=ledger&mode=semantic"):
        response = client.get(
            f"/api/v1/items/search?{query}&offset={10 ** 30}",
            headers=auth_headers)
        assert response.status_code == 200, query
        body = response.get_json()
        assert body["results"] == [] and body["has_more"] is False, query


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
