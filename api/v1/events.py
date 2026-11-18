"""Calendar Event review actions (RFC-014 §6).

The belief-revision action: marks a Core Event entity as occurred. This
is the ONLY path from schedule (Core, mutable belief) to memory (Soil,
immutable fact) — never automatic, never on date passage (RFC-014 D1/D3).

Authz (RFC-014 §6.2): operator JWT or agent-bound API key. Unbound API
keys are refused (they share the owning username's frame — not separate
principals, RFC-010), keeping the fact stream's attribution honest.
Every marking writes an Action fact (actor per RFC-010 attribution)
alongside the EventOccurrence fact.

Cross-DB ordering follows the provider seam (RFC-014 §3.2): Core commits
before Soil — a crash between them can lose the occurrence fact (the
repair path recreates it from the entity's stored stamp on the next mark
call) but can never leave a fact whose entity update did not commit.
"""

import copy
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

from flask import Blueprint, g, jsonify, request

from system.core import get_core
from system.exceptions import (
    PermissionDenied,
    ResourceNotFound,
    ValidationError,
)
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime, uid

from ..schemas.event import MarkOccurredRequest
from ..validation import validate_request

logger = logging.getLogger(__name__)

calendar_events_bp = Blueprint(
    "calendar_events", __name__, url_prefix="/events"
)

# Clock-skew tolerance for "the future" checks — a just-ended event may
# legitimately be marked within moments of its end time.
_MARK_TOLERANCE = timedelta(minutes=5)

# Double-submit serialization for mark-occurred (see _mark_locked).
_MARK_LOCK = threading.Lock()

# Event entity fields captured in the occurrence snapshot (immutable
# memory of the schedule as believed at marking time, RFC-014 §6.3).
_SNAPSHOT_FIELDS = (
    "title",
    "starts_at",
    "ends_at",
    "location",
    "participants",
    "organizer",
    "status",
)


def _parse_occurred_at(raw: str | None) -> str:
    """Validate/normalize the occurred_at stamp.

    Default now; must be timezone-aware; must not postdate the marking
    (beyond clock-skew tolerance) — an occurrence cannot happen after it
    is recorded, or the fact stream stops meaning anything.
    """
    now = datetime.now(UTC)
    if raw is None or raw == "":
        return now.isoformat()
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(
            f"occurred_at is not a parseable ISO datetime: {raw!r}",
            {"field": "occurred_at"},
        ) from None
    if moment.tzinfo is None:
        raise ValidationError(
            "occurred_at must carry a timezone offset — ground truth is absolute",
            {"field": "occurred_at"},
        )
    if moment > now + _MARK_TOLERANCE:
        raise ValidationError(
            "occurred_at lies in the future — an occurrence cannot postdate "
            "its marking",
            {"field": "occurred_at"},
        )
    return moment.astimezone(UTC).isoformat()


def _load_event(core, event_uuid: str) -> dict:
    plain = uid.strip_prefix(event_uuid)
    try:
        entity = core.entity.get_by_id(plain)
    except ResourceNotFound:
        raise
    if entity["type"] != "Event":
        raise ValidationError(
            f"entity {plain} is a {entity['type']}, not an Event — "
            "only Events mark occurred",
            {"event_uuid": plain, "entity_type": entity["type"]},
        )
    return entity


def _existing_occurrence(soil, plain_uuid: str) -> dict | None:
    """Idempotency probe: the EventOccurrence fact for this event, if any.

    Private-connection access mirrors the tada provider / RFC-008 checker
    precedent; Soil has no public content query yet.
    """
    row = soil._get_connection().execute(
        """SELECT * FROM item
           WHERE _type = 'EventOccurrence'
             AND superseded_by IS NULL
             AND json_extract(data, '$.event_uuid') IN (?, ?)
           LIMIT 1""",
        (plain_uuid, uid.add_core_prefix(plain_uuid)),
    ).fetchone()
    return dict(row) if row else None


def _occurrence_view(row: dict) -> dict:
    return {
        "uuid": row["uuid"],
        "realized_at": row["realized_at"],
        "canonical_at": row["canonical_at"],
        "data": row["data"] if isinstance(row["data"], dict)
        else json.loads(row["data"]),
    }


@calendar_events_bp.post("/<event_uuid>/mark-occurred")
@validate_request
def mark_occurred(event_uuid: str, data: MarkOccurredRequest):
    """Mark a Core Event entity as occurred (RFC-014 §6).

    Authz: operator JWT or agent-bound API key (RFC-010 attribution);
    unbound API keys are refused with 403.

    Idempotent: marking an already-marked event returns the existing
    EventOccurrence fact (200, no new facts, no duplicate Action). If the
    entity is marked but its fact is missing (crash between the Core and
    Soil commits), the facts are repaired from the entity's stored stamp.

    Returns:
        201: First marking — the EventOccurrence + Action facts created.
        200: Idempotent re-mark or repair — the existing occurrence.

    Raises:
        ResourceNotFound: Unknown event uuid (404)
        ValidationError: Not an Event / bad occurred_at (400)
        PermissionDenied: Unbound API key (403)
    """
    # Authz gate BEFORE any reads or writes (RFC-014 §6.2).
    if getattr(g, "actor_type", None) not in ("operator", "agent"):
        raise PermissionDenied(
            "mark-occurred requires an operator session (JWT) or an "
            "agent-bound API key; unbound API keys cannot mark occurrences",
            {"actor_type": getattr(g, "actor_type", None)},
        )

    occurred_at = _parse_occurred_at(data.occurred_at)
    plain = uid.strip_prefix(event_uuid)
    now = isodatetime.now()

    # Double-submit serialization (review P1): a process-local lock around
    # probe → core update → fact write. Covers the realistic double-submit
    # case (review UI); a cross-worker residual race remains and is
    # documented — the airtight fix is a partial unique index on
    # EventOccurrence.event_uuid (schema-version bump, deferred; the
    # runtime upsert path shares this posture).
    with _MARK_LOCK:
        return _mark_locked(plain, occurred_at, now)


def _mark_locked(plain: str, occurred_at: str, now: str):
    with get_core() as core:
        entity = _load_event(core, plain)
        entity_data = entity.get("data") or {}

        with get_soil() as soil:
            existing = _existing_occurrence(soil, plain)
        if existing is not None:
            return jsonify({
                "idempotent": True,
                "event_uuid": uid.add_core_prefix(plain),
                "occurrence": _occurrence_view(existing),
            }), 200

        repaired = False
        if entity_data.get("occurred_at"):
            # Crash-repair (core-first ordering): the entity was marked
            # but its facts never landed — recreate them from the stored
            # stamp instead of overwriting the operator's ground truth.
            repaired = True
            occurred_at = entity_data["occurred_at"]

        # Deepcopy: the snapshot must not share mutable structure with the
        # dict handed to update_data (review P2).
        snapshot = {
            field: copy.deepcopy(entity_data.get(field))
            for field in _SNAPSHOT_FIELDS
        }

        if not repaired:
            core.entity.update_data(
                plain, {**entity_data, "occurred_at": occurred_at}
            )
        # Core-first (RFC-014 §3.2): the entity update commits before the
        # facts do.
        core._conn.commit()

        occurrence_uuid = generate_soil_uuid()
        action_uuid = generate_soil_uuid()
        with get_soil() as soil:
            # In-transaction re-probe: under the process lock this is
            # belt-and-suspenders; if the lock is ever bypassed
            # (multi-worker), it narrows the duplicate window to the
            # SQLite write-serialization point.
            duplicate = _existing_occurrence(soil, plain)
            if duplicate is not None:
                return jsonify({
                    "idempotent": True,
                    "event_uuid": uid.add_core_prefix(plain),
                    "occurrence": _occurrence_view(duplicate),
                }), 200
            soil.create_fact(
                Fact(
                    uuid=occurrence_uuid,
                    _type="EventOccurrence",
                    realized_at=now,
                    canonical_at=occurred_at,
                    data={
                        # core_-prefixed per the entitydelta/action schema
                        # convention for entity references.
                        "event_uuid": uid.add_core_prefix(plain),
                        "occurred_at": occurred_at,
                        "snapshot": snapshot,
                    },
                    metadata={
                        "source": entity_data.get("source"),
                        "account": entity_data.get("account"),
                        "calendar_id": entity_data.get("calendar_id"),
                        "source_id": entity_data.get("source_id"),
                    },
                    integrity_hash=None,  # computed by create_fact
                    fidelity="full",
                )
            )
            soil.create_fact(
                Fact(
                    uuid=action_uuid,
                    _type="Action",
                    realized_at=now,
                    canonical_at=now,
                    data={
                        "actor": g.actor,
                        "actor_type": g.actor_type,
                        "operation": "mark_occurred",
                        "params": {
                            "event_uuid": uid.add_core_prefix(plain),
                            "occurred_at": occurred_at,
                        },
                        "context": "RFC-014 §6 mark-occurred review action",
                    },
                    integrity_hash=None,
                    fidelity="full",
                )
            )
        # Soil commits on context exit — both facts land atomically.

        logger.info(
            "Event marked occurred: %s by %s (%s)%s",
            plain, g.actor, g.actor_type, " [repaired]" if repaired else "",
        )
        return jsonify({
            "idempotent": False,
            "repaired": repaired,
            "event_uuid": uid.add_core_prefix(plain),
            "occurred_at": occurred_at,
            "occurrence_uuid": occurrence_uuid,
            "action_uuid": action_uuid,
        }), 201


# ============================================================================
# Occurrence review queue (RFC-015 §3.3)
# ============================================================================


@calendar_events_bp.get("/pending-review")
def pending_review():
    """List Events awaiting occurrence review (RFC-015 §3.3). JWT-only.

    An Event is a belief; only review creates memory (RFC-014 D3). This
    read backs the console's review queue: events whose scheduled end has
    passed within the review window, `occurred_at` still null, and no
    EventOccurrence fact yet. Date-only events (all-day) compare
    lexicographically against the ISO cutoff — a past `YYYY-MM-DD` sorts
    before any full ISO timestamp of today, a future one sorts after it.

    Events that did not happen simply stay unmarked (no dismissal path
    exists — RFC-015 §4.2); they age out of the window.

    Query params:
        window_days: how far back to look (default 30, cap 365)
        limit: row cap (default 100, cap 500)

    Returns:
        200: {"events": [...], "count": n, "total_unmarked": n,
              "window_days": k}
    """
    from ..middleware.decorators import _authenticate_jwt

    _authenticate_jwt()

    raw_window = request.args.get("window_days", "30")
    raw_limit = request.args.get("limit", "100")
    try:
        window_days = max(1, min(int(raw_window), 365))
        limit = max(1, min(int(raw_limit), 500))
    except ValueError:
        raise ValidationError(
            "window_days/limit must be integers",
            {"window_days": raw_window, "limit": raw_limit},
        ) from None

    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    now = isodatetime.now()

    core = get_core()
    where = """superseded_by IS NULL AND type = 'Event'
        AND json_extract(data, '$.occurred_at') IS NULL
        AND COALESCE(json_extract(data, '$.ends_at'),
                     json_extract(data, '$.starts_at')) <= ?
        AND COALESCE(json_extract(data, '$.ends_at'),
                     json_extract(data, '$.starts_at')) >= ?"""
    params: list = [now, cutoff]

    rows = core._conn.execute(
        f"""SELECT uuid, data, updated_at FROM entity
            WHERE {where}
            ORDER BY COALESCE(json_extract(data, '$.ends_at'),
                              json_extract(data, '$.starts_at')) DESC
            LIMIT {limit}""",
        params,
    ).fetchall()
    total_unmarked = core._conn.execute(
        f"SELECT COUNT(*) AS n FROM entity WHERE {where}", params
    ).fetchone()["n"]

    # Exclude events that already have an occurrence fact (idempotency
    # probe pattern from mark_occurred — occurrence data stores the
    # core_-prefixed event uuid).
    candidates = {row["uuid"]: row for row in rows}
    marked = set()
    if candidates:
        with get_soil() as soil:
            sconn = soil._get_connection()
            placeholders = ",".join("?" * len(candidates))
            marked = {
                r["ev"]
                for r in sconn.execute(
                    f"""SELECT DISTINCT json_extract(data, '$.event_uuid') AS ev
                        FROM item
                        WHERE _type = 'EventOccurrence' AND superseded_by IS NULL
                          AND json_extract(data, '$.event_uuid') IN ({placeholders})""",
                    [uid.add_core_prefix(u) for u in candidates],
                ).fetchall()
            }

    events = []
    for row in rows:
        if uid.add_core_prefix(row["uuid"]) in marked:
            continue
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        events.append({
            "uuid": uid.add_core_prefix(row["uuid"]),
            "title": data.get("title"),
            "starts_at": data.get("starts_at"),
            "ends_at": data.get("ends_at"),
            "status": data.get("status"),
            "calendar_id": data.get("calendar_id"),
            "account": data.get("account"),
            "captured_after_fact": data.get("captured_after_fact", False),
            "updated_at": row["updated_at"],
        })

    return jsonify({
        "events": events,
        "count": len(events),
        "total_unmarked": total_unmarked,
        "window_days": window_days,
    })
