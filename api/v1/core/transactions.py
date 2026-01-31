"""Transaction CRUD endpoints for MemoGarden Core API.

This module implements RESTful endpoints for transaction management:
- POST   /api/v1/transactions                 - Create transaction
- GET    /api/v1/transactions                 - List with filtering
- GET    /api/v1/transactions/{id}            - Get single transaction
- PUT    /api/v1/transactions/{id}            - Update transaction
- DELETE /api/v1/transactions/{id}            - Delete transaction
- GET    /api/v1/transactions/accounts        - List distinct accounts
- GET    /api/v1/transactions/categories      - List distinct categories

All endpoints require authentication via JWT token or API key (enforced via before_request).

Per PRD v6:
- UUIDs use core_ prefix in responses
- Hash chain enables optimistic locking
- based_on_hash/based_on_version for conflict detection on updates

Architecture Notes:
- Accounts and categories are labels (strings), not relational entities
- Entity registry pattern: create entity first, then transaction
- ISO 8601 timestamps for all date/time fields
- Parameterized queries to prevent SQL injection
- Author field tracks which user created each transaction
"""

import logging
from flask import Blueprint, g, jsonify, request

from system.core import get_core
from system.utils import uid
from ...validation import validate_request
from ...schemas.transaction import ConflictResponse, TransactionCreate, TransactionUpdate

logger = logging.getLogger(__name__)

# Create Blueprint
transactions_bp = Blueprint('transactions', __name__, url_prefix='/transactions')


def _row_to_transaction_response(row) -> dict:
    """
    Convert a database row from transactions_view to TransactionResponse dict.

    Adds core_ prefix to UUIDs and includes hash chain fields.

    Args:
        row: SQLite Row object from transactions_view

    Returns:
        Dictionary matching TransactionResponse schema (PRD v6 compliant)
    """
    # Get the UUID (row has both 'id' from transactions and 'uuid' from the join)
    # The view creates 'uuid' alias for t.id
    entity_uuid = row.get("uuid") or row["id"]

    return {
        "uuid": uid.add_core_prefix(entity_uuid),
        "id": uid.add_core_prefix(entity_uuid),  # Legacy field for compatibility
        "amount": row["amount"],
        "currency": row["currency"],
        "transaction_date": row["transaction_date"],
        "description": row["description"],
        "account": row["account"],
        "category": row["category"],
        "notes": row["notes"],
        "author": row["author"],
        "recurrence_id": uid.add_core_prefix(row["recurrence_id"]) if row.get("recurrence_id") else None,
        # Hash chain fields (PRD v6)
        "hash": row["hash"],
        "previous_hash": row.get("previous_hash"),
        "version": row["version"],
        # Entity metadata
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "superseded_by": uid.add_core_prefix(row["superseded_by"]) if row.get("superseded_by") else None,
        "superseded_at": row.get("superseded_at"),
        "group_id": uid.add_core_prefix(row["group_id"]) if row.get("group_id") else None,
        "derived_from": uid.add_core_prefix(row["derived_from"]) if row.get("derived_from") else None,
    }


@transactions_bp.post("")
@validate_request
def create_transaction(data: TransactionCreate):
    """
    Create a new transaction.

    Requires authentication via JWT token or API key (enforced by before_request).
    The authenticated user's username is stored as the transaction author.

    Request Body (TransactionCreate):
        - amount: float (required)
        - currency: str (default: "SGD")
        - transaction_date: date (ISO 8601 YYYY-MM-DD, required)
        - description: str (default: "")
        - account: str (required)
        - category: str | None (default: None)
        - notes: str | None (default: None)

    Returns:
        201: TransactionResponse with created transaction (uuid includes core_ prefix)
        400: Validation error
        401: Authentication required (if no valid auth provided)
    """
    # Use atomic transaction for coordinated entity + transaction creation
    # Get author from authenticated user (set by before_request:authenticate)
    author = g.username

    with get_core(atomic=True) as core:
        transaction_id = core.transaction.create(
            amount=data.amount,
            transaction_date=data.transaction_date,
            description=data.description,
            account=data.account,
            category=data.category,
            notes=data.notes,
            author=author  # Track who created this transaction
        )

    # Fetch created transaction with fresh Core (connection closed after atomic block)
    core = get_core()
    row = core.transaction.get_by_id(transaction_id)

    return jsonify(_row_to_transaction_response(row)), 201


@transactions_bp.get("/<transaction_id>")
def get_transaction(transaction_id: str):
    """
    Get a single transaction by ID.

    Requires authentication via JWT token or API key (enforced by before_request).

    Args:
        transaction_id: UUID of the transaction (with or without core_ prefix)

    Returns:
        200: TransactionResponse (uuid includes core_ prefix)
        404: Transaction not found
        401: Authentication required (if no valid auth provided)
    """
    core = get_core()
    row = core.transaction.get_by_id(transaction_id)

    return jsonify(_row_to_transaction_response(row))


@transactions_bp.get("")
def list_transactions():
    """
    List transactions with optional filtering.

    Requires authentication via JWT token or API key (enforced by before_request).

    Query Parameters:
        - start_date: ISO 8601 date (YYYY-MM-DD) - Filter from this date
        - end_date: ISO 8601 date (YYYY-MM-DD) - Filter until this date
        - account: str - Filter by account label
        - category: str - Filter by category label
        - include_superseded: bool - Include superseded transactions (default: false)
        - limit: int - Maximum results to return (default: 100)
        - offset: int - Number of results to skip (default: 0)

    Returns:
        200: Array of TransactionResponse objects (uuids include core_ prefix)
        401: Authentication required (if no valid auth provided)
    """
    # Parse query parameters
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    account = request.args.get("account")
    category = request.args.get("category")
    include_superseded = request.args.get("include_superseded", "false").lower() == "true"
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))

    filters = {
        "account": account,
        "category": category,
        "start_date": start_date,
        "end_date": end_date,
        "include_superseded": include_superseded
    }

    core = get_core()
    rows = core.transaction.list(filters, limit=limit, offset=offset)

    return jsonify([_row_to_transaction_response(row) for row in rows])


@transactions_bp.put("/<transaction_id>")
@validate_request
def update_transaction(transaction_id: str, data: TransactionUpdate):
    """
    Update a transaction with optimistic locking.

    Requires authentication via JWT token or API key.

    Only provided fields are updated (partial update).
    If based_on_hash or based_on_version is provided and doesn't match
    the current entity state, returns 409 Conflict.

    Args:
        transaction_id: UUID of the transaction (with or without core_ prefix)

    Request Body (TransactionUpdate):
        All fields optional:
        - amount: float | None
        - currency: str | None
        - transaction_date: date | None
        - description: str | None
        - account: str | None
        - category: str | None
        - notes: str | None
        - based_on_hash: str | None (for conflict detection)
        - based_on_version: int | None (alternative to hash)

    Returns:
        200: TransactionResponse with updated transaction
        404: Transaction not found
        409: Conflict - entity was modified by another client
        400: Validation error
        401: Authentication required
    """
    # Strip prefix if provided
    transaction_id = uid.strip_prefix(transaction_id)

    core = get_core()

    # Get current state for conflict detection
    current_row = core.transaction.get_by_id(transaction_id)
    current_hash = current_row["hash"]
    current_version = current_row["version"]

    # Check for conflicts if client provided hash or version
    if data.based_on_hash and data.based_on_hash != current_hash:
        conflict_response = ConflictResponse(
            message="Transaction was modified by another client",
            current_hash=current_hash,
            current_version=current_version,
            client_hash=data.based_on_hash,
        )
        return jsonify(conflict_response.model_dump()), 409

    if data.based_on_version is not None and data.based_on_version != current_version:
        conflict_response = ConflictResponse(
            message="Transaction version mismatch",
            current_hash=current_hash,
            current_version=current_version,
            client_version=data.based_on_version,
        )
        return jsonify(conflict_response.model_dump()), 409

    # Build update data from only provided fields (excluding optimistic locking fields)
    update_data = data.model_dump(exclude_unset=True, exclude={"based_on_hash", "based_on_version"})

    if update_data:
        core.transaction.update(transaction_id, update_data)

    # Fetch updated transaction
    row = core.transaction.get_by_id(transaction_id)

    return jsonify(_row_to_transaction_response(row))


@transactions_bp.delete("/<transaction_id>")
def delete_transaction(transaction_id: str):
    """
    Delete a transaction (soft delete via superseding).

    Requires authentication via JWT token or API key (enforced by before_request).

    Creates a tombstone entity and marks the original as superseded.

    Args:
        transaction_id: UUID of the transaction (with or without core_ prefix)

    Returns:
        204: No content (successful deletion)
        404: Transaction not found
        401: Authentication required (if no valid auth provided)
    """
    # Strip prefix if provided
    transaction_id = uid.strip_prefix(transaction_id)

    with get_core(atomic=True) as core:
        # Verify transaction exists
        core.transaction.get_by_id(transaction_id)

        # Create tombstone entity
        tombstone_id = core.entity.create("transactions")

        # Mark original as superseded
        core.entity.supersede(transaction_id, tombstone_id)

    return "", 204


@transactions_bp.get("/accounts")
def list_accounts():
    """
    List distinct account labels.

    Requires authentication via JWT token or API key (enforced by before_request).

    Useful for UI autocomplete/dropdowns.
    Accounts are simple string labels, not entities.

    Returns:
        200: Array of account label strings
        401: Authentication required (if no valid auth provided)
    """
    core = get_core()
    rows = core.transaction.get_accounts()

    return jsonify(rows)


@transactions_bp.get("/categories")
def list_categories():
    """
    List distinct category labels.

    Requires authentication via JWT token or API key (enforced by before_request).

    Useful for UI autocomplete/dropdowns.
    Categories are simple string labels, not entities.

    Returns:
        200: Array of category label strings
        401: Authentication required (if no valid auth provided)
    """
    core = get_core()
    rows = core.transaction.get_categories()

    return jsonify(rows)
