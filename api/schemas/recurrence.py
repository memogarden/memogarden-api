"""
Recurrence Pydantic schemas for API request/response validation.

These schemas define the API contract for recurrence operations:
- RecurrenceBase: Shared fields between Create and Response
- RecurrenceCreate: Request body for creating recurrences (POST)
- RecurrenceUpdate: Request body for updating recurrences (PUT/PATCH, all fields optional)
- RecurrenceResponse: API response including recurrence + entity metadata

Recurrences are templates for generating recurring transactions. The `entities` field
contains JSON describing the transaction template(s) to generate on each occurrence.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RecurrenceBase(BaseModel):
    """Shared fields between RecurrenceCreate and RecurrenceResponse."""

    rrule: str = Field(..., description="iCal RRULE string (e.g., 'FREQ=MONTHLY;BYDAY=2FR')")
    entities: str = Field(..., description="JSON: transaction template(s) to generate")
    valid_from: datetime = Field(..., description="Start of recurrence window (ISO 8601 UTC)")
    valid_until: datetime | None = Field(default=None, description="End of recurrence window (ISO 8601 UTC, null = forever)")


class RecurrenceCreate(RecurrenceBase):
    """
    Request body for creating a recurrence.

    The `entities` field contains a JSON string with transaction template data.
    This defines what transaction(s) to generate on each occurrence.

    Example:
    ```json
    {
        "rrule": "FREQ=MONTHLY;BYDAY=2FR",
        "entities": "[{\\"amount\\": -1500,\\"currency\\":\\"SGD\\",\\"description\\":\\"Rent\\",\\"account\\":\\"Household\\"}]",
        "valid_from": "2025-01-01T00:00:00Z",
        "valid_until": null
    }
    ```
    """
    pass


class RecurrenceUpdate(BaseModel):
    """
    Request body for updating a recurrence (all fields optional).

    Allows partial updates - only provided fields will be updated.
    Entity metadata (created_at, updated_at) is managed automatically.

    Example (updating only rrule and valid_until):
    ```json
    {
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
        "valid_until": "2025-12-31T23:59:59Z"
    }
    ```
    """

    rrule: str | None = None
    entities: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class RecurrenceResponse(RecurrenceBase):
    """
    API response with recurrence data and entity metadata.

    Includes all recurrence fields plus entity metadata from the registry.
    This schema represents data from the recurrences_view (join of recurrences + entity).

    Example:
    ```json
    {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "rrule": "FREQ=MONTHLY;BYDAY=2FR",
        "entities": "[{\\"amount\\": -1500,\\"description\\":\\"Rent\\"}]",
        "valid_from": "2025-01-01T00:00:00Z",
        "valid_until": null,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "superseded_by": null,
        "superseded_at": null,
        "group_id": null,
        "derived_from": null
    }
    ```
    """

    id: str = Field(..., description="UUID of the recurrence")

    # Entity metadata from registry
    created_at: datetime = Field(..., description="When the entity was created (ISO 8601 UTC)")
    updated_at: datetime = Field(..., description="When the entity was last updated (ISO 8601 UTC)")
    superseded_by: str | None = Field(default=None, description="ID of superseding entity (if superseded)")
    superseded_at: datetime | None = Field(default=None, description="When the entity was superseded")
    group_id: str | None = Field(default=None, description="Optional grouping/clustering of related entities")
    derived_from: str | None = Field(default=None, description="Provenance: ID of source entity")

    model_config = ConfigDict(
        from_attributes=True  # Enable ORM mode for easier database row mapping
    )
