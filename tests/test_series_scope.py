"""Recurring-series scope semantics (api#94).

Two directions, one belief: a scope decision on one instance of a series
is a decision about the series.

- Apply-time: op:edit on a series member cascades to all live siblings
  (server-side, transactional; cascade_series=false opts out).
- Ingestion-time: new occurrences inherit the series' scopes; re-syncs
  re-inherit instead of drifting back to the policy default (the update
  path strips stored scopes and re-labels by policy — inheritance restores
  the decision).
"""

import json
import time
import uuid

import pytest


# ---------------------------------------------------------------------------
# apply-time cascade (handle_edit)
# ---------------------------------------------------------------------------

def _make_event(core, source_id, series_key=None, ical_uid=None, scopes_key_absent=True):
    """Create a gcal-shaped Event entity; returns the stored uuid (plain)."""
    data = {
        "source_id": source_id,
        "title": f"Birthday {source_id}",
        "starts_at": "2026-11-23T09:00:00+00:00",
        "original_start_at": "2026-11-23T09:00:00+00:00",
    }
    if series_key is not None:
        data["recurring_event_id"] = series_key
    if ical_uid is not None:
        data["ical_uid"] = ical_uid
    if not scopes_key_absent:
        data["scopes"] = []
    uuid_ = core.entity.create(entity_type="Event", data=data)
    core._conn.commit()  # entity.create doesn't commit; the API handler has its own connection
    return uuid_


def _scopes_of(core, entity_uuid):
    row = core._conn.execute(
        "SELECT data FROM entity WHERE uuid = ?", (entity_uuid,)
    ).fetchone()
    data = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
    return data.get("scopes")


class TestApplyCascade:
    def test_edit_cascades_scopes_across_series(self, client, auth_headers, core):
        hh = _make_event(core, "e-1", series_key="series-1")
        sib1 = _make_event(core, "e-2", series_key="series-1")
        sib2 = _make_event(core, "e-3", series_key="series-1")
        dead = _make_event(core, "e-4", series_key="series-1")
        # a superseded sibling is out of the sweep (live rows only)
        core._conn.execute(
            "UPDATE entity SET superseded_by = ? WHERE uuid = ?",
            ("core_ghost", dead),
        )
        core._conn.commit()

        response = client.post(
            "/mg",
            json={"op": "edit", "target": hh, "set": {"title": "renamed",
                                                       "scopes": ["scope-household"]}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _scopes_of(core, hh) == ["scope-household"]
        assert _scopes_of(core, sib1) == ["scope-household"]
        assert _scopes_of(core, sib2) == ["scope-household"]
        assert _scopes_of(core, dead) is None  # superseded: untouched
        # siblings kept their other fields (scopes ride along, nothing clobbered)
        row = core._conn.execute(
            "SELECT data FROM entity WHERE uuid = ?", (sib1,)
        ).fetchone()
        assert json.loads(row["data"])["title"] == "Birthday e-2"

    def test_cascade_opt_out_scopes_single_instance(self, client, auth_headers, core):
        target = _make_event(core, "o-1", series_key="series-opt")
        sib = _make_event(core, "o-2", series_key="series-opt")
        response = client.post(
            "/mg",
            json={"op": "edit", "target": target,
                  "set": {"scopes": ["scope-household"]},
                  "cascade_series": False},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _scopes_of(core, target) == ["scope-household"]
        assert _scopes_of(core, sib) is None

    def test_make_public_cascades_cleared(self, client, auth_headers, core):
        # instances previously decided into a scope; → Public clears ALL of
        # them — present-but-empty [] is a real decision that cascades too
        target = _make_event(core, "p-1", series_key="series-pub",
                             scopes_key_absent=False)
        sib = _make_event(core, "p-2", series_key="series-pub",
                          scopes_key_absent=False)
        response = client.post(
            "/mg",
            json={"op": "edit", "target": target, "set": {"scopes": []}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _scopes_of(core, target) == []
        assert _scopes_of(core, sib) == []

    def test_non_scope_edit_does_not_cascade(self, client, auth_headers, core):
        target = _make_event(core, "n-1", series_key="series-n")
        sib = _make_event(core, "n-2", series_key="series-n")
        response = client.post(
            "/mg",
            json={"op": "edit", "target": target, "set": {"title": "renamed only"}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _scopes_of(core, sib) is None

    def test_ical_uid_fallback(self, client, auth_headers, core):
        # occurrences without recurring_event_id but sharing ical_uid
        target = _make_event(core, "i-1", ical_uid="abc123@google.com")
        sib = _make_event(core, "i-2", ical_uid="abc123@google.com")
        response = client.post(
            "/mg",
            json={"op": "edit", "target": target,
                  "set": {"scopes": ["scope-household"]}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _scopes_of(core, sib) == ["scope-household"]

    def test_cascade_is_transactional(self, client, auth_headers, core):
        # a poisoned sibling (corrupt JSON data) fails the sweep mid-way;
        # get_core rolls back on exception, so NOTHING changes — not even
        # the primary edit
        target = _make_event(core, "t-1", series_key="series-tx")
        good_sib = _make_event(core, "t-2", series_key="series-tx")
        bad_sib = _make_event(core, "t-3", series_key="series-tx")
        core._conn.execute(
            "UPDATE entity SET data = '{corrupt' WHERE uuid = ?", (bad_sib,)
        )
        core._conn.commit()

        response = client.post(
            "/mg",
            json={"op": "edit", "target": target,
                  "set": {"scopes": ["scope-household"]}},
            headers=auth_headers,
        )
        assert response.status_code >= 400
        assert _scopes_of(core, target) is None      # primary rolled back too
        assert _scopes_of(core, good_sib) is None    # sibling untouched


# ---------------------------------------------------------------------------
# ingestion inheritance (provider_runtime._upsert_entity)
# ---------------------------------------------------------------------------

ENTITY_MODULE_TEMPLATE = '''
import json


class UpsertProvider:
    ENTITY_UPSERTS = True

    def __init__(self, config=None):
        self.config = config or {}

    def sync(self, since=None):
        with open(self.config["events_file"]) as fh:
            events = json.load(fh)
        for event in events:
            yield {
                "op": "upsert_entity",
                "type": "Event",
                "key": {
                    "source": "google_calendar",
                    "account": "me@gmail.com",
                    "calendar_id": "cal1",
                    "source_id": event["source_id"],
                },
                "data": dict(event.get("data") or {}),
                "metadata": dict(event.get("metadata") or {}),
            }
'''


@pytest.fixture
def entity_module(tmp_path, monkeypatch):
    module_name = f"series_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(ENTITY_MODULE_TEMPLATE)
    events_file = tmp_path / "events.json"
    events_file.write_text("[]")
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"module": module_name, "events_file": str(events_file)}


@pytest.fixture
def triage_uuid(core):
    """Mint the builtin triage scope (same shape as the upsert tests') —
    the default scope policy routes unmatched entities to it, and the
    triage queue tests need it to label rows into the window."""
    uuid_ = core.entity.create(
        entity_type="Scope",
        data={"label": "Triage / Untrusted", "active_participants": [],
              "artifact_uuids": []},
    )
    core._conn.commit()
    return uuid_


def _register(client, auth_headers, entity_module):
    payload = {
        "name": "seriesprov",
        "module": entity_module["module"],
        "class": "UpsertProvider",
        "interval": 3600,
        "config": {"events_file": entity_module["events_file"]},
    }
    response = client.post("/api/v1/providers", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.get_json()


def _run_and_wait(client, auth_headers, name="seriesprov", timeout=5.0):
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
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def _series_event(source_id, etag, series=None, scopes=None):
    data = {
        "source_id": source_id,
        "title": "Birthday",
        "starts_at": "2027-08-22T09:00:00+00:00",
    }
    if series is not None:
        data["recurring_event_id"] = series
    if scopes is not None:
        data["scopes"] = scopes
    return {
        "source_id": source_id,
        "data": data,
        "metadata": {
            "source": "google_calendar",
            "account": "me@gmail.com",
            "calendar_id": "cal1",
            "etag": etag,
        },
    }


def _set_events(entity_module, events):
    with open(entity_module["events_file"], "w") as fh:
        json.dump(events, fh)


def _event_scopes(core, source_id):
    from system.core import get_core

    with get_core() as c:
        row = c._conn.execute(
            "SELECT data FROM entity WHERE type = 'Event' "
            "AND json_extract(data, '$.source_id') = ? LIMIT 1",
            (source_id,),
        ).fetchone()
    assert row is not None, f"event {source_id} not stored"
    data = json.loads(row["data"])
    return data.get("scopes", "__absent__")


class TestIngestionInheritance:
    def test_new_occurrence_inherits_series_scope(self, client, auth_headers,
                                                  core, entity_module, triage_uuid):
        _register(client, auth_headers, entity_module)
        # run 1: the first instance, decided into Household by the operator
        _set_events(entity_module, [_series_event("a1", "etag-1", series="ser-1")])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        assert _event_scopes(core, "a1") != "__absent__"  # policy labeled it
        first_uuid = core._conn.execute(
            "SELECT uuid FROM entity WHERE type = 'Event' "
            "AND json_extract(data, '$.source_id') = 'a1'"
        ).fetchone()[0]
        response = client.post(
            "/mg",
            json={"op": "edit", "target": first_uuid,
                  "set": {"scopes": ["scope-household"]}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        assert _event_scopes(core, "a1") == ["scope-household"]

        # run 2: a NEW occurrence of the same series arrives — inherits the
        # series decision instead of the policy default (triage)
        _set_events(entity_module, [
            _series_event("a1", "etag-1", series="ser-1"),
            _series_event("a2", "etag-1", series="ser-1"),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        assert _event_scopes(core, "a2") == ["scope-household"]

    def test_resync_reinherits_instead_of_drifting(self, client, auth_headers,
                                                   core, entity_module, triage_uuid):
        """The drift kill: the update path strips stored scopes and
        re-labels by policy on every etag bump — series inheritance must
        restore the operator's decision."""
        _register(client, auth_headers, entity_module)
        _set_events(entity_module, [_series_event("d1", "etag-1", series="ser-d")])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        first_uuid = core._conn.execute(
            "SELECT uuid FROM entity WHERE type = 'Event' "
            "AND json_extract(data, '$.source_id') = 'd1'"
        ).fetchone()[0]
        assert client.post(
            "/mg",
            json={"op": "edit", "target": first_uuid,
                  "set": {"scopes": ["scope-household"]}},
            headers=auth_headers,
        ).status_code == 200

        # etag bump: Google changed something (here: starts_at)
        _set_events(entity_module, [
            _series_event("d1", "etag-2", series="ser-d"),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        assert _event_scopes(core, "d1") == ["scope-household"]

    def test_cleared_series_stays_public(self, client, auth_headers,
                                         core, entity_module, triage_uuid):
        """A present-but-empty scopes list (→ Public) is a decision too —
        inheritance copies it instead of letting the policy re-triage.
        (Providers never deliver [] as a decision — falsy scopes are
        'unlabeled' per RFC-014 §10 — so the cleared state comes from an
        operator edit, which is the only real path to it.)"""
        _register(client, auth_headers, entity_module)
        _set_events(entity_module, [
            _series_event("c1", "etag-1", series="ser-c"),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        c1_uuid = core._conn.execute(
            "SELECT uuid FROM entity WHERE type = 'Event' "
            "AND json_extract(data, '$.source_id') = 'c1'"
        ).fetchone()[0]
        assert client.post(
            "/mg",
            json={"op": "edit", "target": c1_uuid, "set": {"scopes": []}},
            headers=auth_headers,
        ).status_code == 200
        assert _event_scopes(core, "c1") == []

        _set_events(entity_module, [
            _series_event("c1", "etag-1", series="ser-c"),  # noop
            _series_event("c2", "etag-1", series="ser-c"),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        assert _event_scopes(core, "c2") == []

    def test_provider_scopes_win_over_series(self, client, auth_headers,
                                             core, entity_module, triage_uuid):
        _register(client, auth_headers, entity_module)
        _set_events(entity_module, [
            _series_event("w1", "etag-1", series="ser-w", scopes=[]),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        _set_events(entity_module, [
            _series_event("w1", "etag-1", series="ser-w", scopes=[]),
            _series_event("w2", "etag-1", series="ser-w",
                          scopes=["scope-provider-label"]),
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        assert _event_scopes(core, "w2") == ["scope-provider-label"]

    def test_undecided_series_follows_policy(self, client, auth_headers,
                                             core, entity_module, triage_uuid):
        _register(client, auth_headers, entity_module)
        _set_events(entity_module, [
            _series_event("u1", "etag-1", series="ser-u"),  # no scopes key
        ])
        assert _run_and_wait(client, auth_headers)["last_status"] == "success"
        # default policy routes unmatched to triage — the series was never
        # decided, so inheritance must NOT have fired
        scopes = _event_scopes(core, "u1")
        assert scopes != "__absent__" and scopes != []


class TestTriageSeriesMap:
    def test_triage_reports_series_blast_radius(self, client, auth_headers,
                                                core, triage_uuid):
        for i in ("m-1", "m-2", "m-3"):
            _make_event(core, i, series_key="ser-m", scopes_key_absent=False)
        core._conn.execute(
            """UPDATE entity SET data = json_set(data, '$.scopes',
                   json_array(?))
               WHERE json_extract(data, '$.source_id') IN ('m-1','m-2','m-3')""",
            (triage_uuid,),
        )
        core._conn.commit()
        response = client.get(f"/api/v1/triage?scope={triage_uuid}",
                              headers=auth_headers)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["series"]["ser-m"]["count"] == 3
        assert body["series"]["ser-m"]["first"] is not None
        assert body["series"]["ser-m"]["last"] is not None

    def test_triage_series_map_empty_without_series(self, client, auth_headers,
                                                    core, triage_uuid):
        _make_event(core, "solo-1", series_key=None, scopes_key_absent=False)
        response = client.get(f"/api/v1/triage?scope={triage_uuid}",
                              headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["series"] == {}


class TestUnsetCascade:
    def test_unset_scopes_cascades_as_cleared(self, client, auth_headers, core):
        target = _make_event(core, "u-1", series_key="series-unset",
                             scopes_key_absent=False)
        sib = _make_event(core, "u-2", series_key="series-unset",
                          scopes_key_absent=False)
        # label both first, then unset the target's scopes
        assert client.post(
            "/mg",
            json={"op": "edit", "target": target,
                  "set": {"scopes": ["scope-household"]}},
            headers=auth_headers,
        ).status_code == 200
        assert _scopes_of(core, sib) == ["scope-household"]  # cascade labeled

        response = client.post(
            "/mg",
            json={"op": "edit", "target": target, "unset": ["scopes"]},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        # unset lands as the cleared decision across the series (the
        # post-edit row lacks the key → new_scopes = [])
        assert _scopes_of(core, target) is None
        assert _scopes_of(core, sib) == []
