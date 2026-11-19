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

from flask import Blueprint, jsonify, request

from system.core import get_core
from system.exceptions import ValidationError
from system.soil import get_soil
from utils import uid

from ..middleware.decorators import _authenticate_jwt
from ..middleware.scope_access import enforcement_enabled

logger = logging.getLogger(__name__)

operator_bp = Blueprint("operator", __name__)

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
    with get_soil() as soil:
        conn = soil._get_connection()
        facts = conn.execute(
            f"""SELECT uuid, _type, realized_at, canonical_at, data, metadata
                FROM item WHERE {fact_where}
                ORDER BY realized_at DESC LIMIT ? OFFSET ?""",
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

    core = get_core()
    entities = core._conn.execute(
        f"""SELECT uuid, type, created_at, updated_at, data
            FROM entity WHERE {entity_where}
            ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
        (*entity_params, limit, offset),
    ).fetchall()
    total_entities = core._conn.execute(
        f"SELECT COUNT(*) AS n FROM entity WHERE {entity_where}", entity_params
    ).fetchone()["n"]

    def _loads(value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    return jsonify({
        "scope": {
            "uuid": uid.add_core_prefix(scope_uuid),
            "data": _loads(scope["data"]),
            "created_at": scope["created_at"],
        },
        "facts": [
            {
                "uuid": uid.add_soil_prefix(r["uuid"]),
                "type": r["_type"],
                "realized_at": r["realized_at"],
                "data": _loads(r["data"]),
                "metadata": _loads(r["metadata"]) or {},
            }
            for r in facts
        ],
        "entities": [
            {
                "uuid": uid.add_core_prefix(r["uuid"]),
                "type": r["type"],
                "updated_at": r["updated_at"],
                "data": _loads(r["data"]),
            }
            for r in entities
        ],
        "total_facts": total_facts,
        "total_entities": total_entities,
    })


@operator_bp.get("/scopes/overview")
def scopes_overview():
    """Authority-state overview: scopes, item counts, grants, policies (RFC-015 §3.3).

    One read for the console's Scopes & grants screen: every scope with
    its labeled-item counts (facts + entities), every live key with its
    grants (and the agent it binds to), and every provider's scope policy.

    Returns:
        200: {"enforcement": bool, "scopes": [...], "grants": [...],
              "provider_policies": {name: policy}}
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

    return jsonify({
        "enforcement": enforcement_enabled(),
        "scopes": scopes,
        "grants": grants,
        "provider_policies": policies,
    })
