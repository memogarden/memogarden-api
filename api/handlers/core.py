"""Core bundle verb handlers.

Implements the Core bundle verbs from RFC-005 v7:
- create: Create entity (any type)
- edit: Edit entity (set/unset semantics)
- forget: Soft delete entity
- get: Get entity by UUID
- query: Query entities with filters

Session 1: Baseline entity types only (Transaction, Recurrence, etc.)
"""

import json
import logging

from system.core import get_core
from system.utils import isodatetime, uid

from ..schemas.semantic import (
    CreateRequest,
    EditRequest,
    ForgetRequest,
    GetRequest,
    LinkRequest,
    QueryRequest,
)

logger = logging.getLogger(__name__)

# Baseline entity types that can be created via Semantic API
# Session 1: These are the types defined in memogarden/schemas/types/entities/
BASELINE_ENTITY_TYPES = {
    "Transaction",
    "Recurrence",
    "Artifact",
    "Label",
    "Operator",
    "Agent",
    "Entity",  # Generic entity type
}


# ============================================================================
# Helper Functions
# ============================================================================

def _row_to_entity_response(row, entity_type: str = "Entity") -> dict:
    """Convert a database row to entity response dict.

    Adds core_ prefix to UUIDs and includes all entity fields.
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
    def safe_get(key, default=None):
        try:
            val = row[key]
            return val if val is not None else default
        except (KeyError, IndexError):
            return default

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
        "superseded_by": uid.add_core_prefix(safe_get("superseded_by")) if safe_get("superseded_by") else None,
        "superseded_at": safe_get("superseded_at"),
        "group_id": uid.add_core_prefix(safe_get("group_id")) if safe_get("group_id") else None,
        "derived_from": uid.add_core_prefix(safe_get("derived_from")) if safe_get("derived_from") else None,
    }


# ============================================================================
# Verb Handlers
# ============================================================================

def handle_create(request: CreateRequest, actor: str) -> dict:
    """Handle create verb - create a new entity.

    Session 1: Supports baseline entity types only.
    Future: Will support registered custom schemas.

    Args:
        request: Validated CreateRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with created entity data
    """
    # Validate entity type is in baseline
    if request.type not in BASELINE_ENTITY_TYPES:
        raise ValueError(
            f"Entity type '{request.type}' not supported. "
            f"Baseline types: {', '.join(sorted(BASELINE_ENTITY_TYPES))}. "
            f"Custom schema registration not yet implemented."
        )

    # Use atomic transaction for entity creation
    with get_core(atomic=True) as core:
        entity_uuid = core.entity.create(
            entity_type=request.type,
            data=json.dumps(request.data) if request.data else json.dumps({}),
        )

    # Fetch created entity
    core = get_core()
    row = core.entity.get_by_id(entity_uuid)

    return _row_to_entity_response(row)


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
    core = get_core()
    row = core.entity.get_by_id(request.target)

    return _row_to_entity_response(row)


def handle_edit(request: EditRequest, actor: str) -> dict:
    """Handle edit verb - edit entity with set/unset semantics.

    Session 1: For baseline types, edits update the entity.data JSON field.
    Domain-specific tables (e.g., transaction) are NOT updated yet.

    set: handles both add-new and update-existing
    unset: removes fields from entity.data

    Args:
        request: Validated EditRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with updated entity data
    """
    entity_id = uid.strip_prefix(request.target)
    core = get_core()

    # Get current entity state
    current = core.entity.get_by_id(entity_id)
    # sqlite3.Row doesn't have .get(), use direct access
    data_value = current["data"]
    current_data = json.loads(data_value) if data_value else {}

    # Apply set operations
    if request.set:
        current_data.update(request.set)

    # Apply unset operations
    if request.unset:
        for field in request.unset:
            current_data.pop(field, None)

    # Update entity data and hash via Core API
    core.entity.update_data(entity_id, current_data)

    # Fetch updated entity
    row = core.entity.get_by_id(entity_id)

    return _row_to_entity_response(row)


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

    with get_core(atomic=True) as core:
        # Verify entity exists
        core.entity.get_by_id(entity_id)

        # Create tombstone entity
        tombstone_id = core.entity.create(entity_type="Tombstone")

        # Mark original as superseded
        core.entity.supersede(entity_id, tombstone_id)

    # Return the original entity (now superseded)
    core = get_core()
    row = core.entity.get_by_id(entity_id)

    return _row_to_entity_response(row)


def handle_query(request: QueryRequest, actor: str) -> dict:
    """Handle query verb - query entities with filters.

    Session 1: Basic equality filters only.
    Filters are applied to entity.type and JSON data fields.

    Args:
        request: Validated QueryRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with query results (results, total, start_index, count)
    """
    core = get_core()

    # Build query with basic filters
    # Session 1: Filter by type, basic pagination
    # Future: Full DSL with operators (any, not, etc.)

    # Build WHERE clause
    where_parts = []
    params = []

    # Filter by type
    if request.type:
        where_parts.append("type = ?")
        params.append(request.type)

    # Filter by superseded status (default: exclude superseded)
    where_parts.append("superseded_by IS NULL")

    # Build full query
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"
    query = f"""
        SELECT * FROM entity
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """

    params.extend([request.count, request.start_index])

    # Execute query
    rows = core._conn.execute(query, params).fetchall()

    # Get total count
    count_query = f"SELECT COUNT(*) as total FROM entity WHERE {where_clause}"
    total_row = core._conn.execute(count_query, params[:-2]).fetchone()
    total = total_row["total"]

    # Convert rows to response format
    results = [_row_to_entity_response(row) for row in rows]

    return {
        "results": results,
        "total": total,
        "start_index": request.start_index,
        "count": len(results),
    }


def handle_link(request: LinkRequest, actor: str) -> dict:
    """Handle link verb - create user relation with time horizon (RFC-002).

    Creates a user relation (engagement signal) with an initial time horizon.
    The relation will decay over time based on access patterns.

    Args:
        request: Validated LinkRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with created relation details

    Raises:
        ValueError: If relation kind is invalid
    """
    core = get_core()

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
