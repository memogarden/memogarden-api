"""Core bundle verb handlers.

Implements the Core bundle verbs from RFC-005 v7:
- create: Create entity (any type)
- edit: Edit entity (set/unset semantics)
- forget: Soft delete entity
- get: Get entity by UUID
- query: Query entities with filters

Session 1: Baseline entity types only (Transaction, Recurrence, etc.)

Session 20B: Event Integration
Publishes SSE events for context and scope operations:
- context_updated: Published when ContextFrame containers change
- frame_updated: Published when participant's head_item_uuid changes
- scope_created: Published when new Scope entity is created
- scope_modified: Published when Scope entity data changes
"""

import json
import logging

from system.core import get_core
from system.exceptions import PermissionDenied, ResourceNotFound, ValidationError
from utils import uid

from ..middleware.scope_access import (
    active_scope_for,
    enforcement_enabled,
    filter_visible,
    item_visible,
    normalize_scopes,
    public_scope_uuids,
    require_scope_grant,
    requester_has_all_scopes,
)
from ..series import series_key, series_members

from ..schemas.semantic import (
    CreateRequest,
    EditRequest,
    EnterRequest,
    ExploreRequest,
    FocusRequest,
    ForgetRequest,
    GetRequest,
    LeaveRequest,
    LinkRequest,
    QueryRelationRequest,
    QueryRequest,
    SearchRequest,
    TrackRequest,
    UnlinkRequest,
)
from .decorators import with_audit

from ..events import (
    publish_context_updated,
    publish_frame_updated,
    publish_event,
)

logger = logging.getLogger(__name__)

# Baseline entity types that can be created via Semantic API
# Session 1: These are the types defined in memogarden/schemas/types/entities/
# Session 18: Added ConversationLog for Project Studio
# Session 24: Added Scope for Project Studio scope management
# RFC-010: Agent/Operator are NOT creatable here — identity issuance is
# explicit and operator-gated via the management endpoints (POST /agents,
# JWT-only). Agents were never functional through this path; they are
# now explicitly blocked so no key can mint or mutate identity.
BASELINE_ENTITY_TYPES = {
    "Transaction",
    "Recurrence",
    # The request entity (tada ontology; schemas/task.yaml) — agents file
    # requests via /mg create; providers bypass this whitelist.
    "Task",
    "Artifact",
    "Label",
    "Entity",  # Generic entity type
    "ConversationLog",  # Session 18: Project Studio conversation
    "Scope",  # Session 24: Project Studio scope management
}

# RFC-010 INV-1: identity entity types cannot be created, edited, or
# forgotten through the Semantic API — only through JWT-gated management
# endpoints. The auth hot path stays read-only over identity state.
IDENTITY_ENTITY_TYPES = {"Agent", "Operator"}


# ============================================================================
# Helper Functions
# ============================================================================

def _add_core_prefix(entity: dict) -> dict:
    """Add core_ prefix to entity UUID fields.

    Args:
        entity: Entity dict from Core API (has parsed JSON data)

    Returns:
        Entity dict with prefixed UUIDs
    """
    result = entity.copy()

    # Add core_ prefix to UUID fields
    result["uuid"] = uid.add_core_prefix(entity["uuid"])

    if entity.get("superseded_by"):
        result["superseded_by"] = uid.add_core_prefix(entity["superseded_by"])

    if entity.get("group_id"):
        result["group_id"] = uid.add_core_prefix(entity["group_id"])

    if entity.get("derived_from"):
        result["derived_from"] = uid.add_core_prefix(entity["derived_from"])

    return result


def _row_to_entity_response(row, entity_type: str = "Entity") -> dict:
    """Convert a database row to entity response dict.

    Adds core_ prefix to UUIDs and includes all entity fields.

    Note: This is kept for backward compatibility but _add_core_prefix
    should be used for dicts returned by Core API.
    """
    entity_uuid = row["uuid"]

    # Parse JSON data if present
    # sqlite3.Row doesn't have .get(), use direct access with exception handling
    try:
        data_value = row["data"]
        data = json.loads(data_value) if data_value else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        data = {}

    # Helper to safely get optional values from sqlite3.Row
    def safe_get(key: str, default: str | None = None) -> str | None:
        try:
            val = row[key]
            return val if val is not None else default
        except (KeyError, IndexError):
            return default

    # Get optional UUID fields and add prefix if present
    superseded_by = safe_get("superseded_by")
    group_id = safe_get("group_id")
    derived_from = safe_get("derived_from")

    return {
        "uuid": uid.add_core_prefix(entity_uuid),
        "type": row["type"],
        "data": data,
        # Hash chain fields
        "hash": row["hash"],
        "previous_hash": safe_get("previous_hash"),
        "version": row["version"],
        # Entity metadata
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "superseded_by": uid.add_core_prefix(superseded_by) if superseded_by else None,
        "superseded_at": safe_get("superseded_at"),
        "group_id": uid.add_core_prefix(group_id) if group_id else None,
        "derived_from": uid.add_core_prefix(derived_from) if derived_from else None,
    }


# ============================================================================
# Verb Handlers
# ============================================================================

@with_audit
def handle_create(request: CreateRequest, actor: str) -> dict:
    """Handle create verb - create a new entity.

    Session 1: Supports baseline entity types only.
    Future: Will support registered custom schemas.

    Session 20B: Publishes scope_created SSE event for Scope entities.

    Args:
        request: Validated CreateRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with created entity data
    """
    # Validate entity type is in baseline
    if request.type not in BASELINE_ENTITY_TYPES:
        if request.type in IDENTITY_ENTITY_TYPES:
            # RFC-010 INV-1: identity issuance is operator-gated
            raise PermissionDenied(
                f"Entity type '{request.type}' cannot be created via the "
                f"Semantic API. Identity issuance is operator-gated: "
                f"mint agents via POST /agents (JWT only). (RFC-010)"
            )
        raise ValueError(
            f"Entity type '{request.type}' not supported. "
            f"Baseline types: {', '.join(sorted(BASELINE_ENTITY_TYPES))}. "
            f"Custom schema registration not yet implemented."
        )

    # RFC-011 v1.2 (D1): scopes are security boundaries — creation is
    # operator-gated (RFC-010's issuance invariant applied to boundaries).
    # Operator JWT sessions mint via the same verb under sovereignty.
    # Not flag-gated: issuance governance is unconditional, like identity.
    if request.type == "Scope" and not requester_has_all_scopes():
        raise PermissionDenied(
            "Scope creation is operator-gated: scopes are security "
            "boundaries. Ask the operator to mint the scope. (RFC-011 v1.2 D1)",
            {"type": "Scope", "code": "scope_mint_denied"},
        )

    # Use transaction for entity creation and fetch
    with get_core() as core:
        entity_uuid = core.entity.create(
            entity_type=request.type,
            data=request.data if request.data else {},
        )

        # Fetch created entity (now returns dict with parsed JSON)
        entity = core.entity.get_by_id(entity_uuid)

        # Session 20B: Publish SSE event for Scope creation
        if request.type == "Scope":
            publish_event(
                "scope_created",
                {
                    "scope_uuid": uid.add_core_prefix(entity_uuid),
                    "label": request.data.get("label", ""),
                    "actor": actor,
                },
                scope_uuid=uid.add_core_prefix(entity_uuid),
            )

        return _add_core_prefix(entity)


@with_audit
def handle_get(request: GetRequest, actor: str) -> dict:
    """Handle get verb - get entity by UUID.

    Supports prefixed and non-prefixed UUIDs.
    Determines target type from UUID prefix.

    Args:
        request: Validated GetRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with entity data
    """
    with get_core() as core:
        entity = core.entity.get_by_id(request.target)

        # RFC-011 §4.9(2): entity reads enforce §4.5 like fact reads.
        # READ semantics — public-visibility labels relax this gate
        # (most permissive wins); writes elsewhere stay frame-gated.
        if enforcement_enabled() and not requester_has_all_scopes():
            entity_scopes = (entity.get("data") or {}).get("scopes") or []
            if not item_visible(
                entity_scopes, active_scope_for(actor), public_scope_uuids()
            ):
                raise PermissionDenied(
                    "Entity is in a scope you are not inside",
                    {"target": request.target, "item_scopes": entity_scopes,
                     "code": "scope_read_denied"},
                )

        return _add_core_prefix(entity)


@with_audit
def handle_edit(request: EditRequest, actor: str) -> dict:
    """Handle edit verb - edit entity with set/unset semantics.

    Session 1: For baseline types, edits update the entity.data JSON field.
    Domain-specific tables (e.g., transaction) are NOT updated yet.

    set: handles both add-new and update-existing
    unset: removes fields from entity.data

    Session 20B: Publishes scope_modified SSE event for Scope entities.

    Args:
        request: Validated EditRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with updated entity data
    """
    entity_id = uid.strip_prefix(request.target)

    with get_core() as core:
        # RFC-010 INV-1: identity guard BEFORE lookup — Agent uuids are
        # stored core_-prefixed, so the stripped lookup below would 404
        # on them and the guard would never run
        canonical = uid.add_core_prefix(request.target)
        identity_row = core._conn.execute(
            "SELECT type FROM entity WHERE uuid = ?", (canonical,)
        ).fetchone()
        if identity_row and identity_row["type"] in IDENTITY_ENTITY_TYPES:
            raise PermissionDenied(
                f"Entity type '{identity_row['type']}' cannot be edited via the "
                f"Semantic API (RFC-010 identity protection)"
            )

        # RFC-011 D1 extended (RFC-015 review finding): a Scope's label IS
        # the boundary vocabulary — editing it is shape-governing, the same
        # authority minting requires. Unconditional, like the minting gate.
        # RFC-011 D1 extended (RFC-015 review finding): a Scope's label IS
        # the boundary vocabulary — editing it is shape-governing, the same
        # authority minting requires. Unconditional, like the minting gate.
        # Lookup BOTH stored forms: management-endpoint rows (agents/keys)
        # carry prefixed uuids, core.entity.create rows (scopes) are plain.
        identity_row = core._conn.execute(
            "SELECT type FROM entity WHERE uuid IN (?, ?)",
            (canonical, entity_id),
        ).fetchone()
        if identity_row and identity_row["type"] == "Scope" and not requester_has_all_scopes():
            raise PermissionDenied(
                "Scope entities are operator-governed — mint, edit, and forget "
                "all require an operator session (RFC-011 D1)",
                {"scope": canonical, "code": "scope_governance_denied"},
            )

        # Get current entity state (Core API returns dict with parsed JSON data)
        current = core.entity.get_by_id(entity_id)

        # Data is already a dict (parsed by Core API)
        current_data = current.get("data") or {}

        # RFC-011 §4.9(2): entity WRITES enforce like fact writes (the
        # amend precedent) — strict frame gate, no public-visibility
        # short-circuit: visibility relaxes reads only.
        old_labels = set(current_data.get("scopes") or [])
        if enforcement_enabled() and not requester_has_all_scopes():
            if not item_visible(sorted(old_labels), active_scope_for(actor)):
                raise PermissionDenied(
                    "Entity is in a scope you are not inside",
                    {"target": request.target, "item_scopes": sorted(old_labels),
                     "code": "scope_write_denied"},
                )

        # Apply set operations
        if request.set:
            # RFC-011: entity scope labels live in data.scopes as plain
            # uuids (DB convention) — normalize at the boundary exactly
            # like handle_amend does for metadata.scopes, so prefixed
            # labels from callers never wedge the scope predicates
            # (a prefixed label silently matches nothing).
            if "scopes" in request.set:
                request.set["scopes"] = normalize_scopes(request.set["scopes"] or [])
            current_data.update(request.set)

        # RFC-011 v1.2 (D2) for entities: symmetric label grants on the
        # edit's relabel channel — a label added OR removed requires the
        # grant on that scope, removal being the exposure-widening
        # direction. Ungated previously (the decorated-not-protected gap).
        if enforcement_enabled() and not requester_has_all_scopes():
            if request.unset and "scopes" in request.unset:
                new_labels: set = set()
            else:
                new_labels = set(current_data.get("scopes") or [])
            for changed in old_labels.symmetric_difference(new_labels):
                require_scope_grant(changed)

        # Apply unset operations
        if request.unset:
            for field in request.unset:
                current_data.pop(field, None)

        # Update entity data and hash via Core API
        core.entity.update_data(entity_id, current_data)

        # Fetch updated entity
        row = core.entity.get_by_id(entity_id)

        # api#94: recurring-series scope cascade — a scope decision on one
        # instance of a series is a decision about the series (contact
        # birthdays ingest as 70–90 identical occurrences; per-instance
        # triage is untenable). Applies when the edit touched scopes and the
        # target carries a series key (recurring_event_id / ical_uid);
        # cascade_series: false opts out for an instance-only override.
        # Same transaction as the primary edit — get_core rolls back on
        # exception, so a mid-sweep failure leaves nothing changed.
        scopes_touched = (
            (request.set is not None and "scopes" in request.set)
            or (request.unset is not None and "scopes" in request.unset)
        )
        if request.cascade_series and scopes_touched:
            key = series_key(row.get("data") or {})
            if key is not None:
                new_scopes = list((row.get("data") or {}).get("scopes") or [])
                for sibling in series_members(core, row["type"], key,
                                              exclude_uuid=row["uuid"]):
                    s_data = sibling["data"]
                    if isinstance(s_data, str):
                        s_data = json.loads(s_data)
                    old_labels = set(s_data.get("scopes") or [])
                    if enforcement_enabled() and not requester_has_all_scopes():
                        # Same symmetric label gate as the primary edit
                        # (RFC-011 D2): the sweep relabels instances too, so
                        # each changed label needs the caller's grant.
                        for changed in old_labels.symmetric_difference(set(new_scopes)):
                            require_scope_grant(changed)
                    s_data["scopes"] = new_scopes
                    core.entity.update_data(sibling["uuid"], s_data)

        # Session 20B: Publish SSE event for Scope modification
        if row.get("type") == "Scope":
            publish_event(
                "scope_modified",
                {
                    "scope_uuid": uid.add_core_prefix(entity_id),
                    "actor": actor,
                },
                scope_uuid=uid.add_core_prefix(entity_id),
            )

        return _add_core_prefix(row)


@with_audit
def handle_forget(request: ForgetRequest, actor: str) -> dict:
    """Handle forget verb - soft delete entity via supersession.

    Creates a tombstone entity and marks the original as superseded.

    Args:
        request: Validated ForgetRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with forgotten entity data
    """
    entity_id = uid.strip_prefix(request.target)

    with get_core() as core:
        # RFC-010: identity guard before lookup (see handle_edit) — Agent
        # entities are stored core_-prefixed and are never deleted (§4.3;
        # retirement is key revocation today, supersession via management later)
        canonical = uid.add_core_prefix(request.target)
        identity_row = core._conn.execute(
            "SELECT type FROM entity WHERE uuid = ?", (canonical,)
        ).fetchone()
        if identity_row and identity_row["type"] in IDENTITY_ENTITY_TYPES:
            raise PermissionDenied(
                f"Entity type '{identity_row['type']}' cannot be forgotten "
                f"(RFC-010 identity protection)"
            )

        # RFC-011 D1 extended (RFC-015 review finding): deleting a scope is
        # boundary-shaping — same operator-gated authority as minting.
        # Both stored forms, as in handle_edit (scopes are stored plain).
        identity_row = core._conn.execute(
            "SELECT type FROM entity WHERE uuid IN (?, ?)",
            (canonical, entity_id),
        ).fetchone()
        if identity_row and identity_row["type"] == "Scope" and not requester_has_all_scopes():
            raise PermissionDenied(
                "Scope entities are operator-governed — mint, edit, and forget "
                "all require an operator session (RFC-011 D1)",
                {"scope": canonical, "code": "scope_governance_denied"},
            )

        # Verify entity exists (+ RFC-011 §4.9(2) write gate: forgetting a
        # scoped entity is a write on it — strict frame gate, edit/amend
        # precedent; no public-visibility short-circuit on writes)
        current = core.entity.get_by_id(entity_id)
        if enforcement_enabled() and not requester_has_all_scopes():
            forget_scopes = (current.get("data") or {}).get("scopes") or []
            if not item_visible(forget_scopes, active_scope_for(actor)):
                raise PermissionDenied(
                    "Entity is in a scope you are not inside",
                    {"target": request.target, "item_scopes": forget_scopes,
                     "code": "scope_write_denied"},
                )

        # Create tombstone entity
        tombstone_id = core.entity.create(entity_type="Tombstone")

        # Mark original as superseded
        core.entity.supersede(entity_id, tombstone_id)

        # Return the original entity (now superseded)
        row = core.entity.get_by_id(entity_id)

        return _add_core_prefix(row)


@with_audit
def handle_query(request: QueryRequest, actor: str) -> dict:
    """Handle query verb - query entities with filters.

    Session 1: Basic equality filters only.
    Filters are applied to entity.type and JSON data fields (#27): the
    documented `{"_type": T}` filter resolves as the type filter; every
    other key is a data-field equality match (find_by_data's path rule —
    [A-Za-z0-9_.]+, dotted segments = nested paths). All filters are
    WHERE-level, so filtering happens BEFORE pagination.

    Args:
        request: Validated QueryRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with query results (results, total, start_index, count)
    """
    with get_core() as core:
        entity_type = request.type
        data_filters: dict = {}
        for field, value in (request.filters or {}).items():
            if field == "_type":
                if entity_type and entity_type != value:
                    raise ValidationError(
                        "conflicting type filters: 'type' and filters._type disagree",
                        {"type": entity_type, "filters._type": value},
                    )
                entity_type = entity_type or value
                continue
            if isinstance(value, (dict, list)):
                raise ValidationError(
                    f"filter '{field}' must be a scalar (equality matching only)",
                    {"field": field},
                )
            data_filters[field] = value

        # Use public API method to query entities
        try:
            rows, total = core.entity.query_with_filters(
                entity_type=entity_type,
                include_superseded=False,
                limit=request.count,
                offset=request.start_index,
                data_filters=data_filters or None,
            )
        except ValueError as e:
            # Bad data-field path (house rule shared with find_by_data)
            raise ValidationError(str(e), {"filters": request.filters}) from e

        # Convert rows to response format (already dicts with parsed JSON)
        results = [_add_core_prefix(entity) for entity in rows]

        # RFC-011 §4.9(2): query results are visibility-filtered (SG-10),
        # mirroring the fact query handler: WHERE-level data filters run
        # before pagination; the visible-set exclusion runs after fetch —
        # `total` reflects the fetched set, `count` reflects what is
        # actually returned (accepted cardinality posture).
        results = filter_visible(
            results, actor, lambda r: (r.get("data") or {}).get("scopes")
        )

        return {
            "results": results,
            "total": total,
            "start_index": request.start_index,
            "count": len(results),
        }


@with_audit
def handle_link(request: LinkRequest, actor: str) -> dict:
    """Handle link verb - create user relation with time horizon (RFC-002).

    Creates a user relation (engagement signal) with an initial time horizon.
    The relation will decay over time based on access patterns.

    Session 20B: Publishes relation_created SSE event.

    Args:
        request: Validated LinkRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with created relation details

    Raises:
        ValueError: If relation kind is invalid
    """
    with get_core() as core:
        # Create the user relation
        relation_uuid = core.relation.create(
            kind=request.kind,
            source=request.source,
            source_type=request.source_type,
            target=request.target,
            target_type=request.target_type,
            initial_horizon_days=request.initial_horizon_days,
            evidence=request.evidence,
            metadata=request.metadata,
        )

        # Get the created relation
        row = core.relation.get_by_id(relation_uuid)

        # Session 20B: Publish SSE event for relation creation
        publish_event(
            "relation_created",
            {
                "relation_uuid": uid.add_core_prefix(relation_uuid),
                "kind": request.kind,
                "source": uid.add_core_prefix(request.source),
                "target": uid.add_core_prefix(request.target),
                "actor": actor,
            },
            scope_uuid=None,  # Relations are cross-scope, global event
        )

        return {
            "uuid": uid.add_core_prefix(row["uuid"]),
            "kind": row["kind"],
            "source": uid.add_core_prefix(row["source"]),
            "source_type": row["source_type"],
            "target": uid.add_core_prefix(row["target"]),
            "target_type": row["target_type"],
            "time_horizon": row["time_horizon"],
            "last_access_at": row["last_access_at"],
            "created_at": row["created_at"],
        }


# ============================================================================
# Relations Bundle Verb Handlers (RFC-002 v5)
# ============================================================================

@with_audit
def handle_unlink(request: UnlinkRequest, actor: str) -> dict:
    """Handle unlink verb - remove user relation.

    Per RFC-002 v5:
    - unlink: Remove a user relation

    User relations can be removed by the operator who created them.
    System relations are immutable and cannot be unlinked.

    Session 20B: Publishes relation_modified SSE event on unlink.

    Args:
        request: Validated UnlinkRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with deleted relation UUID

    Raises:
        ResourceNotFound: If relation doesn't exist

    SECURITY NOTE: Currently does NOT verify that actor created the relation.
    TODO: Add created_by field to user_relation schema and implement authorization check.
    """
    with get_core() as core:
        # TODO: Add authorization check
        # Verify that actor created this relation before allowing deletion
        # This requires adding created_by field to user_relation table

        # Delete the relation
        core.relation.delete(request.target)

        # Session 20B: Publish SSE event for relation deletion
        publish_event(
            "relation_modified",
            {
                "relation_uuid": uid.add_core_prefix(request.target),
                "action": "deleted",
                "actor": actor,
            },
            scope_uuid=None,  # Relations are cross-scope, global event
        )

        return {
            "uuid": uid.add_core_prefix(request.target),
            "deleted": True
        }


@with_audit
def handle_edit_relation(request: EditRequest, actor: str) -> dict:
    """Handle edit_relation verb - edit relation attributes.

    Per RFC-002 v5:
    - edit_relation: Edit relation attributes (time_horizon, metadata, evidence)

    Session 20B: Publishes relation_modified SSE event on edit.

    Args:
        request: Validated EditRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with updated relation details

    Raises:
        ResourceNotFound: If relation doesn't exist
    """
    with get_core() as core:
        # Edit the relation
        core.relation.edit(
            relation_id=request.target,
            time_horizon=request.set.get("time_horizon") if request.set else None,
            metadata=request.set.get("metadata") if request.set else None,
            evidence=request.set.get("evidence") if request.set else None,
        )

        # Get the updated relation
        row = core.relation.get_by_id(request.target)

        # Session 20B: Publish SSE event for relation edit
        publish_event(
            "relation_modified",
            {
                "relation_uuid": uid.add_core_prefix(request.target),
                "action": "edited",
                "actor": actor,
            },
            scope_uuid=None,  # Relations are cross-scope, global event
        )

        return {
            "uuid": uid.add_core_prefix(row["uuid"]),
            "kind": row["kind"],
            "source": uid.add_core_prefix(row["source"]),
            "source_type": row["source_type"],
            "target": uid.add_core_prefix(row["target"]),
            "target_type": row["target_type"],
            "time_horizon": row["time_horizon"],
            "last_access_at": row["last_access_at"],
            "created_at": row["created_at"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
        }


@with_audit
def handle_get_relation(request: GetRequest, actor: str) -> dict:
    """Handle get_relation verb - get relation by UUID.

    Args:
        request: Validated GetRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with relation details

    Raises:
        ResourceNotFound: If relation doesn't exist
    """
    with get_core() as core:
        row = core.relation.get_by_id(request.target)

        return {
            "uuid": uid.add_core_prefix(row["uuid"]),
            "kind": row["kind"],
            "source": uid.add_core_prefix(row["source"]),
            "source_type": row["source_type"],
            "target": uid.add_core_prefix(row["target"]),
            "target_type": row["target_type"],
            "time_horizon": row["time_horizon"],
            "last_access_at": row["last_access_at"],
            "created_at": row["created_at"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
        }


@with_audit
def handle_query_relation(request: QueryRelationRequest, actor: str) -> dict:
    """Handle query_relation verb - query relations with filters.

    Per RFC-002 v5:
    - query_relation: Query relations with filters

    Args:
        request: Validated QueryRelationRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with results list and count
    """
    with get_core() as core:
        rows = core.relation.query(
            source=request.source,
            target=request.target,
            kind=request.kind,
            source_type=request.source_type,
            target_type=request.target_type,
            alive_only=request.alive_only,
            limit=request.limit,
        )

        results = []
        for row in rows:
            results.append({
                "uuid": uid.add_core_prefix(row["uuid"]),
                "kind": row["kind"],
                "source": uid.add_core_prefix(row["source"]),
                "source_type": row["source_type"],
                "target": uid.add_core_prefix(row["target"]),
                "target_type": row["target_type"],
                "time_horizon": row["time_horizon"],
                "last_access_at": row["last_access_at"],
                "created_at": row["created_at"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
                "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
            })

        return {
            "results": results,
            "count": len(results),
        }


@with_audit
def handle_explore(request: ExploreRequest, actor: str) -> dict:
    """Handle explore verb - graph expansion from anchor.

    Per RFC-002 v5:
    - explore: Graph expansion from anchor entity/fact

    Traverses the relation graph to find connected entities/facts.
    Supports direction control (outgoing, incoming, both) and radius limits.

    Args:
        request: Validated ExploreRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with nodes (entities/facts) and edges (relations)

    Example:
        # Find all entities connected to this one (1-2 hops)
        explore(anchor="entity_xxx", radius=2)
    """
    from system.soil import get_soil

    with get_core() as core:
        # Track visited nodes and edges to avoid duplicates
        visited_nodes = set()
        visited_edges = set()
        nodes = []
        edges = []

        # BFS traversal
        from collections import deque

        # RFC-011 §4.9(2)/(4): graph reads enforce §4.5. The anchor is a
        # direct read and gates like one; edges stay fully listed
        # (containerizing-not-hiding: structure is not concealed), while
        # nodes the requester cannot read are presented as UNRESOLVED
        # REFERENCES — stub shape, truthful count — not omitted silently.
        # Reads honor public-visibility labels; writes elsewhere do not.
        # Flag-off and sovereign callers resolve every node fully —
        # advisory byte-identity and SG-11 both depend on that.
        enforce = enforcement_enabled() and not requester_has_all_scopes()
        if enforce:
            active_scope = active_scope_for(actor)
            public_scopes = public_scope_uuids()
            anchor_plain = uid.strip_prefix(request.anchor)
            anchor_labels = []
            try:
                anchor_row = core.entity.get_by_id(anchor_plain)
                anchor_labels = (anchor_row.get("data") or {}).get("scopes") or []
            except ResourceNotFound:
                with get_soil() as soil:
                    anchor_item = soil.get_fact(anchor_plain)
                if anchor_item is not None:
                    anchor_labels = (anchor_item.metadata or {}).get("scopes") or []
            if not item_visible(anchor_labels, active_scope, public_scopes):
                raise PermissionDenied(
                    "Anchor is in a scope you are not inside",
                    {"anchor": request.anchor, "code": "scope_read_denied"},
                )

        # Queue: (current_uuid, distance)
        queue = deque([(uid.strip_prefix(request.anchor), 0)])

        while queue and len(visited_nodes) < request.limit:
            current_uuid, distance = queue.popleft()

            # Check radius limit
            if request.radius is not None and distance > request.radius:
                break

            # Skip if already visited
            if current_uuid in visited_nodes:
                continue

            visited_nodes.add(current_uuid)

            # Get incoming relations (target = current_uuid)
            if request.direction in ("incoming", "both"):
                incoming_rels = core.relation.list_inbound(
                    current_uuid,
                    alive_only=True
                )
                for rel_row in incoming_rels:
                    if request.kind and rel_row["kind"] != request.kind:
                        continue

                    edge_id = f"{rel_row['uuid']}"
                    if edge_id not in visited_edges:
                        visited_edges.add(edge_id)
                        edges.append({
                            "uuid": uid.add_core_prefix(rel_row["uuid"]),
                            "kind": rel_row["kind"],
                            "source": uid.add_core_prefix(rel_row["source"]),
                            "source_type": rel_row["source_type"],
                            "target": uid.add_core_prefix(rel_row["target"]),
                            "target_type": rel_row["target_type"],
                            "direction": "incoming",
                        })

                        # Add source to queue for next hop
                        source_uuid = rel_row["source"]
                        if source_uuid not in visited_nodes:
                            queue.append((source_uuid, distance + 1))

            # Get outgoing relations (source = current_uuid)
            if request.direction in ("outgoing", "both"):
                outgoing_rels = core.relation.list_outbound(
                    current_uuid,
                    alive_only=True
                )
                for rel_row in outgoing_rels:
                    if request.kind and rel_row["kind"] != request.kind:
                        continue

                    edge_id = f"{rel_row['uuid']}"
                    if edge_id not in visited_edges:
                        visited_edges.add(edge_id)
                        edges.append({
                            "uuid": uid.add_core_prefix(rel_row["uuid"]),
                            "kind": rel_row["kind"],
                            "source": uid.add_core_prefix(rel_row["source"]),
                            "source_type": rel_row["source_type"],
                            "target": uid.add_core_prefix(rel_row["target"]),
                            "target_type": rel_row["target_type"],
                            "direction": "outgoing",
                        })

                        # Add target to queue for next hop
                        target_uuid = rel_row["target"]
                        if target_uuid not in visited_nodes:
                            queue.append((target_uuid, distance + 1))

        # Get node details for all visited nodes
        # Note: This combines both Core entities and Soil items
        for node_uuid in visited_nodes:
            # Try to get from Core first
            try:
                entity_row = core.entity.get_by_id(node_uuid)
                entity_scopes = (entity_row.get("data") or {}).get("scopes") or []
                if enforce and not item_visible(
                    entity_scopes, active_scope, public_scopes
                ):
                    # §4.9(4): unresolved reference, not silent omission
                    nodes.append({
                        "uuid": uid.add_core_prefix(entity_row["uuid"]),
                        "layer": "core",
                        "unresolved": True,
                    })
                    continue
                nodes.append({
                    "uuid": uid.add_core_prefix(entity_row["uuid"]),
                    "layer": "core",
                    "type": entity_row["type"],
                })
                continue
            except ResourceNotFound:
                # Not a Core entity, try Soil
                pass
            except Exception as e:
                # Log unexpected errors
                logger.warning(f"Error looking up Core entity {node_uuid}: {e}")
                pass

            # Try to get from Soil
            try:
                with get_soil() as soil:
                    item = soil.get_fact(node_uuid)
                    if item:
                        item_scopes = (item.metadata or {}).get("scopes") or []
                        if enforce and not item_visible(
                            item_scopes, active_scope, public_scopes
                        ):
                            # §4.9(4): unresolved reference, not silent omission
                            nodes.append({
                                "uuid": uid.add_soil_prefix(item.uuid),
                                "layer": "soil",
                                "unresolved": True,
                            })
                        else:
                            nodes.append({
                                "uuid": uid.add_soil_prefix(item.uuid),
                                "layer": "soil",
                                "type": item._type,
                            })
            except ResourceNotFound:
                # Fact not found in Soil either - skip
                pass
            except Exception as e:
                # Log unexpected errors
                logger.warning(f"Error looking up Soil item {node_uuid}: {e}")
                pass

        return {
            "nodes": nodes,
            "edges": edges,
            "count": len(nodes),
        }


@with_audit
def handle_track(request: TrackRequest, actor: str) -> dict:
    """Handle track verb - trace causal chain from entity back to originating facts.

    Per RFC-005 v7.1:
    - track: Trace entity lineage through derived_from links

    Traces the causal chain showing how an entity was created and what sources
    it was derived from. Handles diamond ancestry naturally (same source referenced
    multiple times).

    Args:
        request: Validated TrackRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with target and chain (tree structure with kind markers)

    Example:
        # Trace all sources for this entity
        track(target="ent_xxx", depth=3)
    """
    from system.soil import get_soil

    # Strip prefix from target UUID
    target_uuid = uid.strip_prefix(request.target)

    with get_core() as core:
        # Get the target entity
        entity_row = core.entity.get_by_id(target_uuid)

        # Build the derivation tree
        # For now, track derived_from links (entity-to-entity derivation)
        # Future: Track through EntityDelta items for fact-level lineage

        visited = set()  # Track visited entities to avoid cycles
        depth_limit = request.depth

        def build_tree(entity_uuid: str, current_depth: int) -> dict:
            """Recursively build derivation tree for an entity."""
            # Check depth limit
            if depth_limit is not None and current_depth >= depth_limit:
                return {
                    "kind": "entity",
                    "id": uid.add_core_prefix(entity_uuid),
                    "sources": [],
                }

            # Avoid cycles
            if entity_uuid in visited:
                return {
                    "kind": "entity",
                    "id": uid.add_core_prefix(entity_uuid),
                    "sources": [],  # Already visited, no sources
                }

            visited.add(entity_uuid)

            # Get entity details
            try:
                entity = core.entity.get_by_id(entity_uuid)
            except ResourceNotFound:
                return {
                    "kind": "entity",
                    "id": uid.add_core_prefix(entity_uuid),
                    "sources": [],  # Entity not found
                }

            sources = []

            # Check for derived_from link (entity-to-entity derivation)
            # sqlite3.Row doesn't have .get(), use direct key access
            try:
                derived_from = entity["derived_from"]
            except (KeyError, IndexError):
                derived_from = None

            if derived_from:
                derived_uuid = uid.strip_prefix(derived_from)
                # Recursively trace the parent entity
                parent_tree = build_tree(derived_uuid, current_depth + 1)
                sources.append(parent_tree)

            # Future: Query EntityDelta items for fact-level sources
            # For now, only track derived_from chain

            return {
                "kind": "entity",
                "id": uid.add_core_prefix(entity_uuid),
                "type": entity["type"],  # Direct access, type is always present
                "sources": sources,
            }

        # Build the tree starting from target
        tree = build_tree(target_uuid, 0)

        return {
            "target": uid.add_core_prefix(target_uuid),
            "chain": [tree],  # Wrap in array for consistent format
        }


# ============================================================================
# Context Verb Handlers (RFC-003 v4)
# ============================================================================

@with_audit
def handle_enter(request: EnterRequest, actor: str) -> dict:
    """Handle enter verb - adopt a scope as the exclusive active scope.

    RFC-003 v5 INV-11a/11b; RFC-011 §4.4. Grant-gated at this layer
    (RFC-011 §4.9 — enforcement reads the presenting key; operator JWT
    sessions bypass per SG-11). Entering while inside another scope is
    an ATOMIC SWITCH (previous_scope in the response); re-entering the
    already-active scope is an idempotent no-op. The @with_audit wrapper
    records the transition as an Action fact.

    Session 20B: publishes context_updated SSE event on enter.

    Args:
        request: Validated EnterRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with scope, previous_scope (on switch), active_scope

    Raises:
        PermissionDenied: Key holds no grant on the scope (RFC-011 SG-4)
    """
    with get_core() as core:
        # Get user's ContextFrame
        context_frame = core.context.get_context_frame(
            owner=actor,
            owner_type="operator",
            create_if_missing=True
        )

        # Strip prefix from scope UUID
        scope_uuid = uid.strip_prefix(request.scope)

        # RFC-011 SG-4: grant-gated entry (denial is audited via with_audit).
        # §8 advisory mode: with enforcement off, entry is advisory —
        # grants gate entry only when the flag is on.
        if enforcement_enabled():
            require_scope_grant(scope_uuid)

        # Snapshot prior scope for the switch record before entering
        previous_scope = context_frame.active_scope

        # Enter the scope (exclusive; atomic switch per INV-11a)
        context_frame = core.context.enter_scope(context_frame, scope_uuid)

        # Session 20B: Publish SSE event for context update
        # Get containers from updated context frame for event payload
        containers = context_frame.containers if hasattr(context_frame, "containers") else []
        publish_context_updated(
            participant_uuid=actor,
            containers=containers,
            scope_uuid=uid.add_core_prefix(scope_uuid),
        )

        return {
            "scope": uid.add_core_prefix(scope_uuid),
            "previous_scope": uid.add_core_prefix(previous_scope) if previous_scope else None,
            "active_scope": uid.add_core_prefix(context_frame.active_scope) if context_frame.active_scope else None,
        }


@with_audit
def handle_leave(request: LeaveRequest, actor: str) -> dict:
    """Handle leave verb - release the active scope.

    RFC-003 v5 INV-11; RFC-011 §4.4. The named scope must be the one
    currently active (SG-3 exclusivity); afterwards the principal
    operates unscoped (global space only, SG-1/SG-5). INV-8 stream
    suspension follows. The @with_audit wrapper records the transition.

    Session 20B: publishes frame_updated and context_updated SSE events.

    Args:
        request: Validated LeaveRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with scope and active_scope (always None on success)

    Raises:
        ValueError: If no ContextFrame exists or the scope is not active
    """
    with get_core() as core:
        try:
            # Get user's ContextFrame (don't create if missing)
            context_frame = core.context.get_context_frame(
                owner=actor,
                owner_type="operator",
                create_if_missing=False
            )
        except Exception as e:
            # Convert to ValueError for consistent 400 response
            if "not found" in str(e).lower():
                raise ValueError("No active context frame. You must enter a scope first.") from None
            raise

        # Strip prefix from scope UUID
        scope_uuid = uid.strip_prefix(request.scope)

        # Leave the scope (must be the active one — ValidationError on
        # mismatch maps to 400 in the dispatcher)
        context_frame = core.context.leave_scope(context_frame, scope_uuid)

        # Session 20B: Publish SSE events for context update
        # Get containers from updated context frame for event payload
        containers = context_frame.containers if hasattr(context_frame, "containers") else []
        publish_context_updated(
            participant_uuid=actor,
            containers=containers,
            scope_uuid=uid.add_core_prefix(scope_uuid),
        )

        # Get head item for frame_updated event
        head_item_uuid = context_frame.head_item_uuid if hasattr(context_frame, "head_item_uuid") else None
        publish_frame_updated(
            participant_uuid=actor,
            head_item_uuid=head_item_uuid,
            scope_uuid=uid.add_core_prefix(scope_uuid),
        )

        return {
            "scope": uid.add_core_prefix(scope_uuid),
            "active_scope": None,
        }


@with_audit
def handle_focus(request: FocusRequest, actor: str) -> dict:
    """RETIRED verb — deprecated no-op alias (RFC-003 v5 INV-11a / RFC-011).

    With exclusive activation there is no primary-vs-active distinction:
    the active scope IS the only scope. Kept for the migration window
    only — responds with the current active scope, changes no state,
    publishes no events. Migrate to `enter`.
    """
    with get_core() as core:
        try:
            context_frame = core.context.get_context_frame(
                owner=actor,
                owner_type="operator",
                create_if_missing=False
            )
            active_scope = context_frame.active_scope
        except Exception:
            active_scope = None

        return {
            "scope": uid.add_core_prefix(uid.strip_prefix(request.scope)),
            "active_scope": uid.add_core_prefix(active_scope) if active_scope else None,
        }


# ============================================================================
# Search Verb Handler (RFC-005 v7)
# ============================================================================

@with_audit
def handle_search(request: SearchRequest, actor: str) -> dict:
    """Handle search verb - semantic search and discovery.

    Per RFC-005 v7:
    - search: Semantic search and discovery

    Session 9: Fuzzy text search with configurable coverage and effort.

    Coverage levels:
    - names: Title/name fields only (fast)
    - content: Names + body text
    - full: All indexed fields including metadata

    Effort modes:
    - quick: Cached results, shallow search
    - standard: Full search (default)
    - deep: Exhaustive search

    Strategy:
    - fuzzy: Text matching with typo tolerance (SQLite LIKE)
    - auto: System chooses based on query characteristics

    Continuation tokens enable pagination for large result sets.

    Args:
        request: Validated SearchRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with search results and continuation token
    """
    from system.soil import get_soil

    # RFC-011 §4.9(3): scope enforcement is PRE-MATCH (RFC-005 v8) — the
    # system searches exclude scoped items outside the allowed set before
    # LIKE evaluation, ORDER, and LIMIT, so a scoped hit can never
    # displace a real match and result counts stay truthful. Allowed set
    # = the requester's active frame + public-visibility scopes (read
    # semantics; most permissive wins). The convenience `scope` request
    # field narrows results further; enforcement is independent of it.
    if enforcement_enabled() and not requester_has_all_scopes():
        active = active_scope_for(actor)
        allowed_scopes = sorted(
            ({active} if active else set()) | set(public_scope_uuids())
        )
    else:
        allowed_scopes = None
    scope_uuid = uid.strip_prefix(request.scope) if request.scope else None

    # Collect results based on target_type
    results = []
    entity_results = []
    fact_results = []

    # Search entities (Core) - using public API
    if request.target_type in ("entity", "all"):
        with get_core() as core:
            # Use public search API instead of direct _conn access
            entity_rows = core.entity.search(
                query=request.query,
                coverage=request.coverage,
                limit=request.limit,
                allowed_scopes=allowed_scopes,
                scope_uuid=scope_uuid,
            )

            # Convert to response format (already dicts with parsed JSON)
            for entity in entity_rows:
                entity_results.append(_add_core_prefix(entity))

    # Search facts (Soil/Facts) - using public API
    if request.target_type in ("fact", "all"):
        with get_soil() as soil:
            # Use public search API instead of direct _conn access
            item_rows = soil.search_items(
                query=request.query,
                coverage=request.coverage,
                limit=request.limit,
                allowed_scopes=allowed_scopes,
                scope_uuid=scope_uuid,
            )

            # Convert to response format
            for row in item_rows:
                # Parse JSON fields
                try:
                    data = json.loads(row["data"]) if row["data"] else {}
                except (json.JSONDecodeError, TypeError, KeyError):
                    data = {}
                try:
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                except (json.JSONDecodeError, TypeError, KeyError):
                    metadata = {}

                fact_results.append({
                    "uuid": uid.add_soil_prefix(row["uuid"]),
                    "type": row["_type"],
                    "data": data,
                    "integrity_hash": row["integrity_hash"],
                    "realized_at": row["realized_at"],
                    "canonical_at": row["canonical_at"],
                    "metadata": metadata,
                    "fidelity": row["fidelity"],
                    "kind": "fact",  # Add kind marker for disambiguation
                })

    # Merge and deduplicate results
    # Session 9: Simple concatenation (entities first, then facts)
    results = entity_results + fact_results

    # RFC-011 §4.9(3): pre-match filtering happened at the SQL level in
    # the system searches (above) — scoped items outside the requester's
    # allowed set never became matches, so no post-fetch exclusion here.

    # TODO: Continuation token implementation (RFC-005)
    # Session 9: Deferred - requires encoding offset/limit in base64
    # Future: Implement continuation_token = base64encode(f"{offset}:{limit}:{last_timestamp}")
    # Future: If request.continuation_token is provided, decode and resume from offset
    continuation_token = None
    if len(results) == request.limit:
        # Would generate token here for real pagination
        continuation_token = None  # Session 9: Deferred

    # TODO: Strategy parameter implementation
    # Session 9: Always uses fuzzy (LIKE with wildcards)
    # Future: Implement "auto" strategy to choose between fuzzy/semantic based on query
    # Future: Implement "semantic" strategy with embeddings/vector DB
    strategy_used = request.strategy  # Placeholder - currently ignored, always uses fuzzy

    # TODO: Effort mode implementation
    # Session 9: Framework in place but not implemented
    # Future: "quick" - use cached results
    # Future: "deep" - exhaustive search with higher limits
    effort_used = request.effort  # Placeholder - currently ignored

    # TODO: Threshold parameter implementation
    # Session 9: Not implemented (only relevant for semantic search with scores)
    # Future: Filter results by similarity score when semantic search is implemented
    threshold_used = request.threshold  # Placeholder - currently ignored

    return {
        "query": request.query,
        "results": results,
        "count": len(results),
        "continuation_token": continuation_token,
        "strategy": strategy_used,
        "coverage": request.coverage,
        "effort": effort_used,
    }
