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


class DismissRequest(BaseModel):
    """Body for POST /events/<uuid>/dismiss (RFC-014 amendment
    2026-11-26, api#57) — mark-occurred's symmetric sibling.

    dismissed_at defaults to now and carries NO not-in-the-future rule:
    dismissing a future slot is legitimate (the slot will not happen);
    occurred_at keeps its rule because an occurrence cannot postdate its
    stamp. reason is optional free-form and rides the verdict fact's
    metadata — the entity stamps stay exactly as the amendment ruled
    (data.did_not_occur + data.dismissed_at).
    """

    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional free-form reason; recorded on the verdict "
        "fact's metadata for the audit trail.",
    )
    dismissed_at: str | None = Field(
        default=None,
        description="ISO datetime (with timezone offset) of the dismissal "
        "decision; defaults to now. No future rule — dismissing a future "
        "slot is legitimate.",
    )
