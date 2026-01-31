"""
Transaction Pydantic schemas for API request/response validation.

These schemas define the API contract for transaction operations:
- TransactionBase: Shared fields between Create and Response
- TransactionCreate: Request body for creating transactions (POST)
- TransactionUpdate: Request body for updating transactions (PUT/PATCH, all fields optional)
- TransactionResponse: API response including transaction + entity metadata + hash chain

Per PRD v6:
- UUIDs use core_ prefix in API responses
- Hash chain enables optimistic locking and conflict detection
- based_on_hash/based_on_version for conflict detection on updates

Note: Accounts and categories are labels (strings), not relational entities.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class TransactionBase(BaseModel):
    """Shared fields between TransactionCreate and TransactionResponse."""

    amount: float = Field(..., description="Transaction amount (positive or negative)")
    currency: str = Field(default="SGD", description="Currency code (e.g., SGD, USD)")
    transaction_date: date = Field(..., description="Date of transaction (YYYY-MM-DD)")
    description: str = Field(default="", description="Short title (e.g., 'Coffee at Starbucks')")
    account: str = Field(..., description="Account label (e.g., 'Household', 'Personal')")
    category: str | None = Field(default=None, description="Category label (e.g., 'Food', 'Transport')")
    notes: str | None = Field(default=None, description="Optional longer details")


class TransactionCreate(TransactionBase):
    """
    Request body for creating a transaction.

    All business fields are required except optional ones (category, notes).
    ID, author, and timestamps are generated server-side.

    Example:
    ```json
    {
        "amount": -15.50,
        "currency": "SGD",
        "transaction_date": "2025-12-23",
        "description": "Coffee at Starbucks",
        "account": "Personal",
        "category": "Food",
        "notes": "Morning coffee with colleague"
    }
    ```
    """
    pass


class TransactionUpdate(BaseModel):
    """
    Request body for updating a transaction (all fields optional).

    Allows partial updates - only provided fields will be updated.
    Entity metadata (created_at, updated_at) is managed automatically.

    For optimistic locking, provide either based_on_hash or based_on_version
    from a previous GET response. If the entity has been modified by another
    client, the update will return a 409 Conflict response.

    Example (updating only amount and category with conflict detection):
    ```json
    {
        "amount": -16.00,
        "category": "Food & Drinks",
        "based_on_hash": "a1b2c3d4e5f6..."
    }
    ```
    """

    amount: float | None = None
    currency: str | None = None
    transaction_date: date | None = None
    description: str | None = None
    account: str | None = None
    category: str | None = None
    notes: str | None = None

    # Optimistic locking fields (at least one recommended for updates)
    based_on_hash: str | None = Field(
        default=None,
        description="Hash from previous GET response (for conflict detection)"
    )
    based_on_version: int | None = Field(
        default=None,
        description="Version number from previous GET response (alternative to hash)"
    )


class TransactionResponse(TransactionBase):
    """
    API response with transaction data, entity metadata, and hash chain.

    Includes all transaction fields plus:
    - uuid: UUID of the transaction (with core_ prefix)
    - hash: Current state hash (for optimistic locking)
    - previous_hash: Previous state hash (NULL for initial version)
    - version: Monotonically increasing version number
    - author: User or agent who created the transaction
    - recurrence_id: Optional link to recurring transaction template
    - Entity metadata from registry: created_at, updated_at, superseded_by, etc.

    This schema represents data from the transactions_view (join of transactions + entity).

    Per PRD v6, the UUID includes the core_ prefix to distinguish Core entities
    from future Soil items.

    Example:
    ```json
    {
        "uuid": "core_550e8400-e29b-41d4-a716-446655440000",
        "amount": -15.50,
        "currency": "SGD",
        "transaction_date": "2025-12-23",
        "description": "Coffee at Starbucks",
        "account": "Personal",
        "category": "Food",
        "notes": "Morning coffee",
        "author": "user@example.com",
        "recurrence_id": null,
        "hash": "a1b2c3d4e5f6...",
        "previous_hash": null,
        "version": 1,
        "created_at": "2025-12-23T06:31:33.544668Z",
        "updated_at": "2025-12-23T06:31:33.544668Z",
        "superseded_by": null,
        "superseded_at": null,
        "group_id": null,
        "derived_from": null
    }
    ```
    """

    # Use uuid instead of id (PRD v6 naming)
    uuid: str = Field(..., description="UUID of the transaction (with core_ prefix)")
    author: str = Field(default="system", description="User or agent who created the transaction")
    recurrence_id: str | None = Field(default=None, description="FK to recurrence entity (if recurring)")

    # Hash chain fields (PRD v6)
    hash: str = Field(..., description="Current state hash (for optimistic locking)")
    previous_hash: str | None = Field(default=None, description="Previous state hash (NULL for initial)")
    version: int = Field(..., description="Entity version number (monotonically increasing)")

    # Entity metadata from registry
    created_at: datetime = Field(..., description="When the entity was created (ISO 8601 UTC)")
    updated_at: datetime = Field(..., description="When the entity was last updated (ISO 8601 UTC)")
    superseded_by: str | None = Field(default=None, description="UUID of superseding entity (if superseded)")
    superseded_at: datetime | None = Field(default=None, description="When the entity was superseded")
    group_id: str | None = Field(default=None, description="Optional grouping/clustering of related entities")
    derived_from: str | None = Field(default=None, description="Provenance: UUID of source entity")

    model_config = ConfigDict(
        from_attributes=True  # Enable ORM mode for easier database row mapping
    )


class ConflictResponse(BaseModel):
    """Response returned when update conflicts with current state (409 Conflict)."""

    error: str = Field(default="Conflict", description="Error type")
    message: str = Field(..., description="Human-readable conflict description")
    current_hash: str = Field(..., description="Current entity hash on server")
    current_version: int = Field(..., description="Current entity version on server")
    client_hash: str | None = Field(default=None, description="Hash provided by client")
    client_version: int | None = Field(default=None, description="Version provided by client")
