"""Recurrence CRUD endpoints for MemoGarden Core API.

This module implements RESTful endpoints for recurrence management:
- POST   /api/v1/recurrences                 - Create recurrence
- GET    /api/v1/recurrences                 - List with filtering
- GET    /api/v1/recurrences/{id}            - Get single recurrence
- PUT    /api/v1/recurrences/{id}            - Update recurrence
- DELETE /api/v1/recurrences/{id}            - Delete recurrence

All endpoints require authentication via JWT token or API key (enforced via before_request).

Architecture Notes:
- Recurrences are templates for generating recurring transactions
- Entity registry pattern: create entity first, then recurrence
- ISO 8601 timestamps for all date/time fields
- Parameterized queries to prevent SQL injection
- The `entities` field contains JSON describing transaction template(s)
"""

import logging
from datetime import datetime
from flask import Blueprint, jsonify, request

from system.core import get_core
from system.utils import isodatetime, recurrence
from ...validation import validate_request
from ...schemas.recurrence import RecurrenceCreate, RecurrenceUpdate

logger = logging.getLogger(__name__)

# Create Blueprint
recurrences_bp = Blueprint('recurrences', __name__, url_prefix='/recurrences')


def _row_to_recurrence_response(row) -> dict:
    """
    Convert a database row from recurrences_view to RecurrenceResponse dict.

    Args:
        row: SQLite Row object from recurrences_view

    Returns:
        Dictionary matching RecurrenceResponse schema
    """
    return {
        "id": row["id"],
        "rrule": row["rrule"],
        "entities": row["entities"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "superseded_by": row["superseded_by"],
        "superseded_at": row["superseded_at"],
        "group_id": row["group_id"],
        "derived_from": row["derived_from"],
    }


@recurrences_bp.post("")
@validate_request
def create_recurrence(data: RecurrenceCreate):
    """
    Create a new recurrence.

    Requires authentication via JWT token or API key (enforced by before_request).

    Request Body (RecurrenceCreate):
        - rrule: str (required) - iCal RRULE string
        - entities: str (required) - JSON: transaction template(s)
        - valid_from: datetime (required) - Start of recurrence window
        - valid_until: datetime | None (optional) - End of recurrence window

    Returns:
        201: RecurrenceResponse with created recurrence
        400: Validation error
        401: Authentication required (if no valid auth provided)
    """
    # Validate RRULE format
    if not recurrence.validate_rrule(data.rrule):
        return jsonify({"error": "Invalid RRULE format"}), 400

    # Validate recurrence window
    try:
        valid_from_str = isodatetime.to_timestamp(data.valid_from)
        valid_until_str = isodatetime.to_timestamp(data.valid_until) if data.valid_until else None

        if not recurrence.is_valid_recurrence_window(valid_from_str, valid_until_str):
            return jsonify({"error": "Invalid recurrence window: valid_from must be before valid_until"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Use atomic transaction for coordinated entity + recurrence creation
    with get_core(atomic=True) as core:
        recurrence_id = core.recurrence.create(
            rrule=data.rrule,
            entities=data.entities,
            valid_from=data.valid_from,
            valid_until=data.valid_until,
        )

    # Fetch created recurrence with fresh Core (connection closed after atomic block)
    core = get_core()
    row = core.recurrence.get_by_id(recurrence_id)

    return jsonify(_row_to_recurrence_response(row)), 201


@recurrences_bp.get("/<recurrence_id>")
def get_recurrence(recurrence_id: str):
    """
    Get a single recurrence by ID.

    Requires authentication via JWT token or API key (enforced by before_request).

    Args:
        recurrence_id: UUID of the recurrence

    Returns:
        200: RecurrenceResponse
        404: Recurrence not found
        401: Authentication required (if no valid auth provided)
    """
    core = get_core()
    row = core.recurrence.get_by_id(recurrence_id)

    return jsonify(_row_to_recurrence_response(row))


@recurrences_bp.get("")
def list_recurrences():
    """
    List recurrences with optional filtering.

    Requires authentication via JWT token or API key (enforced by before_request).

    Query Parameters:
        - valid_from: ISO 8601 datetime - Filter recurrences starting after this
        - valid_until: ISO 8601 datetime - Filter recurrences ending before this
        - include_superseded: bool - Include superseded recurrences (default: false)
        - limit: int - Maximum results to return (default: 100)
        - offset: int - Number of results to skip (default: 0)

    Returns:
        200: Array of RecurrenceResponse objects
        401: Authentication required (if no valid auth provided)
    """
    # Parse query parameters
    valid_from = request.args.get("valid_from")
    valid_until = request.args.get("valid_until")
    include_superseded = request.args.get("include_superseded", "false").lower() == "true"
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))

    filters = {
        "valid_from": valid_from,
        "valid_until": valid_until,
        "include_superseded": include_superseded
    }

    core = get_core()
    rows = core.recurrence.list(filters, limit=limit, offset=offset)

    return jsonify([_row_to_recurrence_response(row) for row in rows])


@recurrences_bp.put("/<recurrence_id>")
@validate_request
def update_recurrence(recurrence_id: str, data: RecurrenceUpdate):
    """
    Update a recurrence.

    Requires authentication via JWT token or API key.

    Only provided fields are updated (partial update).

    Args:
        recurrence_id: UUID of the recurrence

    Request Body (RecurrenceUpdate):
        All fields optional:
        - rrule: str | None
        - entities: str | None
        - valid_from: datetime | None
        - valid_until: datetime | None

    Returns:
        200: RecurrenceResponse with updated recurrence
        404: Recurrence not found
        400: Validation error
        401: Authentication required
    """
    core = get_core()

    # Verify recurrence exists
    core.recurrence.get_by_id(recurrence_id)

    # Validate RRULE format if provided
    if data.rrule and not recurrence.validate_rrule(data.rrule):
        return jsonify({"error": "Invalid RRULE format"}), 400

    # Build update data from only provided fields
    update_data = data.model_dump(exclude_unset=True)

    # Validate recurrence window if date fields are being updated
    if update_data:
        row = core.recurrence.get_by_id(recurrence_id)
        current_valid_from = row["valid_from"]
        current_valid_until = row["valid_until"]

        new_valid_from = update_data.get("valid_from", isodatetime.to_datetime(current_valid_from))
        new_valid_until = update_data.get("valid_until", isodatetime.to_datetime(current_valid_until) if current_valid_until else None)

        try:
            valid_from_str = isodatetime.to_timestamp(new_valid_from)
            valid_until_str = isodatetime.to_timestamp(new_valid_until) if new_valid_until else None

            if not recurrence.is_valid_recurrence_window(valid_from_str, valid_until_str):
                return jsonify({"error": "Invalid recurrence window: valid_from must be before valid_until"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        core.recurrence.update(recurrence_id, update_data)

    # Fetch updated recurrence
    row = core.recurrence.get_by_id(recurrence_id)

    return jsonify(_row_to_recurrence_response(row))


@recurrences_bp.delete("/<recurrence_id>")
def delete_recurrence(recurrence_id: str):
    """
    Delete a recurrence (soft delete via superseding).

    Requires authentication via JWT token or API key (enforced by before_request).

    Creates a tombstone entity and marks the original as superseded.

    Args:
        recurrence_id: UUID of the recurrence

    Returns:
        204: No content (successful deletion)
        404: Recurrence not found
        401: Authentication required (if no valid auth provided)
    """
    with get_core(atomic=True) as core:
        # Verify recurrence exists
        core.recurrence.get_by_id(recurrence_id)

        # Create tombstone entity
        tombstone_id = core.entity.create("recurrences")

        # Mark original as superseded
        core.entity.supersede(recurrence_id, tombstone_id)

    return "", 204
