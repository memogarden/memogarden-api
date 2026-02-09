"""Pydantic schemas for Semantic API requests and responses.

Per RFC-005 v7, the Semantic API uses a message-passing interface with
a consistent request/response envelope format.

Request envelope:
    {"op": "create", "type": "Contact", "data": {...}}

Response envelope (success):
    {"ok": true, "actor": "usr_xxx", "timestamp": "...", "result": {...}}

Response envelope (error):
    {"ok": false, "actor": "usr_xxx", "timestamp": "...", "error": {...}}
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ============================================================================
# Request Envelope
# ============================================================================

class SemanticRequest(BaseModel):
    """Base Semantic API request envelope.

    All requests use the "op" field to specify the verb.
    Additional fields vary by operation.

    Session 6: Added bypass_semantic_api field to prevent audit recursion.
    """

    op: Literal[
        "create", "edit", "forget", "get", "query",
        "add", "amend",
        "link", "unlink", "edit_relation", "get_relation", "query_relation", "explore",
        "enter", "leave", "focus", "rejoin",
        "track", "search",
        "register"
    ] = Field(..., description="Operation verb")
    bypass_semantic_api: bool = Field(default=False, description="If True, skip audit logging (internal use)")


# ============================================================================
# Common Request Types
# ============================================================================

class CreateRequest(SemanticRequest):
    """Request to create an entity.

    Per RFC-005:
    - create: to bring into being. Reifies a belief into existence in MemoGarden
    """
    op: Literal["create"] = "create"  # type: ignore[var-annotated]
    type: str = Field(..., description="Entity type (e.g., 'Transaction', 'Recurrence')")
    data: dict[str, Any] = Field(default_factory=dict, description="Entity data (type-specific fields)")
    metadata: dict[str, Any] | None = Field(default=None, description="Optional app-defined metadata")


class GetRequest(SemanticRequest):
    """Request to get an entity, fact, or relation by UUID.

    Per RFC-005:
    - get: to obtain. Retrieve by identifier
    UUID prefix indicates target type (soil_, core_, rel_)
    """
    op: Literal["get", "get_relation"] = "get"  # type: ignore[var-annotated]
    target: str = Field(..., description="UUID of the target (with or without prefix)")


class EditRequest(SemanticRequest):
    """Request to edit an entity or relation.

    Per RFC-005 v7:
    - edit: to revise and publish. Makes changes to entity state
    Uses set/unset semantics for field modifications

    set: handles both add-new and update-existing
    unset: removes fields entirely
    """
    op: Literal["edit", "edit_relation"] = "edit"  # type: ignore[var-annotated]
    target: str = Field(..., description="Entity or relation UUID")
    set: dict[str, Any] | None = Field(default=None, description="Fields to add or update")
    unset: list[str] | None = Field(default=None, description="Field names to remove")

    @field_validator('unset')
    @classmethod
    def validate_unset_not_empty(cls, v: list[str] | None) -> list[str] | None:
        """Ensure unset list is not empty if provided."""
        if v is not None and len(v) == 0:
            raise ValueError("unset list must not be empty")
        return v


class ForgetRequest(SemanticRequest):
    """Request to soft delete an entity.

    Per RFC-005:
    - forget: to lose the power of recall. Entity becomes inactive but traces remain in Soil
    """
    op: Literal["forget"] = "forget"  # type: ignore[var-annotated]
    target: str = Field(..., description="Entity UUID to forget")


class QueryRequest(SemanticRequest):
    """Request to query entities with filters.

    Per RFC-005:
    - query: to ask, to seek by asking. Find entities matching criteria

    Session 1: Basic equality filters only
    Future: Full DSL with operators (any, not, etc.)
    """
    op: Literal["query", "query_relation"] = "query"  # type: ignore[var-annotated]
    target_type: Literal["entity", "fact", "relation"] = Field(
        default="entity",
        description="Target type to query"
    )
    type: str | None = Field(default=None, description="Filter by exact type name")
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Field-value filters (basic equality in Session 1)"
    )
    start_index: int = Field(default=0, description="Pagination start index", ge=0)
    count: int = Field(default=20, description="Max results to return", ge=1, le=100)


class AddRequest(SemanticRequest):
    """Request to add a fact (Item) to Soil.

    Per RFC-005 v7:
    - add: to bring external data into MemoGarden as a Fact

    Facts are immutable once created. Use `amend` to create superseding facts.

    Session 2: Supports baseline item types only
    """
    op: Literal["add"] = "add"  # type: ignore[var-annotated]
    type: str = Field(
        ...,
        description="Item type (e.g., 'Note', 'Message', 'Email', 'ToolCall')"
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Item data (type-specific fields)"
    )
    canonical_at: str | None = Field(
        default=None,
        description="User-controllable subjective time as ISO 8601 string (defaults to realized_at)"
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional app-defined metadata"
    )


class AmendRequest(SemanticRequest):
    """Request to amend a fact (Item) in Soil.

    Per RFC-005 v7:
    - amend: to correct or rectify. Creates a superseding Fact

    Creates a new Fact with a `supersedes` relation to the original.
    The original Fact remains immutable but is marked as superseded.

    Session 2: Basic amendment with new data
    """
    op: Literal["amend"] = "amend"  # type: ignore[var-annotated]
    target: str = Field(
        ...,
        description="UUID of the Item to amend (with or without soil_ prefix)"
    )
    data: dict[str, Any] = Field(
        ...,
        description="New/corrected data for the Item"
    )
    canonical_at: str | None = Field(
        default=None,
        description="Updated canonical time as ISO 8601 string (defaults to original)"
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata for the amendment"
    )


class LinkRequest(SemanticRequest):
    """Request to create a user relation (RFC-002).

    Per RFC-002 v5:
    - link: Create explicit user relation with time horizon

    User relations track engagement signals and decay over time
    based on access patterns. The time horizon determines when
    the relation should fossilize.
    """
    op: Literal["link"] = "link"  # type: ignore[var-annotated]
    kind: Literal["explicit_link"] = Field(
        default="explicit_link",
        description="Relation kind (currently only explicit_link supported)"
    )
    source: str = Field(
        ...,
        description="UUID of source entity/fact (with or without prefix)"
    )
    source_type: Literal["item", "entity", "artifact"] = Field(
        ...,
        description="Type of source"
    )
    target: str = Field(
        ...,
        description="UUID of target entity/fact (with or without prefix)"
    )
    target_type: Literal["item", "entity", "artifact", "fragment"] = Field(
        ...,
        description="Type of target"
    )
    initial_horizon_days: int = Field(
        default=7,
        description="Initial time horizon in days (default: 7)",
        ge=1
    )
    evidence: dict[str, Any] | None = Field(
        default=None,
        description="Optional evidence for the relation"
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata for the relation"
    )


class EnterRequest(SemanticRequest):
    """Request to enter a scope - add to active set (RFC-003).

    Per RFC-003 v4:
    - enter: Add scope to active set

    INV-11a: Focus Separation - enter does NOT auto-focus
    INV-11b: Implied Focus - first scope becomes primary automatically
    """
    op: Literal["enter"] = "enter"  # type: ignore[var-annotated]
    scope: str = Field(
        ...,
        description="UUID of the scope to enter (with or without prefix)"
    )


class LeaveRequest(SemanticRequest):
    """Request to leave a scope - remove from active set (RFC-003).

    Per RFC-003 v4:
    - leave: Remove scope from active set

    INV-8: Stream Suspension on Leave - scope view-stream suspends
    """
    op: Literal["leave"] = "leave"  # type: ignore[var-annotated]
    scope: str = Field(
        ...,
        description="UUID of the scope to leave (with or without prefix)"
    )


class FocusRequest(SemanticRequest):
    """Request to focus a scope - switch primary scope (RFC-003).

    Per RFC-003 v4:
    - focus: Switch primary scope among active scopes

    INV-11: Explicit Scope Control - focus requires explicit action
    """
    op: Literal["focus"] = "focus"  # type: ignore[var-annotated]
    scope: str = Field(
        ...,
        description="UUID of the scope to focus (must be in active set, with or without prefix)"
    )


class UnlinkRequest(SemanticRequest):
    """Request to unlink/remove a user relation (RFC-002).

    Per RFC-002 v5:
    - unlink: Remove a user relation

    User relations can be removed by the operator who created them.
    System relations are immutable and cannot be unlinked.
    """
    op: Literal["unlink"] = "unlink"  # type: ignore[var-annotated]
    target: str = Field(
        ...,
        description="UUID of the relation to remove (with or without core_ prefix)"
    )


class QueryRelationRequest(SemanticRequest):
    """Request to query user relations with filters (RFC-002).

    Allows filtering relations by source, target, kind, and type.
    """
    op: Literal["query_relation"] = "query_relation"  # type: ignore[var-annotated]
    source: str | None = Field(
        default=None,
        description="Filter by source UUID (with or without prefix)"
    )
    target: str | None = Field(
        default=None,
        description="Filter by target UUID (with or without prefix)"
    )
    kind: str | None = Field(
        default=None,
        description="Filter by relation kind (e.g., 'explicit_link')"
    )
    source_type: str | None = Field(
        default=None,
        description="Filter by source type (item, entity, artifact)"
    )
    target_type: str | None = Field(
        default=None,
        description="Filter by target type (item, entity, artifact, fragment)"
    )
    alive_only: bool = Field(
        default=True,
        description="If True, only return alive relations (time_horizon >= today)"
    )
    limit: int = Field(
        default=100,
        description="Maximum number of results",
        ge=1,
        le=1000
    )


class ExploreRequest(SemanticRequest):
    """Request to explore/graph expand from an anchor (RFC-002).

    Traverses the relation graph to find connected entities/facts.
    """
    op: Literal["explore"] = "explore"  # type: ignore[var-annotated]
    anchor: str = Field(
        ...,
        description="UUID of the starting entity/fact (with or without prefix)"
    )
    direction: Literal["outgoing", "incoming", "both"] = Field(
        default="both",
        description="Direction of traversal"
    )
    radius: int | None = Field(
        default=None,
        description="Maximum hop distance (None = unlimited)",
        ge=1
    )
    kind: str | None = Field(
        default=None,
        description="Filter by relation kind"
    )
    limit: int = Field(
        default=100,
        description="Maximum number of results",
        ge=1,
        le=1000
    )


class TrackRequest(SemanticRequest):
    """Request to track causal chain from entity back to originating facts (RFC-005 v7.1).

    Traces entity lineage through EntityDelta records to source facts.
    Enables audit/reconstruction workflows.

    Response format is a tree structure with 'kind' markers:
    - kind: 'entity' | 'fact' | 'relation'
    - sources: Array of source nodes (recursive)

    Handles diamond ancestry naturally (same fact referenced multiple times).
    """
    op: Literal["track"] = "track"  # type: ignore[var-annotated]
    target: str = Field(
        ...,
        description="Entity UUID to track (with or without core_ prefix)"
    )
    depth: int | None = Field(
        default=None,
        description="Hop limit for traversal (None = unlimited)",
        ge=1
    )


# ============================================================================
# Response Envelope
# ============================================================================

class SemanticResponse(BaseModel):
    """Base Semantic API response envelope.

    Per RFC-005 v7, all responses include:
    - ok: boolean indicating success/failure
    - actor: the authenticated user/agent performing the operation
    - timestamp: ISO 8601 timestamp
    - result: operation-specific payload (on success)
    - error: error details (on failure)
    """

    ok: bool = Field(..., description="True if operation succeeded, false otherwise")
    actor: str = Field(..., description="Actor UUID (usr_xxx or agt_xxx)")
    timestamp: str = Field(..., description="ISO 8601 timestamp string")
    result: dict[str, Any] | None = Field(default=None, description="Operation result (on success)")
    error: dict[str, Any] | None = Field(default=None, description="Error details (on failure)")


class QueryResult(BaseModel):
    """Response envelope for query operations."""

    results: list[dict[str, Any]] = Field(default_factory=list, description="Query results")
    total: int = Field(..., description="Total matching results")
    start_index: int = Field(..., description="Pagination start index")
    count: int = Field(..., description="Number of results returned")


# ============================================================================
# Type aliases for request validation
# ============================================================================

SemanticRequestType = (
    CreateRequest |
    GetRequest |
    EditRequest |
    ForgetRequest |
    QueryRequest |
    AddRequest |
    AmendRequest |
    LinkRequest |
    UnlinkRequest |
    QueryRelationRequest |
    ExploreRequest |
    TrackRequest |
    EnterRequest |
    LeaveRequest |
    FocusRequest
)
