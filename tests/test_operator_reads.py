"""
Tests for operator-only console reads (RFC-015 §3.3).

- GET /api/v1/triage            — scope queue (facts + entities)
- GET /api/v1/scopes/overview   — scopes, counts, grants, policies
- GET /api/v1/events/pending-review — occurrence review queue

All three are JWT-only: they render authority state (grants, policies)
and the sovereign operator's full view, and RFC-011 keeps boundary
configuration out of API-key hands even in read form.
"""

import json
from datetime import UTC, datetime, timedelta

from system.soil import Fact, generate_soil_uuid, get_soil
from system.soil.relation import SystemRelation
from utils import isodatetime


def _make_scope(core, label: str, visibility: str | None = None) -> str:
    data = {"label": label, "active_participants": [], "artifact_uuids": []}
    if visibility is not None:
        data["visibility"] = visibility
    uuid = core.entity.create(
        entity_type="Scope",
        data=data,
    )
    core._conn.commit()
    return uuid


def _labeled_fact(title: str, scope_uuids: list, account: str | None = None) -> str:
    metadata = {"scopes": list(scope_uuids)}
    if account:
        metadata["account"] = account
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Note",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={"title": title, "description": None, "summary": None},
            metadata=metadata,
            integrity_hash=None,
            fidelity="full",
        ))


def _unlabeled_fact(title: str) -> str:
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Note",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={"title": title, "description": None, "summary": None},
            metadata=None,
            integrity_hash=None,
            fidelity="full",
        ))


def _fact(_type: str, data: dict, metadata: dict | None) -> str:
    """Generic soil fact helper (emails, attachments, notes, ...)."""
    with get_soil() as soil:
        return soil.create_fact(Fact(
            uuid=generate_soil_uuid(), _type=_type,
            realized_at=isodatetime.now(), canonical_at=isodatetime.now(),
            data=data, metadata=metadata, integrity_hash=None, fidelity="full",
        ))


def _make_event(core, title: str, starts: str, ends: str | None,
                occurred_at: str | None = None) -> str:
    data = {"title": title, "starts_at": starts, "status": "confirmed",
            "active_participants": [], "artifact_uuids": []}
    if ends:
        data["ends_at"] = ends
    if occurred_at:
        data["occurred_at"] = occurred_at
    uuid = core.entity.create(entity_type="Event", data=data)
    core._conn.commit()
    if occurred_at:
        # Mirror the crash-free mark-occurred state: the Event fact is present.
        with get_soil() as soil:
            soil.create_fact(Fact(
                uuid=generate_soil_uuid(),
                _type="Event",
                realized_at=isodatetime.now(),
                canonical_at=occurred_at,
                data={"event_uuid": "core_" + uuid, "kind": "event"},
                metadata={"status": "occurred", "resolved_at": occurred_at},
                integrity_hash=None,
                fidelity="full",
            ))
    return uuid


# ============================================================================
# GET /api/v1/triage
# ============================================================================


def test_triage_lists_labeled_only(client, auth_headers, core):
    triage = _make_scope(core, "Triage / Untrusted")
    finances = _make_scope(core, "Finances")
    _labeled_fact("triaged email", [triage])
    _labeled_fact("other scope", [finances])
    _unlabeled_fact("global note")

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    titles = [f["data"]["title"] for f in data["facts"]]
    assert titles == ["triaged email"]
    assert data["total_facts"] == 1
    assert data["scope"]["data"]["label"] == "Triage / Untrusted"


def test_triage_account_filter(client, auth_headers, core):
    triage = _make_scope(core, "Triage / Untrusted")
    _labeled_fact("uob pdf", [triage], account="UOB")
    _labeled_fact("gmail email", [triage], account="personal")

    response = client.get("/api/v1/triage?account=UOB", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert [f["data"]["title"] for f in data["facts"]] == ["uob pdf"]


def test_triage_entities_labeled(client, auth_headers, core):
    triage = _make_scope(core, "Triage / Untrusted")
    core.entity.create(entity_type="Event",
                       data={"title": "unmarked", "starts_at": "2026-11-01",
                             "status": "confirmed", "scopes": [triage],
                             "active_participants": [], "artifact_uuids": []})
    core._conn.commit()
    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["total_entities"] == 1
    assert data["entities"][0]["data"]["title"] == "unmarked"


def test_triage_entities_canonical_ordering(client, auth_headers, core):
    """Canonical ordering (RFC-015 §3.3): entities sort+expose when the
    thing happened (starts_at/start_at), not when the import batch landed —
    a 21k gcal backfill shares one updated_at minute, which made the
    queue meaningless. Facts expose canonical_at alongside realized_at.
    """
    triage = _make_scope(core, "Triage / Untrusted")
    core.entity.create(entity_type="Event",
                       data={"title": "old occurrence", "starts_at": "2025-01-31T08:30:00+00:00",
                             "status": "confirmed", "scopes": [triage],
                             "active_participants": [], "artifact_uuids": []})
    core.entity.create(entity_type="Task",
                       data={"title": "recent task", "start_at": "2026-11-20T00:00:00+00:00",
                             "status": "confirmed", "scopes": [triage],
                             "active_participants": [], "artifact_uuids": []})
    core._conn.commit()

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    entities = data["entities"]
    assert [e["data"]["title"] for e in entities] == ["recent task", "old occurrence"]
    assert entities[1]["canonical_at"] == "2025-01-31T08:30:00+00:00"

    # Facts carry canonical_at too (gmail backfills: realized == import,
    # canonical == when the message was actually received).
    _labeled_fact("canonical fact", [triage])
    response = client.get("/api/v1/triage", headers=auth_headers)
    fact = response.get_json()["facts"][0]
    assert "canonical_at" in fact and fact["canonical_at"]


def test_triage_reports_containment(client, auth_headers, core):
    """Families: tada-style children (metadata.task_source_id -> parent
    data.source_id) map to their in-window parent; unlinked items are
    absent from the map."""
    triage = _make_scope(core, "Triage / Untrusted")
    def _fact(_type, data, metadata):
        with get_soil() as soil:
            return soil.create_fact(Fact(
                uuid=generate_soil_uuid(), _type=_type,
                realized_at=isodatetime.now(), canonical_at=isodatetime.now(),
                data=data, metadata=metadata, integrity_hash=None, fidelity="full",
            ))
    parent = _fact("Event", {"source_id": "tada-1", "title": "parent task", "kind": "task"}, {"scopes": [triage]})
    child = _fact("Note", {"description": "child note"}, {"scopes": [triage], "task_source_id": "tada-1"})
    # Orphan: parent exists (live, UNSCOPED — filed elsewhere) but is not in
    # the triage set, so the pair cannot be in-window; the endpoint resolves
    # a stub so the console can caption the child.
    parent2 = _fact("Event", {"source_id": "tada-2", "title": "filed parent", "kind": "task"}, {"scopes": []})
    child2 = _fact("Note", {"description": "orphan note"}, {"scopes": [triage], "task_source_id": "tada-2"})
    _labeled_fact("standalone", [triage])

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    # generate_soil_uuid() returns already-prefixed uuids; the endpoint
    # passes them through prefixed exactly once.
    assert data["containment"]["child_parent"] == {child: parent}
    stubs = data["containment"]["parent_stubs"]
    assert stubs[child2]["uuid"] == parent2
    assert stubs[child2]["title"] == "filed parent"
    assert stubs[child2]["scopes"] == []


def _soil_relation(child: str, parent: str) -> None:
    """Soil contained_by edge — the store provider create_relations hooks
    actually write (gmail attachments -> emails, session links)."""
    with get_soil() as soil:
        soil.create_relation(SystemRelation(
            uuid=generate_soil_uuid(), kind="contained_by",
            source=child, target=parent,
            source_type="item", target_type="item",
            created_at=0, evidence=None, metadata=None,
        ))


def test_triage_families_soil_contained_by(client, auth_headers, core):
    """Attachment -> email families group via soil system_relation (the
    console previously only read core user_relation, which has no rows, so
    gmail families never rendered)."""
    triage = _make_scope(core, "Triage / Untrusted")
    parent = _fact("Email", {"title": "thread parent"}, {"scopes": [triage], "account": "gmail"})
    child = _fact("Attachment", {"filename": "invoice.pdf"}, {"scopes": [triage], "email_uuid": parent})
    _soil_relation(child, parent)

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["containment"]["child_parent"] == {child: parent}


def test_triage_families_follow_supersedes_multi_hop(client, auth_headers, core):
    """system#11 semantics: an amended email's attachment keeps parented to
    the version it was captured for — the family must resolve through the
    supersedes chain (here two hops) to the LIVE parent version."""
    triage = _make_scope(core, "Triage / Untrusted")
    old_parent = _fact("Email", {"title": "v1"}, {"scopes": [triage], "account": "gmail"})
    child = _fact("Attachment", {"filename": "invoice.pdf"}, {"scopes": [triage], "email_uuid": old_parent})
    _soil_relation(child, old_parent)
    mid_parent = _fact("Email", {"title": "v2"}, {"scopes": [triage], "account": "gmail"})
    with get_soil() as soil:
        soil.mark_superseded(old_parent, mid_parent, isodatetime.now())
    newest_parent = _fact("Email", {"title": "v3"}, {"scopes": [triage], "account": "gmail"})
    with get_soil() as soil:
        soil.mark_superseded(mid_parent, newest_parent, isodatetime.now())

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["containment"]["child_parent"] == {child: newest_parent}


def test_triage_orphan_stub_soil_relation(client, auth_headers, core):
    """Attachment whose parent is not in the window (filed elsewhere /
    unscoped) gets a stub resolved via the soil relation, so the console
    captions the orphan instead of showing a mystery row."""
    triage = _make_scope(core, "Triage / Untrusted")
    parent = _fact("Email", {"title": "filed elsewhere"}, {"scopes": []})
    child = _fact("Attachment", {"filename": "invoice.pdf"}, {"scopes": [triage], "email_uuid": parent})
    _soil_relation(child, parent)

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    stubs = data["containment"]["parent_stubs"]
    assert stubs[child]["uuid"] == parent
    assert stubs[child]["title"] == "filed elsewhere"
    assert stubs[child]["scopes"] == []
    assert child not in data["containment"]["child_parent"]


def test_triage_orphan_stub_follows_supersedes(client, auth_headers, core):
    """The stub path resolves the target's supersedes chain too — this exact
    lookup previously used the bare uuid form and could never match soil's
    prefixed item rows."""
    triage = _make_scope(core, "Triage / Untrusted")
    old_parent = _fact("Email", {"title": "v1"}, {"scopes": []})
    child = _fact("Attachment", {"filename": "invoice.pdf"},
                  {"scopes": [triage], "email_uuid": old_parent})
    _soil_relation(child, old_parent)
    mid_parent = _fact("Email", {"title": "v2"}, {"scopes": []})
    with get_soil() as soil:
        soil.mark_superseded(old_parent, mid_parent, isodatetime.now())
    newest_parent = _fact("Email", {"title": "v3"}, {"scopes": []})
    with get_soil() as soil:
        soil.mark_superseded(mid_parent, newest_parent, isodatetime.now())

    response = client.get("/api/v1/triage", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    stub = data["containment"]["parent_stubs"][child]
    assert stub["uuid"] == newest_parent
    assert stub["title"] == "v3"
    assert stub["scopes"] == []


def test_triage_unknown_scope_400(client, auth_headers):
    response = client.get("/api/v1/triage?scope=nonexistent-scope", headers=auth_headers)
    assert response.status_code == 400


def test_triage_requires_jwt(client, auth_headers_apikey):
    response = client.get(
        "/api/v1/triage",
        headers={"X-API-Key": auth_headers_apikey["X-API-Key"]},
    )
    assert response.status_code == 401


# ============================================================================
# GET /api/v1/scopes/overview
# ============================================================================


def test_scopes_overview_shape(client, auth_headers, core):
    triage = _make_scope(core, "Triage / Untrusted")
    _labeled_fact("a", [triage])
    _labeled_fact("b", [triage])

    response = client.get("/api/v1/scopes/overview", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["enforcement"] is False  # conftest env: flag off
    triage_entry = next(s for s in data["scopes"] if s["label"] == "Triage / Untrusted")
    assert triage_entry["fact_count"] == 2
    assert isinstance(data["grants"], list)
    assert isinstance(data["provider_policies"], dict)


def test_scopes_overview_exposes_visibility(client, auth_headers, core):
    """RFC-011 visibility addendum: every scope row carries its visibility.

    Public rides through as stored; scopes predating the attribute default
    to private (the migration stamps private, this is the defensive read).
    """
    _make_scope(core, "Quiet", visibility="private")
    _make_scope(core, "Household", visibility="public")
    _make_scope(core, "Legacy")  # no attribute → defaults private

    response = client.get("/api/v1/scopes/overview", headers=auth_headers)
    assert response.status_code == 200
    scopes = {s["label"]: s for s in response.get_json()["scopes"]}
    assert scopes["Household"]["visibility"] == "public"
    assert scopes["Quiet"]["visibility"] == "private"
    assert scopes["Legacy"]["visibility"] == "private"


def test_scopes_overview_includes_provider_policy(client, auth_headers, core):
    core._conn.execute(
        """INSERT INTO providers
           (name, module, class, interval, enabled, config, capabilities,
            scope_policy, registered_at)
           VALUES (?, 'm', 'C', 3600, 1, '{}', '[]', ?, ?)""",
        ("somemail", json.dumps({"default": "exempt"}), "2026-11-19T00:00:00+00:00"),
    )
    core._conn.commit()
    response = client.get("/api/v1/scopes/overview", headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()["provider_policies"]["somemail"] == {"default": "exempt"}


def test_scopes_overview_requires_jwt(client, auth_headers_apikey):
    response = client.get(
        "/api/v1/scopes/overview",
        headers={"X-API-Key": auth_headers_apikey["X-API-Key"]},
    )
    assert response.status_code == 401


# ============================================================================
# GET /api/v1/events/pending-review
# ============================================================================


def _past_iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_pending_review_queue(client, auth_headers, core):
    marked = _make_event(core, "happened", _past_iso(3), _past_iso(3),
                         occurred_at=_past_iso(3))
    _make_event(core, "unmarked recent", _past_iso(2), _past_iso(2))
    _make_event(core, "date-only past",
                (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d"), None)
    _make_event(core, "in the future", (datetime.now(UTC) + timedelta(days=5)).isoformat(),
                (datetime.now(UTC) + timedelta(days=5)).isoformat())

    response = client.get("/api/v1/events/pending-review", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    uuids = [e["uuid"] for e in data["events"]]
    assert "core_" + marked not in uuids  # already marked → excluded
    titles = [e["title"] for e in data["events"]]
    assert "unmarked recent" in titles
    assert "date-only past" in titles   # date-only past sorts before now
    assert "in the future" not in titles
    assert data["total_unmarked"] == len(data["events"])


def test_pending_review_window_excludes_old(client, auth_headers, core):
    _make_event(core, "ancient", _past_iso(200), _past_iso(200))
    _make_event(core, "recent", _past_iso(2), _past_iso(2))

    response = client.get("/api/v1/events/pending-review", headers=auth_headers)
    assert response.status_code == 200
    titles = [e["title"] for e in response.get_json()["events"]]
    assert titles == ["recent"]


def test_pending_review_requires_jwt(client, auth_headers_apikey):
    response = client.get(
        "/api/v1/events/pending-review",
        headers={"X-API-Key": auth_headers_apikey["X-API-Key"]},
    )
    assert response.status_code == 401


# ============================================================================
# Console pages (HTML shells)
# ============================================================================


def test_console_pages_render(client):
    """Console shells are public HTML — auth happens client-side (RFC-015 §4)."""
    for path, marker in [
        ("/console", "Triage"),
        ("/console/triage", "Triage review"),
        ("/console/scopes", "Scopes"),
        ("/console/identity", "Agents"),
        ("/console/providers", "Providers"),
        ("/console/occurrences", "Occurrence review"),
        ("/console/audit", "Action audit"),
        ("/console/settings", "Settings"),
    ]:
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200, path
        assert marker in response.get_data(as_text=True), path


def test_legacy_ui_pages_redirect_into_console(client):
    """2026-11-19 consolidation: the pre-RFC-010 pages are retired and the
    console absorbed their functionality — old URLs get a soft landing
    (302) instead of a 404. The JSON endpoints behind bare /agents and
    /api-keys/ are untouched (RFC-010/RFC-015 contracts)."""
    for path, target in [
        ("/api-keys", "/console/identity"),
        ("/api-keys/new", "/console/identity"),
        ("/agents/", "/console/identity"),
        ("/settings", "/console/settings"),
        ("/", "/console"),
    ]:
        response = client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"].endswith(target), path


def test_sup_map_cache_ttls_and_rekeys(monkeypatch):
    """_load_sup_map caches per soil db path within the TTL (RFC-015 perf).

    The triage endpoint used to rescan all superseded rows per request
    (67k+ in production). The cache must: hit within TTL, refresh past it,
    and re-key immediately when the soil db path changes (test fixtures
    each carry their own database — and a deployment move too).
    """
    import api.v1.operator as operator_mod

    loads = {"n": 0}
    results = [
        [("u1", "u2")],   # first load
        [("u1", "u3")],   # post-TTL reload must see new rows
    ]
    state = {"db": "/tmp/soilA.db"}
    clock = {"t": 100.0}

    class _FakeSoil:
        db_path = property(lambda self: state["db"])

        def _get_connection(self):
            outer = self

            class _Conn:
                def execute(self, _sql):
                    outer_exec = self

                    class _Cur:
                        def fetchall(self):
                            rows = results[min(loads["n"], len(results) - 1)]
                            loads["n"] += 1
                            return [{"uuid": u, "superseded_by": s} for u, s in rows]
                    return _Cur()
            return _Conn()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(operator_mod, "get_soil", lambda *a, **kw: _FakeSoil())
    monkeypatch.setattr(operator_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        operator_mod, "_sup_map_cache",
        {"key": None, "at": -operator_mod._SUP_MAP_TTL, "map": {}},
    )

    # Cold load
    assert operator_mod._load_sup_map() == {"u1": "u2"}
    assert loads["n"] == 1

    # Within TTL: warm hit, no new query
    clock["t"] += 10
    assert operator_mod._load_sup_map() == {"u1": "u2"}
    assert loads["n"] == 1

    # Past TTL: reload sees the new supersedes rows
    clock["t"] += 31
    assert operator_mod._load_sup_map() == {"u1": "u3"}
    assert loads["n"] == 2

    # DB path change re-keys immediately, even within TTL
    clock["t"] += 1
    state["db"] = "/tmp/soilB.db"
    operator_mod._load_sup_map()
    assert loads["n"] == 3
