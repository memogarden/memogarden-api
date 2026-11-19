"""Soil bundle verb handlers.

Implements the Soil bundle verbs from RFC-005 v7:
- add: Add fact (bring external data into MemoGarden)
- amend: Amend fact (create superseding fact)
- get: Get fact by UUID
- query: Query facts with filters

Session 2: Baseline fact types only (Note, Message, Email, ToolCall, etc.)

Session 20B: Event Integration
Publishes SSE events for fact operations:
- message_sent: Published when Message fact is added
"""

import json
import logging
import re

from system.exceptions import PermissionDenied, ValidationError
from system.soil import Fact, current_day, generate_soil_uuid, get_soil
from utils import isodatetime, uid

from ..schemas.semantic import (
    AddRequest,
    AmendRequest,
    GetRequest,
    QueryRequest,
)
from ..events import publish_message_sent
from ..middleware.scope_access import (
    active_scope_for,
    enforcement_enabled,
    filter_visible,
    item_visible,
    normalize_scopes,
    public_scope_uuids,
    requester_has_all_scopes,
    require_scope_grant,
)
from .decorators import with_audit

logger = logging.getLogger(__name__)

# Baseline fact types that can be added via Semantic API
# Session 2: These are the types defined in memogarden/schemas/types/facts/
BASELINE_ITEM_TYPES = {
    "Note",
    "Message",
    "Email",
    "ToolCall",
    "EntityDelta",
    "SystemEvent",
    # The occurrence record (tada ontology; schemas/event.yaml FACT section)
    # — agents record occurrences via /mg add; providers bypass this
    # whitelist. gcal's legacy EventOccurrence converges here (#31).
    "Event",
    # Operator-authored (memogarden import opml): one fact per subscribed
    # RSS/Atom feed; Entry facts themselves enter via the provider runtime,
    # not this verb (schemas/subscription.yaml).
    "Subscription",
}


# ============================================================================
# Helper Functions
# ============================================================================

def _fact_to_response(item) -> dict:
    """Convert a Fact object to fact response dict.

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
    """Handle add verb - add a new fact (Fact) to Soil.

    Session 2: Supports baseline fact types only.
    Facts are immutable once created. Use `amend` to create superseding facts.

    Session 20B: Publishes message_sent SSE event for Message facts.

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

        # RFC-011 §4.6: scope applied at creation. Explicit caller-supplied
        # labels require the requester to hold grants for them (operator
        # JWT bypasses — SG-11) — grant-gated only under enforcement
        # (§8 advisory mode: flag off, nothing is checked). Otherwise the
        # creator's active scope is inherited — ALWAYS, advisory included,
        # so the labeling corpus is correct before the flag flips.
        metadata = dict(request.metadata) if request.metadata else {}
        explicit_scopes = metadata.get("scopes")
        if explicit_scopes:
            # Grant checks are an enforcement activity — §8 advisory mode
            # (flag off) accepts labels unchecked so provisioning flows work.
            if enforcement_enabled() and not requester_has_all_scopes():
                for s in explicit_scopes:
                    require_scope_grant(uid.strip_prefix(s) if isinstance(s, str) else s)
            metadata["scopes"] = normalize_scopes(explicit_scopes)
        else:
            # Inheritance runs advisory too: items created inside a scope
            # are labeled before the flag flips (no silently-global corpus).
            active_scope = active_scope_for(actor)
            if active_scope:
                metadata["scopes"] = [active_scope]

        # Create Fact
        item = Fact(
            uuid=generate_soil_uuid(),
            _type=request.type,
            realized_at=now,
            canonical_at=canonical_at,
            data=request.data,
            metadata=metadata or None,
            integrity_hash=None,  # Will be computed by create_item
            fidelity="full",
        )

        # Create item in Soil
        item_uuid = soil.create_fact(item)

        # Fetch created item
        item = soil.get_fact(item_uuid)

        # Session 20B: Publish SSE event for Message facts
        if request.type == "Message":
            # Extract conversation log UUID from Message data
            log_uuid = request.data.get("log_uuid")
            sender = request.data.get("sender", "unknown")
            content = request.data.get("description", "")
            fragments = request.data.get("fragments", [])
            scope_uuid = request.data.get("scope_uuid")

            publish_message_sent(
                log_uuid=log_uuid,
                message_uuid=item_uuid,
                sender=sender,
                content=content,
                fragments=fragments,
                scope_uuid=scope_uuid,
            )

        return _fact_to_response(item)


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
        original = soil.get_fact(target_id)
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

        # RFC-011 §4.5: writes require the requester's active scope to be
        # one of the item's labels (operator JWT bypasses — SG-11). The
        # amended fact inherits the original's scope labels via the
        # metadata merge below (SG-9 derivation inheritance).
        if enforcement_enabled() and not requester_has_all_scopes():
            original_scopes = (original.metadata or {}).get("scopes") or []
            if not item_visible(original_scopes, active_scope_for(actor)):
                raise PermissionDenied(
                    "Item is in a scope you are not inside",
                    {"target": request.target, "item_scopes": original_scopes,
                     "code": "scope_write_denied"},
                )

        # Merge metadata with original metadata (computed once; the label
        # diff below and the Fact construction share it)
        new_metadata = {}
        if original.metadata:
            new_metadata.update(original.metadata)
        if request.metadata:
            new_metadata.update(request.metadata)
        if "scopes" in new_metadata:
            new_metadata["scopes"] = normalize_scopes(new_metadata["scopes"] or [])

        # RFC-011 v1.2 (D2): symmetric label grants. A label added OR
        # removed by a non-sovereign principal requires a grant on that
        # label's scope — amend's metadata merge is a relabel channel, and
        # removal is the exposure-widening direction (de-scoped items fall
        # back to global). The before-state persists in the superseded
        # original and the amend is audited via with_audit, satisfying
        # SG-8's relabel record.
        if enforcement_enabled() and not requester_has_all_scopes():
            old_labels = set((original.metadata or {}).get("scopes") or [])
            new_labels = set(new_metadata.get("scopes") or [])
            for changed in old_labels.symmetric_difference(new_labels):
                require_scope_grant(changed)

        # Get current time for realized_at
        now = isodatetime.now()

        # Use provided canonical_at or default to original's canonical_at
        canonical_at = request.canonical_at
        if canonical_at is None:
            canonical_at = original.canonical_at

        # Create new Fact with amended data
        amended_item = Fact(
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
        amended_uuid = soil.create_fact(amended_item)

        # Update original to mark as superseded
        soil.mark_superseded(
            original_uuid=request.target,
            superseded_by_uuid=amended_uuid,
            superseded_at=now
        )

        # Create supersedes relation
        from system.soil import SystemRelation
        relation = SystemRelation(
            uuid=generate_soil_uuid(),  # Uses module-level import from system.soil.fact
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
        amended = soil.get_fact(amended_uuid)

        return _fact_to_response(amended)


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
        item = soil.get_fact(request.target)

        if item is None:
            from system.exceptions import ResourceNotFound
            raise ResourceNotFound(
                f"Fact not found: {request.target}",
                details={"target": request.target}
            )

        # RFC-011 §4.5 read gate (SG-1/SG-5): scoped items are readable
        # only from inside one of their scopes (operator JWT bypasses).
        # READ semantics — public-visibility labels relax this gate (most
        # permissive wins); the amend write gate below stays strict.
        if enforcement_enabled() and not requester_has_all_scopes():
            item_scopes = (item.metadata or {}).get("scopes") or []
            if not item_visible(
                item_scopes, active_scope_for(actor), public_scope_uuids()
            ):
                raise PermissionDenied(
                    "Item is in a scope you are not inside",
                    {"target": request.target, "item_scopes": item_scopes,
                     "code": "scope_read_denied"},
                )

        return _fact_to_response(item)


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

        # RFC-010: equality filters on fact data fields (fully parameterized
        # json_extract paths; dotted keys address nested fields, e.g.
        # "params.resource"). Powers per-agent traceability:
        #   {"op": "query", "target_type": "fact", "type": "Action",
        #    "filters": {"actor": "core_...", "actor_type": "agent"}}
        # The reserved `metadata.` dotted prefix addresses the metadata
        # column instead (same path rule) — Event facts carry their
        # terminal verdict and provenance there (source/status/
        # resolved_at), so without it "all completed tada tasks" is
        # inexpressible (#65). Bare "metadata" is rejected as ambiguous.
        if request.filters:
            for field, value in request.filters.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", field):
                    raise ValidationError(
                        f"Invalid filter field: {field!r} (expected plain or dotted data field name)"
                    )
                if field == "metadata":
                    raise ValidationError(
                        "Ambiguous filter field 'metadata' (reserved "
                        "prefix — use dotted keys like 'metadata.source')"
                    )
                if field.startswith("metadata."):
                    column = "metadata"
                    path = field[len("metadata."):]
                else:
                    column = "data"
                    path = field
                where_parts.append(f"json_extract({column}, ?) = ?")
                params.append(f"$.{path}")
                params.append(value)

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

        # RFC-011 §4.9: filtered before the response is built — scoped
        # items outside the requester's active scope never surface (SG-10).
        # NOTE: exclusions after LIMIT can shorten pages; acceptable for v1
        # (flag default off; SQL-level pre-filter is the indexed follow-up).
        def _row_scopes(row) -> list | None:
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            return metadata.get("scopes")

        visible_rows = filter_visible(rows, actor, _row_scopes)

        # Convert rows to response format
        results = [_row_to_fact_response(row) for row in visible_rows]

        return {
            "results": results,
            "total": total,
            "start_index": request.start_index,
            "count": len(results),
        }
