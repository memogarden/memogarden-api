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
from system.exceptions import ResourceNotFound
from system.utils import uid

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

    # Use transaction for entity creation and fetch
    with get_core() as core:
        entity_uuid = core.entity.create(
            entity_type=request.type,
            data=json.dumps(request.data) if request.data else json.dumps({}),
        )

        # Fetch created entity
        row = core.entity.get_by_id(entity_uuid)

        return _row_to_entity_response(row)


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
        row = core.entity.get_by_id(request.target)
        return _row_to_entity_response(row)


@with_audit
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

    with get_core() as core:
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
        # Verify entity exists
        core.entity.get_by_id(entity_id)

        # Create tombstone entity
        tombstone_id = core.entity.create(entity_type="Tombstone")

        # Mark original as superseded
        core.entity.supersede(entity_id, tombstone_id)

        # Return the original entity (now superseded)
        row = core.entity.get_by_id(entity_id)

        return _row_to_entity_response(row)


@with_audit
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
    with get_core() as core:
        # Use public API method to query entities
        rows, total = core.entity.query_with_filters(
            entity_type=request.type,
            include_superseded=False,
            limit=request.count,
            offset=request.start_index,
        )

        # Convert rows to response format
        results = [_row_to_entity_response(row) for row in rows]

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

        return {
            "uuid": uid.add_core_prefix(request.target),
            "deleted": True
        }


@with_audit
def handle_edit_relation(request: EditRequest, actor: str) -> dict:
    """Handle edit_relation verb - edit relation attributes.

    Per RFC-002 v5:
    - edit_relation: Edit relation attributes (time_horizon, metadata, evidence)

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
                    item = soil.get_item(node_uuid)
                    if item:
                        nodes.append({
                            "uuid": uid.add_soil_prefix(item.uuid),
                            "layer": "soil",
                            "type": item._type,
                        })
            except ResourceNotFound:
                # Item not found in Soil either - skip
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
    """Handle enter verb - add scope to active set.

    Per RFC-003 v4:
    - enter: Add scope to active set
    - INV-11a: Focus Separation (enter does NOT auto-focus)
    - INV-11b: Implied Focus (first scope becomes primary)

    Args:
        request: Validated EnterRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with scope and active_scopes
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

        # Enter the scope
        context_frame = core.context.enter_scope(context_frame, scope_uuid)

        return {
            "scope": uid.add_core_prefix(scope_uuid),
            "active_scopes": [uid.add_core_prefix(s) for s in (context_frame.active_scopes or [])],
            "primary_scope": uid.add_core_prefix(context_frame.primary_scope) if context_frame.primary_scope else None
        }


@with_audit
def handle_leave(request: LeaveRequest, actor: str) -> dict:
    """Handle leave verb - remove scope from active set.

    Per RFC-003 v4:
    - leave: Remove scope from active set
    - INV-8: Stream Suspension on Leave

    Args:
        request: Validated LeaveRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with scope and active_scopes

    Raises:
        ResourceNotFound: If user has no ContextFrame
        ValueError: If scope not in active set
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

        # Leave the scope
        context_frame = core.context.leave_scope(context_frame, scope_uuid)

        return {
            "scope": uid.add_core_prefix(scope_uuid),
            "active_scopes": [uid.add_core_prefix(s) for s in (context_frame.active_scopes or [])],
            "primary_scope": uid.add_core_prefix(context_frame.primary_scope) if context_frame.primary_scope else None
        }


@with_audit
def handle_focus(request: FocusRequest, actor: str) -> dict:
    """Handle focus verb - switch primary scope among active scopes.

    Per RFC-003 v4:
    - focus: Switch primary scope among active scopes
    - INV-11: Explicit Scope Control (focus requires explicit action)

    Args:
        request: Validated FocusRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with scope and primary_scope

    Raises:
        ResourceNotFound: If user has no ContextFrame
        ValueError: If scope not in active set
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

        # Focus the scope
        context_frame = core.context.focus_scope(context_frame, scope_uuid)

        return {
            "scope": uid.add_core_prefix(scope_uuid),
            "primary_scope": uid.add_core_prefix(context_frame.primary_scope) if context_frame.primary_scope else None,
            "active_scopes": [uid.add_core_prefix(s) for s in (context_frame.active_scopes or [])]
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
                limit=request.limit
            )

            # Convert to response format
            for row in entity_rows:
                entity_results.append(_row_to_entity_response(row))

    # Search facts (Soil/Items) - using public API
    if request.target_type in ("fact", "all"):
        with get_soil() as soil:
            # Use public search API instead of direct _conn access
            item_rows = soil.search_items(
                query=request.query,
                coverage=request.coverage,
                limit=request.limit
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
