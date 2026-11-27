"""GET /api/v1/artifacts — the artifact access interface (register v2 D9).

- GET /api/v1/artifacts           — list (doc_class/scope filters, paging)
- GET /api/v1/artifacts/search    — recent → keyword_fallback → lexical ladder
- GET /api/v1/artifacts/<uuid>    — detail incl. content + source-fact summary
- GET /api/v1/artifacts/<uuid>/file — the stored source document's bytes

JWT-only (operator surface); §4.5-gated via scope_access (advisory while
MEMOGARDEN_SCOPE_ENFORCEMENT is off, enforced when on). The lexical mode
runs on entity_fts — the runtime-ensured internal index; tests ensure it
explicitly because the api conftest replaces _create_connection.
"""

import json
import sqlite3

import pytest

from system.core.fts import ensure_entity_fts
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime

FIXTURE_PDF = b"%PDF-1.4 minimal fixture\n"


def _make_attachment_fact(filename: str, sha256: str, content_path: str | None,
                          mime_type: str = "application/pdf",
                          size: int = len(FIXTURE_PDF)) -> str:
    data = {
        "filename": filename,
        "mime_type": mime_type,
        "size": size,
        "sha256": sha256,
    }
    if content_path:
        data["content_path"] = content_path
    with get_soil() as soil:
        uuid = soil.create_fact(Fact(
            uuid=generate_soil_uuid(),
            _type="Attachment",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data=data,
            metadata={"provider": "docling", "source": "documents_watch",
                      "scopes": []},
            integrity_hash=None,
            fidelity="full",
        ))
    return uuid  # plain uuid (no soil_ prefix in storage)


def _make_artifact(core, label: str, content: str, scopes: list | None = None,
                   doc_class: str = "unknown", attachment_uuid: str | None = None,
                   sha256: str | None = None) -> str:
    data = {
        "label": label,
        "content": content,
        "content_type": "text/markdown",
        "scopes": scopes or [],
        "doc_class": doc_class,
        "classification": {"method": "rules-v1", "confidence": "high",
                           "evidence": ["test fixture"], "mixed": False},
        "extraction": {"engine": "docling", "version": "2.153.0",
                       "pages": 2, "seconds": 40.0, "truncated": False},
    }
    if attachment_uuid:
        data["source"] = {"attachment_uuid": attachment_uuid,
                          "sha256": sha256 or "deadbeef",
                          "origin_path": "~/Documents/x.pdf"}
    uuid = core.entity.create(entity_type="Artifact", data=data)
    core._conn.commit()
    return uuid


def _make_artifact_with_fact(core, label: str, content: str,
                             scopes: list | None = None,
                             filename: str = "bill.pdf",
                             content_path: str | None = None,
                             mime_type: str = "application/pdf") -> tuple[str, str]:
    sha = f"sha-{label.replace(' ', '')}"
    fact_uuid = _make_attachment_fact(filename, sha, content_path, mime_type)
    artifact_uuid = _make_artifact(core, label, content, scopes=scopes,
                                   attachment_uuid=fact_uuid, sha256=sha)
    return artifact_uuid, fact_uuid


@pytest.fixture
def fts_enabled(core):
    """The api conftest replaces _create_connection, so the runtime ensure
    never fires in tests — the lexical-mode tests arm it explicitly."""
    ensure_entity_fts(core._conn)
    return core


# ============================================================================
# Auth surface
# ============================================================================

def test_artifact_endpoints_are_jwt_only(client, core, auth_headers_apikey):
    _make_artifact(core, "x", "hello")
    for path in ("/api/v1/artifacts", "/api/v1/artifacts/search"):
        r = client.get(path, headers=auth_headers_apikey)
        assert r.status_code == 401


# ============================================================================
# List
# ============================================================================

def test_list_returns_rows_without_content(client, core, auth_headers):
    _make_artifact(core, "SP Services Bill", "electricity charges table")
    _make_artifact(core, "Course notes", "kinetic theory lecture")

    r = client.get("/api/v1/artifacts", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 2
    labels = {a["label"] for a in body["artifacts"]}
    assert labels == {"SP Services Bill", "Course notes"}
    for a in body["artifacts"]:
        assert "content" not in a
        assert a["doc_class"] == "unknown"
        assert a["uuid"].startswith("core_")


def test_list_doc_class_and_scope_filters(client, core, auth_headers):
    scope_a = core.entity.create(
        entity_type="Scope",
        data={"label": "Household", "visibility": "private",
              "active_participants": [], "artifact_uuids": []},
    )
    core._conn.commit()
    _make_artifact(core, "Bill A", "charges", doc_class="unknown")
    _make_artifact(core, "Bill B", "charges", doc_class="bill",
                   scopes=[scope_a])

    by_doc_class = client.get(
        "/api/v1/artifacts?doc_class=bill", headers=auth_headers).get_json()
    assert [a["label"] for a in by_doc_class["artifacts"]] == ["Bill B"]

    by_scope = client.get(
        f"/api/v1/artifacts?scope={scope_a}", headers=auth_headers).get_json()
    assert [a["label"] for a in by_scope["artifacts"]] == ["Bill B"]

    by_builtin = client.get(
        "/api/v1/artifacts?scope=Household", headers=auth_headers).get_json()
    assert [a["label"] for a in by_builtin["artifacts"]] == ["Bill B"]


# ============================================================================
# Detail + source fact
# ============================================================================

def test_detail_includes_content_and_source_fact(client, core, auth_headers):
    uuid, _fact = _make_artifact_with_fact(
        core, "Nov bill", "service charges table", filename="nov.pdf",
        content_path="ab/cd/nov.pdf")

    r = client.get(f"/api/v1/artifacts/core_{uuid}", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["artifact"]["label"] == "Nov bill"
    assert "charges table" in body["artifact"]["content"]
    assert body["artifact"]["source"]["sha256"] == "sha-Novbill"
    assert body["source_fact"]["filename"] == "nov.pdf"
    assert body["source_fact"]["mime_type"] == "application/pdf"
    assert body["source_fact"]["uuid"].startswith("soil_")


def test_detail_404s_unknown_and_superseded(client, core, auth_headers):
    _make_artifact(core, "x", "hello")
    r = client.get("/api/v1/artifacts/core_does-not-exist", headers=auth_headers)
    assert r.status_code == 404
    r = client.get("/api/v1/artifacts/search-nonsense", headers=auth_headers)
    assert r.status_code == 404  # the static /search rule wins; this hits <uuid>


# ============================================================================
# Source file access
# ============================================================================

def test_file_serves_the_stored_pdf(client, core, auth_headers, tmp_path,
                                    monkeypatch):
    store = tmp_path / "attachments"
    rel = "ab/cd/doc.pdf"
    (store / "ab" / "cd").mkdir(parents=True)
    (store / rel).write_bytes(FIXTURE_PDF)
    monkeypatch.setenv("MEMOGARDEN_ATTACHMENT_STORE", str(store))

    uuid, _fact = _make_artifact_with_fact(
        core, "Doc", "text", content_path=rel)
    r = client.get(f"/api/v1/artifacts/{uuid}/file", headers=auth_headers)
    assert r.status_code == 200
    assert r.data == FIXTURE_PDF
    assert r.content_type.startswith("application/pdf")


def test_file_rejects_paths_leaving_the_store(client, core, auth_headers,
                                              tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOGARDEN_ATTACHMENT_STORE", str(tmp_path))
    uuid, _fact = _make_artifact_with_fact(
        core, "Evil", "text", content_path="../../etc/passwd")
    r = client.get(f"/api/v1/artifacts/{uuid}/file", headers=auth_headers)
    assert r.status_code == 403


def test_file_404s_when_content_missing(client, core, auth_headers, tmp_path,
                                        monkeypatch):
    monkeypatch.setenv("MEMOGARDEN_ATTACHMENT_STORE", str(tmp_path))
    uuid, _fact = _make_artifact_with_fact(
        core, "Ghost", "text", content_path="ab/missing.pdf")
    r = client.get(f"/api/v1/artifacts/{uuid}/file", headers=auth_headers)
    assert r.status_code == 404


# ============================================================================
# Search ladder
# ============================================================================

def test_recent_mode_lists_without_query(client, core, auth_headers):
    _make_artifact(core, "A", "alpha")
    r = client.get("/api/v1/artifacts/search", headers=auth_headers)
    body = r.get_json()
    assert body["mode"] == "recent"
    assert len(body["results"]) == 1
    assert body["has_more"] is False


def test_lexical_mode_finds_document_content(fts_enabled, client, auth_headers):
    core = fts_enabled
    hit, _ = _make_artifact_with_fact(core, "SP bill", "sp services monthly charges")
    _make_artifact(core, "Other", "completely unrelated words here")

    r = client.get("/api/v1/artifacts/search?q=sp+services+charges",
                   headers=auth_headers)
    body = r.get_json()
    assert body["mode"] == "lexical"
    assert [x["uuid"] for x in body["results"]] == [f"core_{hit}"]
    assert body["results"][0]["score"] >= 0  # rank negated to a similarity-ish score


def test_search_degrades_to_keyword_when_fts_is_unavailable(
        client, core, auth_headers, monkeypatch):
    _make_artifact(core, "Named bill", "unmatchable body text")

    def _boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("no such table: entity_fts")

    from system.core import Core
    monkeypatch.setattr(Core, "search_entity_fts", _boom)
    r = client.get("/api/v1/artifacts/search?q=named", headers=auth_headers)
    body = r.get_json()
    assert body["mode"] == "keyword_fallback"
    assert [x["label"] for x in body["results"]] == ["Named bill"]


def test_search_scope_and_doc_class_filters(fts_enabled, client, auth_headers):
    core = fts_enabled
    scope = core.entity.create(
        entity_type="Scope",
        data={"label": "Finances", "visibility": "private",
              "active_participants": [], "artifact_uuids": []},
    )
    core._conn.commit()
    _make_artifact(core, "Statement", "bank statement october", scopes=[scope],
                   doc_class="statement")
    _make_artifact(core, "Lecture", "bank of slides on banks", doc_class="document")

    r = client.get("/api/v1/artifacts/search?q=bank&scope=Finances&doc_class=statement",
                   headers=auth_headers)
    body = r.get_json()
    assert [x["label"] for x in body["results"]] == ["Statement"]


# ============================================================================
# Pagination
# ============================================================================

def test_list_pagination_pages_without_overlap(client, core, auth_headers):
    for i in range(3):
        _make_artifact(core, f"Doc {i}", f"body {i}")
    page1 = client.get("/api/v1/artifacts?limit=2", headers=auth_headers).get_json()
    assert page1["total"] == 3
    assert len(page1["artifacts"]) == 2
    page2 = client.get("/api/v1/artifacts?limit=2&offset=2",
                       headers=auth_headers).get_json()
    assert len(page2["artifacts"]) == 1
    both = {a["uuid"] for a in page1["artifacts"]} | {
        a["uuid"] for a in page2["artifacts"]}
    assert len(both) == 3


def test_keyword_fallback_pages_without_overlap(client, core, auth_headers,
                                                monkeypatch):
    for i in range(5):
        _make_artifact(core, f"Receipt {i}", f"receipt total {i}")

    def _boom(self, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    from system.core import Core
    monkeypatch.setattr(Core, "search_entity_fts", _boom)
    page1 = client.get("/api/v1/artifacts/search?q=receipt&limit=2",
                       headers=auth_headers).get_json()
    page2 = client.get("/api/v1/artifacts/search?q=receipt&limit=2&offset=2",
                       headers=auth_headers).get_json()
    assert page1["mode"] == "keyword_fallback"
    assert page2["mode"] == "keyword_fallback"
    uuids1 = {a["uuid"] for a in page1["results"]}
    uuids2 = {a["uuid"] for a in page2["results"]}
    assert len(page1["results"]) == 2 and len(page2["results"]) == 2
    assert uuids1.isdisjoint(uuids2)                 # the offset-fix assertion


def test_lexical_walk_does_not_shorten_pages_on_stale_hits(
        fts_enabled, client, auth_headers, monkeypatch):
    """A candidate the live re-check drops (a uuid gone from entity since
    the index caught up) must not shorten the page — the console reads a
    short page as the end of results (the api#91 rule)."""
    core = fts_enabled
    real = [_make_artifact(core, f"Inv {i}", f"invoice body {i}")
            for i in range(5)]
    stale = "stale-uuid-not-in-entity"

    pages = {
        0: [{"uuid": stale, "rank": -9.0}]
           + [{"uuid": u, "rank": -5.0 - i} for i, u in enumerate(real[:4])],
        5: [{"uuid": real[4], "rank": -4.0}],
    }

    def fake(self, q, limit=20, offset=0, **kwargs):
        return pages.get(offset, [])

    from system.core import Core
    monkeypatch.setattr(Core, "search_entity_fts", fake)
    r = client.get("/api/v1/artifacts/search?q=invoice&limit=5",
                   headers=auth_headers)
    body = r.get_json()
    assert body["mode"] == "lexical"
    # The stale candidate dropped, the page still holds its full five —
    # and the offsets are candidate-list positions, not returned-row counts.
    assert [x["uuid"] for x in body["results"]] == [f"core_{u}" for u in real]
    assert body["has_more"] is True and body["next_offset"] == 6


# ============================================================================
# Scope gates (§4.5) — enforcement on
# ============================================================================

def test_enforcement_on_keeps_the_operator_sovereign(
        client, core, auth_headers, monkeypatch):
    """SG-11: these are JWT-only operator routes — enforcement never hides
    rows from the sovereign operator, whatever the labels say. The §4.5
    predicate still runs (its result gates /mg access for keyed
    principals); over HTTP on this surface it is always visible."""
    monkeypatch.setenv("MEMOGARDEN_SCOPE_ENFORCEMENT", "1")
    private = _make_scope(core, "NS", visibility="private")
    public = _make_scope(core, "Household", visibility="public")
    _make_artifact(core, "Secret", "unit material", scopes=[private])
    _make_artifact(core, "Shared", "poster", scopes=[public])
    _make_artifact(core, "Open", "unlabelled note")

    r = client.get("/api/v1/artifacts", headers=auth_headers)
    labels = {a["label"] for a in r.get_json()["artifacts"]}
    assert labels == {"Secret", "Shared", "Open"}

    uuid = next(a["uuid"] for a in r.get_json()["artifacts"]
                if a["label"] == "Secret")
    r = client.get(f"/api/v1/artifacts/{uuid}", headers=auth_headers)
    assert r.status_code == 200


def _make_scope(core, label: str, visibility: str | None = None) -> str:
    data = {"label": label, "active_participants": [], "artifact_uuids": []}
    if visibility is not None:
        data["visibility"] = visibility
    uuid = core.entity.create(entity_type="Scope", data=data)
    core._conn.commit()
    return uuid


def test_json_is_json(client, auth_headers):
    """Trivial smoke: the list endpoint answers with a JSON body."""
    r = client.get("/api/v1/artifacts", headers=auth_headers)
    assert r.is_json
    assert json.loads(r.get_data(as_text=True))["artifacts"] == []
