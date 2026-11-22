"""Operator-only console reads (RFC-015 §3.3).

Sovereign-operator conveniences backing the Operator Console: the triage
queue, the scopes/grants/policies overview. Both are reads that a JWT
operator could already express through the generic verbs, but not cheaply
(triage needs a metadata.scopes membership probe the generic query does
not offer; the overview joins three stores). They are JWT-only — they
render authority state (grants, policies), and RFC-011 keeps boundary
configuration out of API-key hands even in read form.
"""

import json
import logging
import os
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from system.core import get_core
from system.exceptions import ValidationError
from system.soil import get_soil
from utils import uid

from ..middleware.decorators import _authenticate_jwt
from ..middleware.scope_access import enforcement_enabled

logger = logging.getLogger(__name__)

operator_bp = Blueprint("operator", __name__)

# Decision-model recommendations (RFC-015 §3.3 extension, 2026-11-22): the
# triage-model batch (~/memogarden/triage-model) precomputes per-item
# recommendations into a JSON sidecar next to the stores. The console
# renders them as read-only badges with a one-tap "apply suggestion", and
# GET /triage/eval scores them against operator decisions already recorded
# in the Action log — no new write path. Absence of a recommendation means
# escalate (the operator decides unaided); the sidecar is operator-owned
# data, never a store of record.
_RECS_CACHE: dict = {"path": None, "mtime": None, "doc": {}}


def _recs_path() -> Path:
    env = os.environ.get("MEMOGARDEN_TRIAGE_RECS")
    if env:
        return Path(env)
    if _RECS_CACHE["path"] is not None:
        return _RECS_CACHE["path"]
    with get_soil() as soil:
        path = soil.db_path.parent / "triage-model" / "recommendations.json"
    _RECS_CACHE["path"] = path
    return path


def _load_recs() -> dict:
    """mtime-cached load of the recommendations sidecar.

    Shape: {model, generated_at, tau, recs: {uuid: {pred, confidence,
    runner}}}. Only non-escalated items are present; a missing/absent entry
    means escalate.
    """
    path = _recs_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _RECS_CACHE["mtime"] == mtime:
        return _RECS_CACHE["doc"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("unreadable triage recommendations sidecar at %s", path)
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("recs"), dict):
        logger.warning("malformed triage recommendations sidecar at %s", path)
        return {}
    _RECS_CACHE["mtime"] = mtime
    _RECS_CACHE["doc"] = doc
    return doc

# Scope labels live on facts in metadata.scopes and on entities in
# data.scopes (RFC-014 §10) — both as plain uuids (DB convention).
_FACT_SCOPES_SQL = """
    SELECT je.value AS scope_uuid, COUNT(*) AS n
    FROM item, json_each(item.metadata, '$.scopes') je
    WHERE item.superseded_by IS NULL
    GROUP BY je.value
"""

_ENTITY_SCOPES_SQL = """
    SELECT je.value AS scope_uuid, entity.type AS entity_type, COUNT(*) AS n
    FROM entity, json_each(entity.data, '$.scopes') je
    WHERE entity.superseded_by IS NULL AND entity.type != 'Scope'
    GROUP BY je.value, entity.type
"""


# Builtin aliases (RFC-011 §4.1) — same mapping as the runtime's
# policy resolver (provider_runtime._make_scope_resolver). "Personal"
# (renamed from "Private", 2026-11-19) — clean break, no legacy alias.
_BUILTIN_ALIASES = {"triage": "Triage / Untrusted", "personal": "Personal"}


def _resolve_scope(scope_ref: str) -> dict:
    """Resolve a scope by uuid, builtin alias, or label to its entity row.

    Accepts: plain/prefixed uuid, builtin alias ('triage', 'personal'), or
    the scope's label ('Triage / Untrusted'). Raises ValidationError if
    nothing matches — the builtin scopes are seeded at startup, so a
    missing triage scope means a broken deployment, not an empty queue.
    """
    plain = uid.strip_prefix(scope_ref)
    label = _BUILTIN_ALIASES.get(scope_ref, scope_ref)
    core = get_core()
    row = core._conn.execute(
        "SELECT uuid, data, created_at FROM entity WHERE type = 'Scope' "
        "AND superseded_by IS NULL AND uuid IN (?, ?)",
        (plain, uid.add_core_prefix(plain)),
    ).fetchone()
    if row is None:
        row = core._conn.execute(
            "SELECT uuid, data, created_at FROM entity WHERE type = 'Scope' "
            "AND superseded_by IS NULL AND json_extract(data, '$.label') = ?",
            (label,),
        ).fetchone()
    if row is None:
        raise ValidationError(
            f"unknown scope: {scope_ref!r} (not a uuid, alias, or label)",
            {"scope": scope_ref},
        )
    return {"uuid": row["uuid"], "data": row["data"], "created_at": row["created_at"]}


# The triage queue collapses supersedes chains for family/stub captions
# (system#11 semantics). That needs every superseded soil row — 67k+ and
# growing with each amend — and rebuilding the map on every request cost
# ~0.5s of each triage load. The map only changes when an amend writes a new
# supersedes edge, and amend goes through the generic handlers (no single
# invalidation seam), so cache with a short TTL: captions may lag a relabel
# by at most _SUP_MAP_TTL seconds. Keyed on the soil db path so test
# fixtures (each with its own database) never share entries. Returns a
# SHARED mapping — read-only use.
_SUP_MAP_TTL = 30.0
_sup_map_cache: dict = {"key": None, "at": -_SUP_MAP_TTL, "map": {}}


def _load_sup_map() -> dict:
    with get_soil() as soil_conn:
        key = str(soil_conn.db_path)
        now = time.monotonic()
        if _sup_map_cache["key"] == key and now - _sup_map_cache["at"] < _SUP_MAP_TTL:
            return _sup_map_cache["map"]
        rows = soil_conn._get_connection().execute(
            "SELECT uuid, superseded_by FROM item WHERE superseded_by IS NOT NULL"
        ).fetchall()
    _sup_map_cache["key"] = key
    _sup_map_cache["at"] = now
    _sup_map_cache["map"] = {r["uuid"]: r["superseded_by"] for r in rows}
    return _sup_map_cache["map"]


@operator_bp.get("/triage")
def triage_queue():
    """List facts and entities currently labeled into a scope (RFC-015 §3.3).

    Defaults to the builtin Triage scope; `?scope=` may name any scope by
    uuid, alias, or label. Items newest-first. `?account=` filters on the
    RFC-012 attribution stamp (facts metadata.account / entities data.account).

    Returns:
        200: {"scope": {...}, "facts": [...], "entities": [...],
              "total_facts": n, "total_entities": n}
        400: Unknown scope / bad limit
    """
    _authenticate_jwt()

    scope_ref = request.args.get("scope", "triage")
    scope = _resolve_scope(scope_ref)
    scope_uuid = scope["uuid"]

    account = request.args.get("account")
    raw_limit = request.args.get("limit", "200")
    raw_offset = request.args.get("offset", "0")
    try:
        limit = max(1, min(int(raw_limit), 500))
        offset = max(0, int(raw_offset))
    except ValueError:
        raise ValidationError(
            "limit/offset must be integers",
            {"limit": raw_limit, "offset": raw_offset},
        ) from None

    fact_where = """superseded_by IS NULL
        AND EXISTS (SELECT 1 FROM json_each(metadata, '$.scopes') je
                    WHERE je.value = ?)"""
    fact_params: list = [scope_uuid]
    if account:
        fact_where += " AND json_extract(metadata, '$.account') = ?"
        fact_params.append(account)

    # Private-connection access mirrors the RFC-014 mark-occurred probe.
    # Canonical ordering: import batches land with near-identical realized_at
    # (the 2026-11 gmail/gcal backfills), which made the queue meaningless —
    # order by when the thing actually happened, falling back to import time.
    with get_soil() as soil:
        conn = soil._get_connection()
        facts = conn.execute(
            f"""SELECT uuid, _type, realized_at, canonical_at, data, metadata
                FROM item WHERE {fact_where}
                ORDER BY COALESCE(canonical_at, realized_at) DESC LIMIT ? OFFSET ?""",
            (*fact_params, limit, offset),
        ).fetchall()
        total_facts = conn.execute(
            f"SELECT COUNT(*) AS n FROM item WHERE {fact_where}", fact_params
        ).fetchone()["n"]

    entity_where = """superseded_by IS NULL AND type != 'Scope'
        AND EXISTS (SELECT 1 FROM json_each(data, '$.scopes') je
                    WHERE je.value = ?)"""
    entity_params: list = [scope_uuid]
    if account:
        entity_where += " AND json_extract(data, '$.account') = ?"
        entity_params.append(account)

    # Entity canonical time lives in type-specific data fields (gcal Events:
    # starts_at; tada Tasks/Recurrences: start_at) — COALESCE down to
    # updated_at so untyped entities still sort by import recency.
    core = get_core()
    entities = core._conn.execute(
        f"""SELECT uuid, type, created_at, updated_at, data
            FROM entity WHERE {entity_where}
            ORDER BY COALESCE(
                json_extract(data, '$.starts_at'),
                json_extract(data, '$.start_at'),
                updated_at) DESC LIMIT ? OFFSET ?""",
        (*entity_params, limit, offset),
    ).fetchall()
    total_entities = core._conn.execute(
        f"SELECT COUNT(*) AS n FROM entity WHERE {entity_where}", entity_params
    ).fetchone()["n"]

    def _loads(value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    # Families (RFC-015 §3.3): children are grouped under their parent so a
    # triage decision covers the family (console cascades filings). Two
    # linkages exist today: tada Notes (child metadata.task_source_id ->
    # parent fact data.source_id) and generic contained_by relations
    # (child -> parent). Only pairs with BOTH endpoints inside the returned
    # window are reported; children whose parent fell outside the window
    # get a parent STUB (live version resolved anywhere in the corpus) so
    # the console can caption the orphan instead of showing a mystery row.
    child_parent: dict = {}
    parents_by_source_id: dict = {}
    for r in facts:
        source_id = (_loads(r["data"]) or {}).get("source_id")
        if source_id:
            parents_by_source_id[source_id] = uid.add_soil_prefix(r["uuid"])
    for r in facts:
        task_source_id = (_loads(r["metadata"]) or {}).get("task_source_id")
        if task_source_id and task_source_id in parents_by_source_id:
            child_parent[uid.add_soil_prefix(r["uuid"])] = parents_by_source_id[task_source_id]
    item_by_bare: dict = {}
    for r in facts:
        prefixed = uid.add_soil_prefix(r["uuid"])
        item_by_bare[prefixed.removeprefix("soil_")] = prefixed
    for r in entities:
        prefixed = uid.add_core_prefix(r["uuid"])
        item_by_bare[prefixed.removeprefix("core_")] = prefixed

    # contained_by pairs live in BOTH relation stores: soil's system_relation
    # (provider-created — gmail attachments -> emails, session links) and
    # core's user_relation (RFC-002, dormant). Relation targets may sit on a
    # superseded email version — system#11 semantics: an amended email's
    # attachments keep parented to the version they were captured for — so
    # collapse supersedes chains to the live parent before matching the
    # window.
    with get_soil() as soil2:
        rconn = soil2._get_connection()
        rel_rows = rconn.execute(
            "SELECT source, target FROM system_relation WHERE kind = 'contained_by'"
        ).fetchall()
    sup_map = _load_sup_map()

    def _live_target(prefixed: str) -> str:
        seen: set = set()
        u = prefixed
        while u in sup_map and u not in seen:
            seen.add(u)
            u = sup_map[u]
        if u in sup_map:
            # A supersedes cycle is data corruption; degrade to dropping the
            # pair rather than hanging, but say so.
            logger.warning("supersedes cycle detected at %s; containment pair dropped", prefixed)
        return u

    user_rel_rows = core._conn.execute(
        "SELECT source, target FROM user_relation WHERE kind = 'contained_by'"
    ).fetchall()
    for rel in list(rel_rows) + list(user_rel_rows):
        child = item_by_bare.get(rel["source"].removeprefix("soil_").removeprefix("core_"))
        parent = item_by_bare.get(
            _live_target(rel["target"]).removeprefix("soil_").removeprefix("core_")
        )
        if child and parent and child != parent:
            child_parent[child] = parent

    # Out-of-window parents split two ways (2026-11-21 operator decision,
    # refining the v1 orphan-stub semantics where actions always stayed on
    # the child):
    #   - Parent still TRIAGE-LABELED (undecided, just on an older page):
    #     PULL it into the window and link the family, so the operator makes
    #     one cascading decision for parent + children — grouping and
    #     cascade are the whole point of families.
    #   - Parent already FILED or UNSCOPED (decision made): keep a caption
    #     STUB only, and never rewrite the parent — the console offers
    #     "→ with parent" on the sibling card instead, moving the CHILDREN
    #     to the parent's destination. Re-opening decided items would
    #     re-litigate choices already made and churn the queue.
    parent_stubs: dict = {}
    # prefixed parent uuid -> payload dict (facts/entities), populated when a
    # triage-labeled out-of-window parent is pulled into the window.
    pulled_facts: dict = {}
    pulled_entities: dict = {}

    def _row_scopes(row, *, entity: bool) -> list:
        """Live scopes of a parent row (facts: metadata.scopes, entities:
        data.scopes — both bare uuids per DB convention)."""
        source = _loads(row["data"]) or {} if entity else (_loads(row["metadata"]) or {})
        return source.get("scopes") or []

    def _stub_from_item(row) -> dict:
        data = _loads(row["data"]) or {}
        meta = _loads(row["metadata"]) or {}
        return {
            "uuid": uid.add_soil_prefix(row["uuid"]),
            "type": row["_type"],
            "title": data.get("title") or data.get("subject")
            or data.get("description") or data.get("label") or "",
            "scopes": meta.get("scopes") or [],
        }

    def _stub_from_entity(row) -> dict:
        data = _loads(row["data"]) or {}
        return {
            "uuid": uid.add_core_prefix(row["uuid"]),
            "type": row["type"],
            "title": data.get("title") or data.get("subject")
            or data.get("description") or data.get("label") or "",
            "scopes": data.get("scopes") or [],
        }

    def _pull_item_parent(row, child: str) -> None:
        """Admit a triage-labeled out-of-window soil item into the window."""
        prefixed = uid.add_soil_prefix(row["uuid"])
        if prefixed not in pulled_facts:
            pulled_facts[prefixed] = {
                "uuid": prefixed,
                "type": row["_type"],
                "realized_at": row["realized_at"],
                "canonical_at": row["canonical_at"],
                "data": _loads(row["data"]),
                "metadata": _loads(row["metadata"]) or {},
                "pulled_in": True,
            }
        child_parent[child] = prefixed

    def _pull_entity_parent(row, child: str) -> None:
        """Admit a triage-labeled out-of-window core entity into the window."""
        prefixed = uid.add_core_prefix(row["uuid"])
        if prefixed not in pulled_entities:
            payload = {
                "uuid": prefixed,
                "type": row["type"],
                "updated_at": row["updated_at"],
                "canonical_at": (
                    (_loads(row["data"]) or {}).get("starts_at")
                    or (_loads(row["data"]) or {}).get("start_at")
                    or row["updated_at"] or ""
                ),
                "data": _loads(row["data"]),
                "pulled_in": True,
            }
            pulled_entities[prefixed] = payload
        child_parent[child] = prefixed

    # tada-style children: resolve the live parent by source_id. Triage-
    # labeled parents get pulled in; decided parents become caption stubs.
    for r in facts:
        task_source_id = (_loads(r["metadata"]) or {}).get("task_source_id")
        child_uuid = uid.add_soil_prefix(r["uuid"])
        if not task_source_id or child_uuid in child_parent:
            continue
        with get_soil() as soil2:
            prow = soil2._get_connection().execute(
                "SELECT uuid, _type, realized_at, canonical_at, data, metadata FROM item "
                "WHERE json_extract(data, '$.source_id') = ? "
                "AND superseded_by IS NULL LIMIT 1",
                (task_source_id,),
            ).fetchone()
        if prow is None:
            continue
        if scope_uuid in _row_scopes(prow, entity=False):
            _pull_item_parent(prow, child_uuid)
        else:
            parent_stubs[child_uuid] = _stub_from_item(prow)

    # contained_by children without in-window parents: resolve the target's
    # live version (the target may live in either layer). Same dual-store
    # read + supersedes collapse as the families map above. NB: soil stores
    # item uuids WITH the soil_ prefix (system_relation targets match), so
    # the item lookup uses the prefixed form; entity rows are bare.
    for rel in list(rel_rows) + list(user_rel_rows):
        child = item_by_bare.get(rel["source"].removeprefix("soil_").removeprefix("core_"))
        if child is None or child in child_parent:
            continue
        live_target = _live_target(rel["target"])
        bare_target = live_target.removeprefix("soil_").removeprefix("core_")
        with get_soil() as soil2:
            prow = soil2._get_connection().execute(
                "SELECT uuid, _type, realized_at, canonical_at, data, metadata FROM item "
                "WHERE uuid = ? AND superseded_by IS NULL LIMIT 1", (live_target,)
            ).fetchone()
        if prow is not None:
            if scope_uuid in _row_scopes(prow, entity=False):
                _pull_item_parent(prow, child)
            else:
                parent_stubs[child] = _stub_from_item(prow)
            continue
        erow = core._conn.execute(
            "SELECT uuid, type, updated_at, data FROM entity "
            "WHERE uuid = ? AND superseded_by IS NULL LIMIT 1", (bare_target,)
        ).fetchone()
        if erow is not None:
            if scope_uuid in _row_scopes(erow, entity=True):
                _pull_entity_parent(erow, child)
            else:
                parent_stubs[child] = _stub_from_entity(erow)

    # A child processed BEFORE its parent was pulled (relation order) may
    # carry a stub for a parent that has since been admitted — consolidate:
    # the edge replaces the stub, captions go away for that family.
    for child in [c for c, stub in parent_stubs.items() if stub["uuid"] in pulled_facts
                  or stub["uuid"] in pulled_entities]:
        child_parent[child] = parent_stubs[child]["uuid"]
        del parent_stubs[child]

    def _entity_canonical_at(row) -> str:
        data = _loads(row["data"]) or {}
        return (data.get("starts_at") or data.get("start_at")
                or row["updated_at"] or "")

    facts_payload = [
        {
            "uuid": uid.add_soil_prefix(r["uuid"]),
            "type": r["_type"],
            "realized_at": r["realized_at"],
            "canonical_at": r["canonical_at"],
            "data": _loads(r["data"]),
            "metadata": _loads(r["metadata"]) or {},
        }
        for r in facts
    ]
    entities_payload = [
        {
            "uuid": uid.add_core_prefix(r["uuid"]),
            "type": r["type"],
            "updated_at": r["updated_at"],
            "canonical_at": _entity_canonical_at(r),
            "data": _loads(r["data"]),
        }
        for r in entities
    ]

    # Splice each pulled parent in right before its first child's slot, so
    # the family renders together instead of the parent landing at the
    # array tail (out-of-window parents sort older than the whole window).
    for prefixed, payload in list(pulled_facts.items()):
        child = next((c for c, p in child_parent.items() if p == prefixed), None)
        idx = next((i for i, f in enumerate(facts_payload) if f["uuid"] == child),
                   len(facts_payload))
        facts_payload.insert(idx, payload)
    for prefixed, payload in list(pulled_entities.items()):
        child = next((c for c, p in child_parent.items() if p == prefixed), None)
        idx = next((i for i, e in enumerate(entities_payload) if e["uuid"] == child),
                   len(entities_payload))
        entities_payload.insert(idx, payload)

    # Attach decision-model recommendations (badge + one-tap apply in the
    # console). Pulled-in parents are included — they are decisions too.
    recs_doc = _load_recs()
    recs_by_uuid = recs_doc.get("recs", {})
    for f in facts_payload:
        rec = recs_by_uuid.get(f["uuid"])
        if rec:
            f["rec"] = rec

    response = {
        "scope": {
            "uuid": uid.add_core_prefix(scope_uuid),
            "data": _loads(scope["data"]),
            "created_at": scope["created_at"],
        },
        "facts": facts_payload,
        "entities": entities_payload,
        "total_facts": total_facts,
        "total_entities": total_entities,
        "containment": {
            "child_parent": child_parent,
            "parent_stubs": parent_stubs,
        },
    }
    if recs_doc:
        response["decision_model"] = {
            "model": recs_doc.get("model"),
            "generated_at": recs_doc.get("generated_at"),
            "tau": recs_doc.get("tau"),
        }
    return jsonify(response)


@operator_bp.get("/triage/eval")
def triage_eval():
    """Decision-model agreement tracker (console eval strip).

    The operator's decisions are already recorded as operator amend Actions;
    this endpoint joins the most recent decision per item (after the
    recommendations sidecar was generated) with the stored recommendation.
    Escalated items (no recommendation) and soft leans are excluded by
    construction; the eval set is the first `n` (default 100) distinct
    targets, ordered by their first post-generation decision, each scored
    against its latest decision. The window resets on every sidecar
    regeneration.
    """
    _authenticate_jwt()

    try:
        n = max(1, min(int(request.args.get("n", "100")), 500))
    except ValueError:
        raise ValidationError(
            "n must be an integer", {"n": request.args.get("n")}
        ) from None

    doc = _load_recs()
    recs_by_uuid = doc.get("recs", {})
    since = doc.get("generated_at")
    if not recs_by_uuid or not since:
        return jsonify({
            "state": "no eval window" if recs_by_uuid
                     else "no recommendations file",
            "n": 0, "target": n,
        })

    def _loads(value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    with get_soil() as soil:
        # INDEXED BY idx_item_realized: the time predicate does the real
        # filtering (since = fresh sidecar generated_at); without the hint
        # sqlite picks idx_item_type and json_extract-scans all 50k+ live
        # Actions per console load (review finding, 2026-11-22). Rows come
        # back pre-ordered, so the temp sort disappears too. String compare
        # relies on generated_at being UTC isoformat (recommend.py contract).
        rows = soil._get_connection().execute(
            """SELECT realized_at, data FROM item
               INDEXED BY idx_item_realized
               WHERE _type = 'Action' AND superseded_by IS NULL
                 AND json_extract(data, '$.actor_type') = 'operator'
                 AND json_extract(data, '$.operation') = 'amend'
                 AND realized_at >= ?
               ORDER BY realized_at ASC""",
            (since,),
        ).fetchall()

    # Scope uuid -> label, for mapping a decision's resulting scopes.
    with get_core() as core_conn:
        scope_rows = core_conn._conn.execute(
            "SELECT uuid, data FROM entity WHERE type = 'Scope' "
            "AND superseded_by IS NULL"
        ).fetchall()
    scope_labels = {}
    for r in scope_rows:
        label = (_loads(r["data"]) or {}).get("label")
        if label:
            scope_labels[r["uuid"]] = label
            scope_labels[uid.add_core_prefix(r["uuid"])] = label

    # Latest decision per target (actions arrive in realized_at order).
    decisions: dict = {}
    for row in rows:
        d = _loads(row["data"]) or {}
        params = d.get("params") or {}
        target = params.get("target")
        if not target:
            continue
        scopes = (params.get("metadata") or {}).get("scopes")
        if scopes is None:
            continue
        if scopes:
            decided = scope_labels.get(scopes[0], scopes[0])
        else:
            decided = "unclassified"  # cleared = explicit unscoped decision
        decisions[target] = {"decided": decided, "at": row["realized_at"]}

    # Eval set: first n decisions with a stored recommendation (non-escalated).
    # Soft entries (low-confidence display-only leans) are not recommendations
    # and never count toward the eval.
    entries = []
    for target, dec in decisions.items():
        rec = recs_by_uuid.get(target)
        if rec is None or rec.get("soft"):
            continue  # escalated, soft lean, or not yet recommended
        rec_pred = rec.get("pred")
        entries.append({
            "uuid": target,
            "at": dec["at"],
            "rec": rec_pred,
            "confidence": rec.get("confidence"),
            "runner": rec.get("runner"),
            "decided": dec["decided"],
            "agree": rec_pred == dec["decided"],
        })
        if len(entries) >= n:
            break

    correct = sum(1 for e in entries if e["agree"])
    return jsonify({
        "model": doc.get("model"),
        "generated_at": since,
        "target": n,
        "n": len(entries),
        "correct": correct,
        "agreement": round(correct / len(entries), 3) if entries else None,
        "decisions": entries,
    })


@operator_bp.get("/items/<uuid>")
def item_inspector(uuid: str):
    """Single-fact inspector behind the console's unique item URLs.

    /console/item?uuid=soil_… fetches this: the live fact (any historical
    uuid resolves forward through the supersedes chain), its scopes, and its
    attachment family (contained_by children across the whole chain).
    Facts only for now — entity inspection is future work.
    """
    _authenticate_jwt()

    def _loads(value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    if uuid.startswith("core_"):
        return jsonify({"error": "entity inspection is not supported yet; "
                         "facts only"}), 404
    bare = uuid.removeprefix("soil_")

    # Follow the supersedes chain forward (relation: source=new, target=old)
    # until the live version, collecting every member for family lookup.
    with get_soil() as soil:
        conn = soil._get_connection()
        live = f"soil_{bare}"
        chain = [live]
        seen = {live}
        while True:
            nxt = conn.execute(
                "SELECT source FROM system_relation "
                "WHERE kind = 'supersedes' AND target = ?", (live,)
            ).fetchone()
            if nxt is None or nxt["source"] in seen:
                break
            live = nxt["source"]
            seen.add(live)
            chain.append(live)
        row = conn.execute(
            """SELECT uuid, _type, realized_at, canonical_at, data, metadata,
                      superseded_by
               FROM item WHERE uuid = ? AND superseded_by IS NULL""",
            (live,),
        ).fetchone()
        if row is None:
            return jsonify({"error": "item not found", "uuid": live}), 404
        # Family = contained_by children of ANY chain version, collapsed to
        # their live versions (relations pin to capture-time versions —
        # system#11 — so filtering live-only children before collapsing
        # drops every amended attachment; 2026-11-22 inspector bug).
        child_rows = conn.execute(
            f"""SELECT DISTINCT rel.source FROM system_relation rel
                WHERE rel.kind = 'contained_by'
                  AND rel.target IN ({",".join("?" * len(chain))})""",
            chain,
        ).fetchall()
        sup_map = _load_sup_map()
        live_children = []
        seen_children = set()
        for (child,) in child_rows:
            c = child
            cseen = {c}
            while c in sup_map and sup_map[c] not in cseen:
                c = sup_map[c]
                cseen.add(c)
            if c not in seen_children:
                seen_children.add(c)
                live_children.append(c)
        # parents / references: contained_by (the container) and replies_to
        # (thread parent) — collapsed to live versions so "browse up" always
        # lands on the current item.
        parent_links = []
        parent_uuids = []
        for kind in ("contained_by", "replies_to"):
            prows = conn.execute(
                f"""SELECT DISTINCT rel.target FROM system_relation rel
                    WHERE rel.kind = '{kind}'
                      AND rel.source IN ({",".join("?" * len(chain))})""",
                chain,
            ).fetchall()
            for (target,) in prows:
                t = target
                tseen = {t}
                while t in sup_map and sup_map[t] not in tseen:
                    t = sup_map[t]
                    tseen.add(t)
                if t not in parent_uuids:
                    parent_uuids.append(t)
                    parent_links.append({"uuid": t, "kind": kind})
        parents = []
        if parent_uuids:
            prows = conn.execute(
                f"""SELECT uuid, _type, canonical_at, data, metadata FROM item
                    WHERE superseded_by IS NULL
                      AND uuid IN ({",".join("?" * len(parent_uuids))})""",
                parent_uuids,
            ).fetchall()
        else:
            prows = []
        by_uuid = {r["uuid"]: r for r in prows}
        for link in parent_links:
            r = by_uuid.get(link["uuid"])
            if r is None:
                continue
            d = _loads(r["data"])
            parents.append({
                "uuid": uid.add_soil_prefix(r["uuid"]),
                "kind": link["kind"],
                "type": r["_type"],
                "title": (d.get("title") or d.get("subject")
                          or d.get("filename") or r["uuid"])[:160],
                "scopes": (_loads(r["metadata"]) or {}).get("scopes", []),
            })

        att_rows = []
        if live_children:
            att_rows = conn.execute(
                f"""SELECT uuid, _type, canonical_at, data, metadata
                    FROM item WHERE superseded_by IS NULL
                      AND uuid IN ({",".join("?" * len(live_children))})
                    ORDER BY canonical_at ASC""",
                live_children,
            ).fetchall()

    item = {
        "uuid": uid.add_soil_prefix(row["uuid"]),
        "type": row["_type"],
        "realized_at": row["realized_at"],
        "canonical_at": row["canonical_at"],
        "data": _loads(row["data"]),
        "metadata": _loads(row["metadata"]) or {},
    }
    attachments = [
        {
            "uuid": uid.add_soil_prefix(a["uuid"]),
            "type": a["_type"],
            "canonical_at": a["canonical_at"],
            "data": _loads(a["data"]),
            "metadata": _loads(a["metadata"]) or {},
        }
        for a in att_rows
    ]
    return jsonify({
        "item": item,
        "chain": [uid.add_soil_prefix(c) for c in chain],
        "attachments": attachments,
        "parents": parents,
    })


# Semantic item search (2026-11-22): the query embedding runs in the model
# layer's LAZY daemon (~/memogarden/triage-model/embed_daemon.py — unix
# socket, spawned on first use, idle self-exit). The serving process never
# loads torch (architecture decision, sysinfo incident 2026-11-22); if the
# daemon cannot start, /items/search degrades to keyword mode.
_EMBED_SOCK = Path.home() / ".local/share/memogarden/triage-model/embed.sock"
_TRIAGE_MODEL = Path(os.environ.get(
    "MEMOGARDEN_TRIAGE_MODEL_DIR",
    str(Path.home() / "memogarden/triage-model")))
_INDEXED_TYPES = ("Email", "Attachment", "Note", "Entry", "Event")


def _daemon_search(q: str, scope_uuids, types, k: int):
    """Ask the embed daemon; spawn it if the socket isn't listening.

    Returns results or None (daemon unavailable → caller falls back to
    keyword mode).
    """
    import socket
    import subprocess

    payload = json.dumps({"op": "search", "q": q, "scopes": scope_uuids,
                          "types": types, "k": k}).encode()
    spawned = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect(str(_EMBED_SOCK))
            s.sendall(payload)
            buf = b""
            while True:
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            s.close()
            out = json.loads(buf)
            return out.get("results") if "results" in out else None
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout,
                json.JSONDecodeError):
            if not spawned:
                # Popen dups the fds onto the child — close the parent's
                # copies immediately or the handle leaks per spawn
                with open(_TRIAGE_MODEL / "embed-daemon.log", "ab") as log:
                    subprocess.Popen(
                        [str(_TRIAGE_MODEL / ".venv/bin/python"),
                         str(_TRIAGE_MODEL / "embed_daemon.py")],
                        cwd=str(_TRIAGE_MODEL), stdout=log, stderr=log,
                        start_new_session=True)
                spawned = True
            time.sleep(0.5)
    return None


@operator_bp.get("/items/search")
def items_search():
    """Browse + keyword/semantic item search for the console Items tab.

    ?q= text query (semantic when the embed daemon is reachable, keyword
    LIKE fallback otherwise); ?scope= label or uuid filter; ?type= comma
    list of fact types; ?limit / ?offset for browse and keyword modes.
    Semantic mode ignores offset (top-k by cosine).
    """
    _authenticate_jwt()

    def _loads(value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    q = (request.args.get("q") or "").strip()
    scope_ref = (request.args.get("scope") or "").strip()
    types = [t.strip() for t in (request.args.get("type") or "").split(",")
             if t.strip()]
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        raise ValidationError(
            "limit/offset must be integers",
            {"limit": request.args.get("limit"),
             "offset": request.args.get("offset")},
        ) from None

    # resolve scope filter (label, alias, or uuid) to a scope uuid set
    scope_uuids = None
    if scope_ref:
        with get_core() as core_conn:
            scope_rows = core_conn._conn.execute(
                "SELECT uuid, data FROM entity WHERE type = 'Scope' "
                "AND superseded_by IS NULL").fetchall()
        wanted = scope_ref.removeprefix("core_")
        scope_uuids = []
        for r in scope_rows:
            label = (_loads(r["data"]) or {}).get("label") or ""
            if wanted in (r["uuid"], label):
                scope_uuids.append(r["uuid"])
        if not scope_uuids:
            raise ValidationError("unknown scope", {"scope": scope_ref})

    filtered_types = [t for t in types if t in _INDEXED_TYPES] or list(
        _INDEXED_TYPES)

    mode = "recent"
    scored = []
    if q:
        daemon = _daemon_search(q, scope_uuids, filtered_types, limit)
        if daemon is not None:
            mode = "semantic"
            scored = daemon
        else:
            mode = "keyword_fallback"

    with get_soil() as soil:
        conn = soil._get_connection()
        where = ["superseded_by IS NULL", f"_type IN ({','.join('?' * len(filtered_types))})"]
        params: list = list(filtered_types)
        if scope_uuids:
            where.append("""EXISTS (SELECT 1 FROM json_each(metadata, '$.scopes') je
                            WHERE je.value IN (%s))""" %
                         ",".join("?" * len(scope_uuids)))
            params.extend(scope_uuids)
        results = []
        if mode in ("semantic",):
            hits = scored[offset:offset + limit] if offset else scored[:limit]
            uuids = [u for u, _ in hits]
            scores = {u: s for u, s in hits}
            if uuids:
                rows = conn.execute(
                    f"""SELECT uuid, _type, canonical_at, data, metadata
                        FROM item WHERE superseded_by IS NULL
                          AND uuid IN ({",".join("?" * len(uuids))})""",
                    uuids,
                ).fetchall()
                by_uuid = {r["uuid"]: r for r in rows}
                for u in uuids:
                    r = by_uuid.get(u)
                    if r is None:
                        continue
                    d = _loads(r["data"])
                    results.append({
                        "uuid": uid.add_soil_prefix(r["uuid"]),
                        "type": r["_type"],
                        "title": (d.get("title") or d.get("subject")
                                  or d.get("filename") or "")[:160],
                        "canonical_at": r["canonical_at"],
                        "scopes": (_loads(r["metadata"]) or {}).get("scopes", []),
                        "score": scores.get(u),
                    })
            return jsonify({"mode": mode, "q": q, "results": results,
                            "offset": offset, "limit": limit})

        # recent / keyword_fallback: SQL-ordered
        if q:
            mode = "keyword_fallback"
            like = f"%{q}%"
            where.append(
                """(json_extract(data, '$.title') LIKE ? COLLATE NOCASE
                    OR json_extract(data, '$.subject') LIKE ? COLLATE NOCASE
                    OR json_extract(data, '$.filename') LIKE ? COLLATE NOCASE
                    OR json_extract(data, '$.description') LIKE ? COLLATE NOCASE)""")
            params.extend([like] * 4)
        rows = conn.execute(
            f"""SELECT uuid, _type, canonical_at, data, metadata FROM item
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(canonical_at, realized_at) DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        for r in rows:
            d = _loads(r["data"])
            results.append({
                "uuid": uid.add_soil_prefix(r["uuid"]),
                "type": r["_type"],
                "title": (d.get("title") or d.get("subject")
                          or d.get("filename") or "")[:160],
                "canonical_at": r["canonical_at"],
                "scopes": (_loads(r["metadata"]) or {}).get("scopes", []),
            })
    return jsonify({"mode": mode, "q": q, "results": results,
                    "offset": offset, "limit": limit})


@operator_bp.get("/scopes/overview")
def scopes_overview():
    """Authority-state overview: scopes, item counts, grants, policies (RFC-015 §3.3).

    One read for the console's Scopes & grants screen: every scope with
    its labeled-item counts (facts + entities), every live key with its
    grants (and the agent it binds to), every provider's scope policy,
    and every account's per-account scope_policy override (RFC-012
    pattern; null = the account inherits its provider's policy).

    Returns:
        200: {"enforcement": bool, "scopes": [...], "grants": [...],
              "provider_policies": {name: policy},
              "account_policies": {provider: [{id, label, subject,
              scope_policy}]}}
    """
    _authenticate_jwt()

    core = get_core()

    scope_rows = core._conn.execute(
        "SELECT uuid, data, created_at FROM entity "
        "WHERE type = 'Scope' AND superseded_by IS NULL ORDER BY created_at ASC"
    ).fetchall()

    with get_soil() as soil:
        fact_counts = {
            r["scope_uuid"]: r["n"]
            for r in soil._get_connection().execute(_FACT_SCOPES_SQL).fetchall()
        }
    entity_counts: dict = {}
    for r in core._conn.execute(_ENTITY_SCOPES_SQL).fetchall():
        entity_counts.setdefault(r["scope_uuid"], {})[r["entity_type"]] = r["n"]

    scopes = []
    for row in scope_rows:
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        scopes.append({
            "uuid": uid.add_core_prefix(row["uuid"]),
            "label": data.get("label"),
            "description": data.get("description"),
            "visibility": data.get("visibility") or "private",
            "created_at": row["created_at"],
            "fact_count": fact_counts.get(row["uuid"], 0),
            "entity_counts": entity_counts.get(row["uuid"], {}),
        })

    # Live keys + grants, with agent attribution resolved to labels.
    key_rows = core._conn.execute(
        """SELECT k.id, k.name, k.key_prefix, k.agent_entity_id, k.grants,
                  k.last_seen, k.revoked_at, a.data AS agent_data
           FROM api_keys k
           LEFT JOIN entity a ON a.uuid = k.agent_entity_id
           WHERE k.revoked_at IS NULL
           ORDER BY k.created_at ASC"""
    ).fetchall()
    grants = []
    for row in key_rows:
        raw_grants = row["grants"]
        if isinstance(raw_grants, str):
            raw_grants = json.loads(raw_grants)
        agent_data = row["agent_data"]
        if isinstance(agent_data, str):
            agent_data = json.loads(agent_data) if agent_data else {}
        grants.append({
            "key_id": uid.add_core_prefix(row["id"]),
            "key_label": row["name"],
            "key_prefix": row["key_prefix"],
            "agent_entity_id": uid.add_core_prefix(row["agent_entity_id"])
            if row["agent_entity_id"] else None,
            "agent_label": (agent_data or {}).get("label"),
            "last_seen": row["last_seen"],
            "grants": raw_grants or [],
        })

    policy_rows = core._conn.execute(
        "SELECT name, scope_policy FROM providers ORDER BY name ASC"
    ).fetchall()
    policies = {}
    for row in policy_rows:
        raw = row["scope_policy"]
        policies[row["name"]] = (
            json.loads(raw) if isinstance(raw, str) and raw else (raw or None)
        )

    # Per-account overrides (RFC-012 pattern): NULL = inherit provider policy.
    account_rows = core._conn.execute(
        "SELECT provider_name, id, label, subject, scope_policy "
        "FROM provider_accounts ORDER BY provider_name, label, subject ASC"
    ).fetchall()
    account_policies: dict = {}
    for row in account_rows:
        raw = row["scope_policy"]
        account_policies.setdefault(row["provider_name"], []).append({
            "id": row["id"],
            "label": row["label"],
            "subject": row["subject"],
            "scope_policy": (
                json.loads(raw) if isinstance(raw, str) and raw else (raw or None)
            ),
        })

    return jsonify({
        "enforcement": enforcement_enabled(),
        "scopes": scopes,
        "grants": grants,
        "provider_policies": policies,
        "account_policies": account_policies,
    })
