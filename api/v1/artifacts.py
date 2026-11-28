"""Artifact read surface — listing, search, detail, source access.

Statement-pipeline register v2 D9 (the trial-exit slice): Core `Artifact`
entities — the docling trial's structured documents — become browsable the
way a file manager would be, without ever exposing the store layout.
JWT-only (operator surfaces; API keys keep entity reads through the /mg
get/query verbs, which gate with the same predicates).

Scope: artifact labels live in `data.scopes` (plain uuids, the entity
convention) and every read runs the RFC-011 §4.5 predicate through
scope_access — advisory while MEMOGARDEN_SCOPE_ENFORCEMENT is off,
enforced when it flips. Search is ladder-shaped like /items/search:
recent → keyword_fallback (LIKE via core.entity.search) → lexical
(entity_fts BM25 via Core.search_entity_fts — the runtime-ensured
internal index; FTS failures degrade down the ladder, never up).

No write surface: relabels go through the existing /mg edit verb
(cascade to derived facts is D8, a later slice). Content never appears
in list payloads — detail, the source-file download, and the fragment-span
read (§15.5 single resolver) are the only content channels.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from api.artifact_spans import content_version_of, spans_for, status_for
from system.core import get_core
from system.exceptions import PermissionDenied, ResourceNotFound, ValidationError
from system.soil import get_soil
from utils import uid

from ..middleware.decorators import _authenticate_jwt
from ..middleware.scope_access import (
    active_scope_for,
    enforcement_enabled,
    filter_visible,
    item_visible,
    public_scope_uuids,
    requester_has_all_scopes,
    row_labels,
)

logger = logging.getLogger(__name__)

artifacts_bp = Blueprint("artifacts", __name__, url_prefix="/artifacts")

_OFFSET_CAP = 100_000
_LIST_LIMIT_CAP = 500
_SEARCH_LIMIT_CAP = 200

_DEFAULT_STORE = "~/.local/share/memogarden/attachments"


def _store_root() -> Path:
    """The content-addressed AttachmentStore root (same resolution the
    docling ingest uses: MEMOGARDEN_ATTACHMENT_STORE, else the data dir's
    attachments/)."""
    root = os.environ.get("MEMOGARDEN_ATTACHMENT_STORE") or _DEFAULT_STORE
    return Path(root).expanduser()


def _int_arg(name: str, default: int, cap: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError(
            f"{name} must be an integer",
            {name: raw},
        ) from None
    return min(max(value, 0), cap)


def _resolve_scope_ref(scope_ref: str) -> str:
    """A ?scope= reference (plain/prefixed uuid, builtin alias, or label)
    to the scope's plain uuid. Same resolution rules as the triage queue."""
    from .operator import _BUILTIN_ALIASES

    plain = uid.strip_prefix(scope_ref)
    label = _BUILTIN_ALIASES.get(scope_ref, scope_ref)
    with get_core() as core:
        row = core._conn.execute(
            "SELECT uuid FROM entity WHERE type = 'Scope' "
            "AND superseded_by IS NULL AND uuid IN (?, ?)",
            (plain, uid.add_core_prefix(plain)),
        ).fetchone()
        if row is None:
            row = core._conn.execute(
                "SELECT uuid FROM entity WHERE type = 'Scope' "
                "AND superseded_by IS NULL "
                "AND json_extract(data, '$.label') = ?",
                (label,),
            ).fetchone()
    if row is None:
        raise ValidationError(
            f"unknown scope: {scope_ref!r} (not a uuid, alias, or label)",
            {"scope": scope_ref},
        )
    return row["uuid"]


def _artifact_row(entity: dict) -> dict:
    """Console row shape for one live Artifact entity (no content)."""
    data = entity.get("data") or {}
    extraction = data.get("extraction") or {}
    classification = data.get("classification") or {}
    return {
        "uuid": uid.add_core_prefix(entity["uuid"]),
        "label": data.get("label") or "",
        "category": data.get("category") or "unknown",
        "doc_class": data.get("doc_class") or "unknown",
        "mixed": bool(classification.get("mixed")),
        "confidence": classification.get("confidence"),
        "scopes": data.get("scopes") or [],
        "pages": extraction.get("pages"),
        "seconds": extraction.get("seconds"),
        "truncated": bool(extraction.get("truncated")),
        "content_type": data.get("content_type") or "",
        "created_at": entity.get("created_at"),
        "updated_at": entity.get("updated_at"),
    }


def _load_artifact(uuid: str, core=None) -> dict | None:
    """One live Artifact entity row (data JSON parsed), or None.

    Pass `core` to reuse an open connection (the search walk re-loads up
    to `limit` hits — one connection, not one per hit).
    """
    plain = uid.strip_prefix(uuid)
    if core is None:
        with get_core() as core:
            return _load_artifact(uuid, core=core)

    row = core._conn.execute(
        "SELECT uuid, type, data, created_at, updated_at, superseded_by "
        "FROM entity WHERE uuid IN (?, ?) AND type = 'Artifact' "
        "AND superseded_by IS NULL",
        (plain, uid.add_core_prefix(plain)),
    ).fetchone()
    if row is None:
        return None
    entity = dict(row)
    entity["data"] = json.loads(entity["data"]) if entity["data"] else {}
    return entity


def _visible(entity: dict) -> bool:
    """The §4.5 predicate, flag- and sovereignty-guarded (SG-11: a JWT
    operator session sees everything; API keys are 401'd at these routes
    anyway — the check exists so the surface stays correct if it ever
    widens, and to keep search results honest under enforcement)."""
    if not enforcement_enabled() or requester_has_all_scopes():
        return True
    return item_visible(
        row_labels(entity.get("data")),
        active_scope_for(_jwt_actor()),
        public_scope_uuids(),
    )


def _gate(entity: dict) -> None:
    """Raise when the §4.5 predicate denies one artifact."""
    if not _visible(entity):
        raise PermissionDenied(
            "Artifact not visible from your current scope (RFC-011 §4.5)",
            {"uuid": uid.add_core_prefix(entity["uuid"]),
             "code": "scope_read_denied"},
        )


def _jwt_actor() -> str:
    from flask import g

    return getattr(g, "actor", "") or ""


@artifacts_bp.get("")
def list_artifacts():
    """List live Artifact documents, newest first.

    ?doc_class= filter (statement/overview/bill/notice/policy/document/
    mixed/unknown) · ?scope= narrow-to-scope filter · ?limit/?offset
    page. Visibility runs the §4.5 predicate post-fetch (SG-10) — the
    total reflects the fetched window when filtering hides rows (the
    same trade-off handle_query documents).
    """
    _authenticate_jwt()
    limit = _int_arg("limit", 50, _LIST_LIMIT_CAP)
    offset = _int_arg("offset", 0, _OFFSET_CAP)
    doc_class = (request.args.get("doc_class") or "").strip()
    scope_ref = (request.args.get("scope") or "").strip()
    scope_uuid = _resolve_scope_ref(scope_ref) if scope_ref else None

    entities, total = _list_entities(doc_class, scope_uuid, limit, offset)
    entities = filter_visible(entities, _jwt_actor(), lambda e: (e.get("data") or {}).get("scopes"))
    return jsonify({
        "artifacts": [_artifact_row(e) for e in entities],
        "total": total,
        "offset": offset,
        "limit": limit,
    })


@artifacts_bp.get("/search")
def search_artifacts():
    """Document search for the console Documents tab.

    ?q= text query · ?scope= filter · ?doc_class= filter · ?limit/?offset.
    Ladder, degrading downward never upward: lexical (entity_fts BM25 —
    the runtime-ensured internal index) → keyword_fallback (LIKE via
    core.entity.search) → recent (no query). Lexical hits are re-checked
    against the LIVE entity row for existence and scope before they are
    returned — the index is never trusted, the same boundary rule
    /items/search applies. Every response carries next_offset/has_more.
    """
    _authenticate_jwt()
    q = (request.args.get("q") or "").strip()
    doc_class = (request.args.get("doc_class") or "").strip()
    scope_ref = (request.args.get("scope") or "").strip()
    limit = _int_arg("limit", 50, _SEARCH_LIMIT_CAP)
    offset = _int_arg("offset", 0, _OFFSET_CAP)
    scope_uuid = _resolve_scope_ref(scope_ref) if scope_ref else None

    if not q:
        entities, total = _list_entities(doc_class, scope_uuid, limit, offset)
        entities = filter_visible(entities, _jwt_actor(), lambda e: (e.get("data") or {}).get("scopes"))
        rows = [_artifact_row(e) for e in entities]
        return jsonify({
            "mode": "recent",
            "q": "",
            "results": rows,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + len(rows) if rows and len(rows) == limit else None,
            "has_more": len(rows) == limit,
        })

    # Enforcement runs in the per-hit re-check (_visible) rather than a
    # pre-match allowlist: the fallback plane fetches whole windows, and
    # one predicate serves both planes and the flag/sovereignty guards.

    mode = "lexical"
    rows: list = []
    next_offset: int | None = None
    has_more = False
    if q:
        try:
            # Probe first so the mode is stable across pages (the
            # /items/search rule): a query with any FTS hits stays
            # lexical even when a page runs past the hits. Zero hits
            # falls through to the LIKE recall tail — unicode61 does not
            # tokenize CJK runs, and short/substring recall is what LIKE
            # is still for.
            with get_core() as core:
                probe = core.search_entity_fts(
                    q, limit=1, offset=0, live_only=True, types=["Artifact"]
                )
            if not probe:
                mode = "keyword_fallback"
            else:
                rows, next_offset, has_more = _lexical_page(
                    q, limit, offset, scope_uuid, doc_class
                )
        except (AttributeError, sqlite3.DatabaseError) as e:
            # DatabaseError covers OperationalError and FTS5 corruption
            # ("database disk image is malformed") — the ladder degrades
            # downward, never 500s.
            logger.warning("artifact lexical search degraded: %s", e)
            mode = "keyword_fallback"

        if mode == "keyword_fallback":
            rows = _keyword_page(q, limit, offset, scope_uuid, doc_class)
            next_offset = offset + limit if len(rows) == limit else None
            has_more = len(rows) == limit

    return jsonify({
        "mode": mode,
        "q": q,
        "results": rows,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "has_more": has_more,
    })


def _lexical_page(q: str, limit: int, offset: int, scope_uuid: str | None,
                  doc_class: str):
    """One page of the ranked candidate list, starting at candidate
    position `offset`.

    Walks FTS pages until it holds `limit` LIVE rows: a candidate the
    re-check drops (superseded, relabeled or uncategorized since the
    index caught up) must not shorten the page — the console reads a
    short page as the end of results (the api#91 rule, /items/search's
    _live_page). The offsets are positions in the candidate list, not
    counts of returned rows. Lexical hits are re-checked against the
    LIVE entity row for existence, scope, doc_class and §4.5 visibility —
    the index is never trusted.
    """
    rows: list = []
    position = offset
    exhausted = False
    with get_core() as core:
        while len(rows) < limit and not exhausted:
            need = limit - len(rows)
            hits = core.search_entity_fts(
                q, limit=need, offset=position,
                live_only=True, types=["Artifact"],
            )
            if not hits:
                exhausted = True
                break
            for hit in hits:
                entity = _load_artifact(hit["uuid"], core=core)
                if entity is None:
                    continue  # superseded/gone since indexing
                if scope_uuid and scope_uuid not in [
                    uid.strip_prefix(s) for s in entity["data"].get("scopes") or []
                ]:
                    continue
                if doc_class and (entity["data"].get("doc_class") or "unknown") != doc_class:
                    continue
                if not _visible(entity):
                    continue
                row = _artifact_row(entity)
                row["score"] = round(-hit["rank"], 3)
                rows.append(row)
            position += len(hits)
            if len(hits) < need:
                exhausted = True
    if exhausted:
        return rows, None, False
    return rows, position, True


def _keyword_page(q: str, limit: int, offset: int, scope_uuid: str | None,
                  doc_class: str) -> list:
    """The LIKE recall tail (core.entity.search has no offset, so the
    window is fetched whole and sliced — pages stay duplicate-free)."""
    with get_core() as core:
        entities = core.entity.search(
            q, coverage="content", limit=limit + offset,
            allowed_scopes=None,  # re-filtered below, same as lexical
        )
    entities = [
        e for e in entities
        if e.get("type") == "Artifact"
        and (not doc_class or (e.get("data") or {}).get("doc_class", "unknown") == doc_class)
        and (not scope_uuid or scope_uuid in [
            uid.strip_prefix(s) for s in (e.get("data") or {}).get("scopes") or []
        ])
        and _visible(e)
    ]
    return [_artifact_row(e) for e in entities[offset:offset + limit]]


def _list_entities(doc_class: str, scope_uuid: str | None, limit: int,
                   offset: int):
    """The list query behind ?recent mode and GET /artifacts."""
    data_filters = {"doc_class": doc_class} if doc_class else None
    with get_core() as core:
        entities, total = core.entity.query_with_filters(
            entity_type="Artifact",
            limit=limit,
            offset=offset,
            data_filters=data_filters,
        )
    if scope_uuid:
        entities = [
            e for e in entities
            if scope_uuid in [uid.strip_prefix(s) for s in (e.get("data") or {}).get("scopes") or []]
        ]
    return entities, total


@artifacts_bp.get("/<uuid>")
def get_artifact(uuid: str):
    """One document: full content, provenance, and its source fact."""
    _authenticate_jwt()
    entity = _load_artifact(uuid)
    if entity is None:
        raise ResourceNotFound("Artifact not found", {"uuid": uuid})
    _gate(entity)

    data = entity["data"]
    source = data.get("source") or {}
    fact_uuid = source.get("attachment_uuid")
    source_fact = None
    if fact_uuid:
        with get_soil() as soil:
            conn = soil._get_connection()
            fact_row = conn.execute(
                "SELECT uuid, data, metadata, realized_at, canonical_at FROM item "
                "WHERE uuid IN (?, ?)",
                (uid.strip_prefix(fact_uuid), uid.add_soil_prefix(uid.strip_prefix(fact_uuid))),
            ).fetchone()
        if fact_row is not None:
            fact_data = json.loads(fact_row["data"]) if fact_row["data"] else {}
            source_fact = {
                "uuid": uid.add_soil_prefix(fact_row["uuid"]),
                "filename": fact_data.get("filename") or data.get("label"),
                "mime_type": fact_data.get("mime_type"),
                "size": fact_data.get("size"),
                "sha256": fact_data.get("sha256") or source.get("sha256"),
                "realized_at": fact_row["realized_at"],
                "canonical_at": fact_row["canonical_at"],
            }

    return jsonify({
        "artifact": {
            **_artifact_row(entity),
            "content": data.get("content") or "",
            "source": source,
            "classification": data.get("classification") or {},
        },
        "source_fact": source_fact,
    })


@artifacts_bp.get("/<uuid>/file")
def get_artifact_file(uuid: str):
    """The source document's bytes, from the content-addressed store.

    Serves the PDF exactly as ingested (sha256-verified at ingest; the
    path is resolved under the store root and never trusted verbatim).
    """
    _authenticate_jwt()
    entity = _load_artifact(uuid)
    if entity is None:
        raise ResourceNotFound("Artifact not found", {"uuid": uuid})
    _gate(entity)

    fact_uuid = (entity["data"].get("source") or {}).get("attachment_uuid")
    if not fact_uuid:
        raise ResourceNotFound(
            "Artifact has no source fact", {"uuid": uid.add_core_prefix(entity["uuid"])}
        )
    with get_soil() as soil:
        fact_row = soil._get_connection().execute(
            "SELECT data FROM item WHERE uuid IN (?, ?)",
            (uid.strip_prefix(fact_uuid), uid.add_soil_prefix(uid.strip_prefix(fact_uuid))),
        ).fetchone()
    if fact_row is None:
        raise ResourceNotFound("Source fact not found", {"attachment_uuid": fact_uuid})

    fact_data = json.loads(fact_row["data"]) if fact_row["data"] else {}
    content_path = fact_data.get("content_path")
    if not content_path:
        raise ResourceNotFound(
            "Source fact carries no content_path",
            {"attachment_uuid": uid.add_soil_prefix(fact_uuid)},
        )

    root = _store_root().resolve()
    candidate = (root / content_path).resolve()
    if not candidate.is_relative_to(root):
        from system.exceptions import PermissionDenied

        raise PermissionDenied(
            "content_path escapes the attachment store",
            {"attachment_uuid": uid.add_soil_prefix(fact_uuid)},
        )
    if not candidate.is_file():
        raise ResourceNotFound(
            "Stored content missing", {"content_path": str(content_path)}
        )
    # The mime comes from agent-written fact JSON — allowlist the trial
    # contract (PDFs) and serve anything else as an opaque download, so a
    # tampered fact can never render inline on an api-origin response.
    mime = fact_data.get("mime_type")
    return send_file(
        candidate,
        mimetype=mime if mime == "application/pdf" else "application/octet-stream",
        conditional=True,
    )


@artifacts_bp.get("/<uuid>/spans")
def artifact_spans(uuid):
    """Fragment spans for the artifact's HEAD content version (§15.4/§15.5).

    The single-resolver read surface: every app resolves ^ids through this
    route — spans are canonical state (schema 20261130), chunked by the
    Artifact Handler and persisted by the bridge. status: `ok` (spans
    follow, ordered by document position) · `pending` (§15.6 step 4 — the
    producer was down at commit; the sweep fills it) · `none` (never
    chunked — pre-span artifact).
    """
    _authenticate_jwt()
    entity = _load_artifact(uuid)
    if entity is None:
        raise ResourceNotFound(
            "Artifact not found", {"uuid": uid.add_core_prefix(uid.strip_prefix(uuid))}
        )
    _gate(entity)

    content = entity["data"].get("content") or ""
    version = content_version_of(content)
    with get_core() as core:
        status = status_for(core._conn, entity["uuid"], version)
        spans = spans_for(core._conn, entity["uuid"], version) if status == "ok" else []
    return jsonify({
        "artifact_uuid": uid.add_core_prefix(entity["uuid"]),
        "content_version": version,
        "doc_class": entity["data"].get("doc_class") or "unknown",
        "status": status,
        "spans": spans,
    })
