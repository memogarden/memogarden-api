"""Calendar Event review actions (RFC-014 §6).

The belief-revision action: marks a Core Event entity as occurred. This
is the ONLY path from schedule (Core, mutable belief) to memory (Soil,
immutable fact) — never automatic, never on date passage (RFC-014 D1/D3).
Since the 2026-11-19 convergence (#31), the memory it creates IS the
universal Event fact (the same type the tada lane lands; the legacy
EventOccurrence type is retired — zero instances existed at convergence).

Authz (RFC-014 §6.2): operator JWT or agent-bound API key. Unbound API
keys are refused (they share the owning username's frame — not separate
principals, RFC-010), keeping the fact stream's attribution honest.
Every marking writes an Action fact (actor per RFC-010 attribution)
alongside the Event fact.

Cross-DB ordering follows the provider seam (RFC-014 §3.2): Core commits
before Soil — a crash between them can lose the occurrence fact (the
repair path recreates it from the entity's stored stamp on the next mark
call) but can never leave a fact whose entity update did not commit.
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from flask import Blueprint, g, jsonify, request

from system.core import get_core
from system.exceptions import (
    LockConflictError,
    PermissionDenied,
    ResourceNotFound,
    ValidationError,
)
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime, uid

from ..schemas.event import DismissRequest, MarkOccurredRequest
from ..validation import validate_request

logger = logging.getLogger(__name__)

calendar_events_bp = Blueprint(
    "calendar_events", __name__, url_prefix="/events"
)

# Clock-skew tolerance for "the future" checks — a just-ended event may
# legitimately be marked within moments of its end time.
_MARK_TOLERANCE = timedelta(minutes=5)

# Review-action double-submit serialization for mark-occurred AND dismiss
# (see _mark_locked / _dismiss_locked). Process-local; the DB-level
# guarantee for cross-process races is the verdict identity index
# (system#5) — these locks narrow the window, the index closes it.
_REVIEW_LOCK = threading.Lock()

# Event entity fields projected FLAT into the Event fact (the 2026-11-19
# convergence: no snapshot nest — the occurrence memory is an authored
# Event record, schemas/event.yaml FACT section). The entity's belief
# status (confirmed/tentative) deliberately stays on the entity: the
# fact's verdict is metadata.status ("occurred"), not the schedule belief.
_FLAT_FIELDS = (
    "title",
    "starts_at",
    "ends_at",
    "location",
    "participants",
    "organizer",
)

_SCHEMA_FIELD_NAMES = {"starts_at": "start_at"}  # belief->record renames


def _to_schema_field(field: str) -> str:
    return _SCHEMA_FIELD_NAMES.get(field, field)


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
    """Idempotency probe: the Event VERDICT fact for this event, if any
    (#31 — the convergence retired the EventOccurrence type; zero
    instances existed). Status-filtered so a dismissal never satisfies a
    mark probe or vice versa.

    Private-connection access mirrors the tada provider / RFC-008 checker
    precedent; Soil has no public content query yet.
    """
    row = soil._get_connection().execute(
        """SELECT * FROM item
           WHERE _type = 'Event'
             AND superseded_by IS NULL
             AND json_extract(data, '$.event_uuid') IN (?, ?)
             AND json_extract(metadata, '$.status') = 'occurred'
           LIMIT 1""",
        (plain_uuid, uid.add_core_prefix(plain_uuid)),
    ).fetchone()
    return dict(row) if row else None


def _existing_dismissal(soil, plain_uuid: str) -> dict | None:
    """Idempotency probe for the dismiss action: the Event verdict fact
    with metadata.status='did_not_occur' (RFC-014 amendment 2026-11-26,
    api#57)."""
    row = soil._get_connection().execute(
        """SELECT * FROM item
           WHERE _type = 'Event'
             AND superseded_by IS NULL
             AND json_extract(data, '$.event_uuid') IN (?, ?)
             AND json_extract(metadata, '$.status') = 'did_not_occur'
           LIMIT 1""",
        (plain_uuid, uid.add_core_prefix(plain_uuid)),
    ).fetchone()
    return dict(row) if row else None


def _verdict_landed(soil, plain_uuid: str) -> dict | None:
    """Either verdict, post-IntegrityError: the identity index rejected
    our insert because a verdict (either kind) landed first."""
    return _existing_occurrence(soil, plain_uuid) or _existing_dismissal(
        soil, plain_uuid
    )


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
    Event fact (200, no new facts, no duplicate Action). If the
    entity is marked but its fact is missing (crash between the Core and
    Soil commits), the facts are repaired from the entity's stored stamp.

    Returns:
        201: First marking — the Event + Action facts created.
        200: Idempotent re-mark or repair — the existing occurrence.

    Raises:
        ResourceNotFound: Unknown event uuid (404)
        ValidationError: Not an Event / bad occurred_at (400)
        PermissionDenied: Unbound API key (403)
        LockConflictError: Review state already closed (409; the error
        envelope type is ConflictError, matching mg_client's mapping)
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

    # Review-action serialization: a process-local lock around probe →
    # core update → fact write. Covers the realistic double-submit case
    # (review UI); a cross-process race is closed by the verdict identity
    # index (system#5) — see the IntegrityError translation below.
    with _REVIEW_LOCK:
        return _mark_locked(plain, occurred_at, now)


def _mark_locked(plain: str, occurred_at: str, now: str):
    with get_core() as core:
        entity = _load_event(core, plain)
        entity_data = entity.get("data") or {}

        # Terminal review (RFC-014 amendment §3, api#57): a dismissal
        # closes the review state — mark-after-dismiss is 409.
        if entity_data.get("did_not_occur"):
            raise LockConflictError(
                "Event already dismissed as did-not-occur — review state "
                "is closed (RFC-014 amendment: review stamps are terminal)",
                {"event_uuid": uid.add_core_prefix(plain),
                 "dismissed_at": entity_data.get("dismissed_at"),
                 "code": "review_terminal"},
            )

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
            # belt-and-suspenders; across processes the verdict identity
            # index (system#5) is the guarantee — a raced double-insert
            # raises IntegrityError, which converges on the verdict that
            # landed first (idempotent), never duplicates it.
            duplicate = _existing_occurrence(soil, plain)
            if duplicate is not None:
                return jsonify({
                    "idempotent": True,
                    "event_uuid": uid.add_core_prefix(plain),
                    "occurrence": _occurrence_view(duplicate),
                }), 200
            # Flat Event fact (#31): the belief fields project directly
            # (starts_at -> start_at rename per the unified schema); the
            # verdict is metadata.status ("occurred" — source vocabulary).
            flat_data = {
                "event_uuid": uid.add_core_prefix(plain),
                "source_id": entity_data.get("source_id"),
                "kind": "event",
            }
            flat_data.update({
                _to_schema_field(f): entity_data.get(f)
                for f in _FLAT_FIELDS
            })
            try:
                soil.create_fact(
                    Fact(
                        uuid=occurrence_uuid,
                        _type="Event",
                        realized_at=now,
                        canonical_at=occurred_at,
                        data=flat_data,
                        metadata={
                            "source": entity_data.get("source"),
                            "status": "occurred",
                            "resolved_at": occurred_at,
                            "account": entity_data.get("account"),
                            "calendar_id": entity_data.get("calendar_id"),
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
            except sqlite3.IntegrityError:
                # system#5: the verdict identity index rejected the insert
                # — a cross-process verdict for this event landed between
                # our probe and our write. Roll back and converge on
                # whichever verdict landed first (idempotent), never
                # duplicate it.
                soil._get_connection().rollback()
                verdict = _verdict_landed(soil, plain)
                if verdict is not None:
                    return jsonify({
                        "idempotent": True,
                        "event_uuid": uid.add_core_prefix(plain),
                        "occurrence": _occurrence_view(verdict),
                    }), 200
                raise
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
# Dismiss (RFC-014 amendment 2026-11-26, api#57) — the did-not-occur verdict
# ============================================================================


def _parse_dismissed_at(raw: str | None) -> str:
    """Validate/normalize the dismissal stamp.

    Same absolute-ground-truth rules as occurred_at (parseable ISO,
    timezone-aware, normalized to UTC) MINUS the future rule: dismissing
    a future slot is legitimate — the slot will not happen (RFC-014
    amendment §1).
    """
    if raw is None:
        return isodatetime.now()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as err:
        raise ValidationError(
            f"dismissed_at is not a parseable ISO datetime: {raw!r}",
            {"field": "dismissed_at"},
        ) from err
    if parsed.tzinfo is None:
        raise ValidationError(
            "dismissed_at must carry a timezone offset — ground truth is "
            "absolute",
            {"field": "dismissed_at"},
        )
    return parsed.astimezone(UTC).isoformat()


@calendar_events_bp.post("/<event_uuid>/dismiss")
@validate_request
def dismiss_event(event_uuid: str, data: DismissRequest):
    """Dismiss a Core Event entity as did-not-occur (RFC-014 amendment
    2026-11-26, api#57).

    mark-occurred's symmetric sibling: stamps data.did_not_occur=true +
    data.dismissed_at on the entity (core-first), lands the universal
    Event verdict fact with metadata.status='did_not_occur' (a recorded
    non-occurrence, visible in the timeline rather than a silent gap)
    plus the audit Action fact.

    Authz: the §6.2 matrix — operator JWT or agent-bound API key;
    unbound API keys are refused with 403.

    Terminal: once either review stamp exists the state is closed —
    dismiss-after-mark and mark-after-dismiss are 409
    (review_terminal). No un-dismissing. Same-verb resubmits converge on
    the existing verdict (200, idempotent — verdicts are immutable, body
    differences included).

    Returns:
        201: First dismissal — the Event + Action facts created.
        200: Idempotent re-dismiss — the existing verdict.
        409: Review state already closed by the other verdict.

    Raises:
        ResourceNotFound: Unknown event uuid (404)
        ValidationError: Not an Event / bad dismissed_at (400)
        PermissionDenied: Unbound API key (403)
        LockConflictError: Review state already closed (409; the error
        envelope type is ConflictError, matching mg_client's mapping)
    """
    # Authz gate BEFORE any reads or writes (RFC-014 §6.2 matrix).
    if getattr(g, "actor_type", None) not in ("operator", "agent"):
        raise PermissionDenied(
            "dismiss requires an operator session (JWT) or an agent-bound "
            "API key; unbound API keys cannot record review verdicts",
            {"actor_type": getattr(g, "actor_type", None)},
        )

    dismissed_at = _parse_dismissed_at(data.dismissed_at)
    plain = uid.strip_prefix(event_uuid)
    now = isodatetime.now()

    with _REVIEW_LOCK:
        return _dismiss_locked(plain, dismissed_at, data.reason, now)


def _dismiss_locked(plain: str, dismissed_at: str, reason: str | None,
                    now: str):
    with get_core() as core:
        entity = _load_event(core, plain)
        entity_data = entity.get("data") or {}

        # Terminal review: dismiss-after-mark is 409 (the mirror guard to
        # mark-occurred's did_not_occur check).
        if entity_data.get("occurred_at"):
            raise LockConflictError(
                "Event already marked occurred — review state is closed "
                "(RFC-014 amendment: review stamps are terminal)",
                {"event_uuid": uid.add_core_prefix(plain),
                 "occurred_at": entity_data["occurred_at"],
                 "code": "review_terminal"},
            )

        with get_soil() as soil:
            existing = _existing_dismissal(soil, plain)
        if existing is not None:
            return jsonify({
                "idempotent": True,
                "event_uuid": uid.add_core_prefix(plain),
                "dismissal": _occurrence_view(existing),
            }), 200

        # Core-first (the mark path's ordering): entity stamp commits
        # before the facts — a crash here loses nothing that matters (a
        # re-dismiss converges idempotently; there is no repair variant
        # because the stamp carries the full verdict inputs).
        core.entity.update_data(
            plain,
            {**entity_data, "did_not_occur": True, "dismissed_at": dismissed_at},
        )
        core._conn.commit()

        verdict_uuid = generate_soil_uuid()
        action_uuid = generate_soil_uuid()
        with get_soil() as soil:
            duplicate = _existing_dismissal(soil, plain)
            if duplicate is not None:
                return jsonify({
                    "idempotent": True,
                    "event_uuid": uid.add_core_prefix(plain),
                    "dismissal": _occurrence_view(duplicate),
                }), 200
            # Flat Event fact, the mark path's projection; the verdict is
            # metadata.status='did_not_occur'. canonical_at follows the
            # cancelled honesty rule: a non-occurrence happened "at" its
            # missed slot, else at the dismissal decision.
            flat_data = {
                "event_uuid": uid.add_core_prefix(plain),
                "source_id": entity_data.get("source_id"),
                "kind": "event",
            }
            flat_data.update({
                _to_schema_field(f): entity_data.get(f)
                for f in _FLAT_FIELDS
            })
            verdict_metadata = {
                "source": entity_data.get("source"),
                "status": "did_not_occur",
                "resolved_at": dismissed_at,
                "account": entity_data.get("account"),
                "calendar_id": entity_data.get("calendar_id"),
            }
            if reason:
                verdict_metadata["reason"] = reason
            params = {
                "event_uuid": uid.add_core_prefix(plain),
                "dismissed_at": dismissed_at,
            }
            if reason:
                params["reason"] = reason
            try:
                soil.create_fact(
                    Fact(
                        uuid=verdict_uuid,
                        _type="Event",
                        realized_at=now,
                        canonical_at=entity_data.get("starts_at")
                        or dismissed_at,
                        data=flat_data,
                        metadata=verdict_metadata,
                        integrity_hash=None,
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
                            "operation": "dismiss_event",
                            "params": params,
                            "context": "RFC-014 amendment dismiss review "
                                       "action (api#57)",
                        },
                        integrity_hash=None,
                        fidelity="full",
                    )
                )
            except sqlite3.IntegrityError:
                # system#5: the verdict identity index rejected the insert
                # — converge on whichever verdict landed first.
                soil._get_connection().rollback()
                verdict = _verdict_landed(soil, plain)
                if verdict is not None:
                    return jsonify({
                        "idempotent": True,
                        "event_uuid": uid.add_core_prefix(plain),
                        "dismissal": _occurrence_view(verdict),
                    }), 200
                raise
        # Soil commits on context exit — both facts land atomically.

        logger.info(
            "Event dismissed: %s by %s (%s)",
            plain, g.actor, g.actor_type,
        )
        return jsonify({
            "idempotent": False,
            "event_uuid": uid.add_core_prefix(plain),
            "dismissed_at": dismissed_at,
            "verdict_uuid": verdict_uuid,
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
    Event fact yet. Date-only events (all-day) compare
    lexicographically against the ISO cutoff — a past `YYYY-MM-DD` sorts
    before any full ISO timestamp of today, a future one sorts after it.

    Events that did not happen can be dismissed (POST /<uuid>/dismiss,
    RFC-014 amendment 2026-11-26 — the did_not_occur verdict); until a
    verdict lands they age out of the window.

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

    # isodatetime everywhere: mixed +00:00/Z suffixes compare fine but
    # one format keeps the lexicographic window comparisons legible.
    cutoff = isodatetime.to_timestamp(
        datetime.now(UTC) - timedelta(days=window_days)
    )
    now = isodatetime.now()

    core = get_core()
    where = """superseded_by IS NULL AND type = 'Event'
        AND json_extract(data, '$.occurred_at') IS NULL
        AND COALESCE(json_extract(data, '$.ends_at'),
                     json_extract(data, '$.starts_at')) <= ?
        AND COALESCE(json_extract(data, '$.ends_at'),
                     json_extract(data, '$.starts_at')) >= ?"""
    params: list = [now, cutoff]

    total_unmarked = core._conn.execute(
        f"SELECT COUNT(*) AS n FROM entity WHERE {where}", params
    ).fetchone()["n"]
    rows = core._conn.execute(
        f"""SELECT uuid, data, updated_at FROM entity
            WHERE {where}
            ORDER BY COALESCE(json_extract(data, '$.ends_at'),
                              json_extract(data, '$.starts_at')) DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()

    # Exclude events that already have an Event fact (the idempotency
    # probe pattern from mark_occurred — the fact's data carries the
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
                        WHERE _type = 'Event' AND superseded_by IS NULL
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
