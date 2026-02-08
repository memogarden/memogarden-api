"""Soil bundle verb handlers.

Implements the Soil bundle verbs from RFC-005 v7:
- add: Add fact (bring external data into MemoGarden)
- amend: Amend fact (create superseding fact)
- get: Get fact by UUID
- query: Query facts with filters

Session 2: Baseline item types only (Note, Message, Email, ToolCall, etc.)
"""

import json
import logging

from system.soil import Item, get_soil
from system.soil.item import current_day, generate_soil_uuid
from system.utils import isodatetime, uid

from ..schemas.semantic import (
    AddRequest,
    AmendRequest,
    GetRequest,
    QueryRequest,
)
from .decorators import with_audit

logger = logging.getLogger(__name__)

# Baseline item types that can be added via Semantic API
# Session 2: These are the types defined in memogarden/schemas/types/items/
BASELINE_ITEM_TYPES = {
    "Note",
    "Message",
    "Email",
    "ToolCall",
    "EntityDelta",
    "SystemEvent",
}


# ============================================================================
# Helper Functions
# ============================================================================

def _item_to_fact_response(item) -> dict:
    """Convert an Item object to fact response dict.

    Adds soil_ prefix to UUIDs and includes all fact fields.
    """
    return {
        "uuid": uid.add_soil_prefix(item.uuid),
        "type": item._type,
        "data": item.data,
        "metadata": item.metadata or {},
        # Hash and integrity
        "integrity_hash": item.integrity_hash,
        "fidelity": item.fidelity,
        # Timestamps
        "realized_at": item.realized_at,
        "canonical_at": item.canonical_at,
        # Supersession fields
        "superseded_by": uid.add_soil_prefix(item.superseded_by) if item.superseded_by else None,
        "superseded_at": item.superseded_at,
    }

def _row_to_fact_response(row) -> dict:
    """Convert a database row to fact response dict.

    Adds soil_ prefix to UUIDs and includes all fact fields.
    """
    fact_uuid = row["uuid"]

    # Parse JSON data if present
    try:
        data_value = row["data"]
        data = json.loads(data_value) if data_value else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        data = {}

    # Parse JSON metadata if present
    try:
        metadata_value = row["metadata"]
        metadata = json.loads(metadata_value) if metadata_value else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        metadata = {}

    # Helper to safely get optional values from sqlite3.Row
    def safe_get(key, default=None):
        try:
            val = row[key]
            return val if val is not None else default
        except (KeyError, IndexError):
            return default

    return {
        "uuid": uid.add_soil_prefix(fact_uuid),
        "type": row["_type"],
        "data": data,
        "metadata": metadata,
        # Hash and integrity
        "integrity_hash": row["integrity_hash"],
        "fidelity": row["fidelity"],
        # Timestamps
        "realized_at": row["realized_at"],
        "canonical_at": row["canonical_at"],
        # Supersession fields
        "superseded_by": uid.add_soil_prefix(safe_get("superseded_by")) if safe_get("superseded_by") else None,
        "superseded_at": safe_get("superseded_at"),
    }


# ============================================================================
# Verb Handlers
# ============================================================================

@with_audit
def handle_add(request: AddRequest, actor: str) -> dict:
    """Handle add verb - add a new fact (Item) to Soil.

    Session 2: Supports baseline item types only.
    Facts are immutable once created. Use `amend` to create superseding facts.

    Args:
        request: Validated AddRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with created fact data
    """
    with get_soil() as soil:
        # Validate item type is in baseline
        if request.type not in BASELINE_ITEM_TYPES:
            raise ValueError(
                f"Item type '{request.type}' not supported. "
                f"Baseline types: {', '.join(sorted(BASELINE_ITEM_TYPES))}. "
                f"Custom schema registration not yet implemented."
            )

        # Get current time for realized_at
        now = isodatetime.now()

        # Use provided canonical_at or default to realized_at
        canonical_at = request.canonical_at
        if canonical_at is None:
            canonical_at = now

        # Create Item
        item = Item(
            uuid=generate_soil_uuid(),
            _type=request.type,
            realized_at=now,
            canonical_at=canonical_at,
            data=request.data,
            metadata=request.metadata,
            integrity_hash=None,  # Will be computed by create_item
            fidelity="full",
        )

        # Create item in Soil
        item_uuid = soil.create_item(item)

        # Fetch created item
        item = soil.get_item(item_uuid)

        return _item_to_fact_response(item)


@with_audit
def handle_amend(request: AmendRequest, actor: str) -> dict:
    """Handle amend verb - amend a fact by creating a superseding fact.

    Creates a new Fact with the corrected data and creates a `supersedes`
    system relation linking the new fact to the original.

    The original fact remains immutable but is marked as superseded.

    Args:
        request: Validated AmendRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with amended fact data
    """
    with get_soil() as soil:
        # Strip prefix to get raw UUID
        target_id = uid.strip_prefix(request.target)

        # Get original item
        original = soil.get_item(target_id)
        if original is None:
            from system.exceptions import ResourceNotFound
            raise ResourceNotFound(
                f"Fact not found: {request.target}",
                details={"target": request.target}
            )

        # Check if already superseded
        if original.superseded_by is not None:
            from system.exceptions import ValidationError as MGValidationError
            raise MGValidationError(
                message="Cannot amend fact that is already superseded",
                details={
                    "fact_uuid": uid.add_soil_prefix(target_id),
                    "superseded_by": uid.add_soil_prefix(original.superseded_by),
                }
            )

        # Get current time for realized_at
        now = isodatetime.now()

        # Use provided canonical_at or default to original's canonical_at
        canonical_at = request.canonical_at
        if canonical_at is None:
            canonical_at = original.canonical_at

        # Merge metadata with original metadata
        new_metadata = {}
        if original.metadata:
            new_metadata.update(original.metadata)
        if request.metadata:
            new_metadata.update(request.metadata)

        # Create new Item with amended data
        amended_item = Item(
            uuid=generate_soil_uuid(),
            _type=original._type,
            realized_at=now,
            canonical_at=canonical_at,
            data=request.data,
            metadata=new_metadata if new_metadata else None,
            integrity_hash=None,  # Will be computed by create_item
            fidelity="full",
        )

        # Create amended item
        amended_uuid = soil.create_item(amended_item)

        # Update original to mark as superseded
        soil.mark_superseded(
            original_uuid=request.target,
            superseded_by_uuid=amended_uuid,
            superseded_at=now
        )

        # Create supersedes relation
        from system.soil.relation import SystemRelation
        relation = SystemRelation(
            uuid=generate_soil_uuid(),  # Uses module-level import from system.soil.item
            kind="supersedes",
            source=amended_uuid,
            source_type="item",
            target=request.target,  # Use request.target which has the prefix
            target_type="item",
            created_at=current_day(),  # Uses module-level import from system.soil.item
            evidence={
                "source": "user_stated",
                "method": "semantic_api_amend",
            },
        )
        soil.create_relation(relation)

        # Fetch amended item
        amended = soil.get_item(amended_uuid)

        return _item_to_fact_response(amended)


@with_audit
def handle_get_fact(request: GetRequest, actor: str) -> dict:
    """Handle get verb - get fact by UUID.

    Supports prefixed and non-prefixed UUIDs.
    UUID must have soil_ prefix (or be recognized as a fact UUID).

    Args:
        request: Validated GetRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with fact data
    """
    with get_soil() as soil:
        item = soil.get_item(request.target)

        if item is None:
            from system.exceptions import ResourceNotFound
            raise ResourceNotFound(
                f"Fact not found: {request.target}",
                details={"target": request.target}
            )

        return _item_to_fact_response(item)


@with_audit
def handle_query_facts(request: QueryRequest, actor: str) -> dict:
    """Handle query verb - query facts with filters.

    Session 2: Basic type filter and pagination only.
    Filters are applied to item._type.

    Args:
        request: Validated QueryRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with query results (results, total, start_index, count)
    """
    with get_soil() as soil:
        # Build WHERE clause
        where_parts = []
        params = []

        # Filter by type
        if request.type:
            where_parts.append("_type = ?")
            params.append(request.type)

        # Filter by superseded status (default: exclude superseded)
        where_parts.append("superseded_by IS NULL")

        # Build full query
        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        query = f"""
            SELECT * FROM item
            WHERE {where_clause}
            ORDER BY realized_at DESC
            LIMIT ? OFFSET ?
        """

        params.extend([request.count, request.start_index])

        # Execute query
        # TODO: Add public API method to Soil for querying items with filters
        # This is a temporary workaround accessing private connection until
        # Soil.query_items_with_filters() or similar public method exists.
        rows = soil._get_connection().execute(query, params).fetchall()

        # Get total count
        count_query = f"SELECT COUNT(*) as total FROM item WHERE {where_clause}"
        total_row = soil._get_connection().execute(count_query, params[:-2]).fetchone()
        total = total_row["total"]

        # Convert rows to response format
        results = [_row_to_fact_response(row) for row in rows]

        return {
            "results": results,
            "total": total,
            "start_index": request.start_index,
            "count": len(results),
        }
