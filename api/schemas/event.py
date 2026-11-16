"""Request schemas for calendar Event review actions (RFC-014 §6)."""

from pydantic import BaseModel, Field


class MarkOccurredRequest(BaseModel):
    """Body for POST /events/<uuid>/mark-occurred (RFC-014 §6.1).

    occurred_at is real-world ground truth supplied by review — never by
    the provider, never by date passage (RFC-014 D3).
    """

    occurred_at: str | None = Field(
        default=None,
        description="ISO datetime (with timezone offset) the event actually "
        "occurred; defaults to now.",
    )
