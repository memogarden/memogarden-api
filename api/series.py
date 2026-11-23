"""Recurring-series identity and membership (api#94).

gcal ingests one Event entity per occurrence (singleEvents=true, RFC-014),
and Google expands contact birthdays to 2099 — one birthday = 70–90
identical entities that each need their own scope decision. Series identity
is DERIVED (no new entity type, api#94 operator decision): every instance
carries a series key in its data (recurring_event_id, falling back to
ical_uid), and "the series' scope" is simply the scopes of its live
instances — re-derived at apply time (cascade) and at ingestion
(inheritance).
"""

from __future__ import annotations

from utils import uid

# Fields that identify a recurring series on an entity payload, in priority
# order. gcal Events always carry recurring_event_id on instances (the
# master's own id); ical_uid is the fallback for sources that only have the
# RFC-5545 UID.
SERIES_KEY_FIELDS = ("recurring_event_id", "ical_uid")



def series_key(data: dict) -> str | None:
    """Series key of an entity payload, or None when not a series member.

    Args:
        data: entity data dict (the merged data+metadata view for provider
            yields — same convention as _apply_entity_scopes).

    Returns:
        The series key value (str) or None. Empty/None fields never match —
        a blank recurring_event_id is not a series identity.
    """
    for field in SERIES_KEY_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def series_members(core, entity_type: str, key: str,
                   exclude_uuid: str | None = None) -> list:
    """Live entities of one series (raw rows: uuid, data, created_at).

    Args:
        core: open Core context (caller manages `with get_core()`).
        entity_type: entity type filter — series identity is only meaningful
            within one type (gcal Events today).
        key: series key value (see series_key).
        exclude_uuid: optional uuid to leave out (the edit target).

    Returns:
        List of sqlite3.Row (uuid, data, created_at), live only
        (superseded_by IS NULL), most recent first.
    """
    placeholders = ", ".join(
        f"json_extract(data, '$.{field}')" for field in SERIES_KEY_FIELDS
    )
    sql = f"""
        SELECT uuid, data, created_at FROM entity
        WHERE type = ? AND superseded_by IS NULL
          AND (? IN ({placeholders}))
    """
    # one probe ? before the IN-list (the key expressions are column
    # extracts, not parameters)
    params: list = [entity_type, key]
    if exclude_uuid:
        sql += " AND uuid NOT IN (?, ?)"
        params.extend([exclude_uuid, uid.add_core_prefix(exclude_uuid)])
    sql += " ORDER BY created_at DESC"
    return core._conn.execute(sql, params).fetchall()


def series_stats(core, entity_type: str, keys: list[str]) -> dict:
    """Blast-radius stats for a batch of series keys: {key: {count, first, last}}.

    One grouped query (api#94 console surface): count + occurrence-date
    range per key, live rows only. `first`/`last` ride original_start_at
    (occurrence time), falling back to created_at when absent.

    Args:
        core: open Core context (caller manages `with get_core()`).
        entity_type: entity type filter.
        keys: series key values to report (deduped internally).

    Returns:
        {key: {"count": int, "first": str|None, "last": str|None}}
    """
    unique = sorted({k for k in keys if k})
    if not unique:
        return {}
    stats: dict = {}
    # IN-groups keep the query plan sane when one request names a handful
    # of series; the console window names at most a few dozen.
    for field in SERIES_KEY_FIELDS:
        col = f"json_extract(data, '$.{field}')"
        batch = [k for k in unique if k not in stats]
        if not batch:
            break
        placeholders = ",".join("?" * len(batch))
        rows = core._conn.execute(
            f"""SELECT {col} AS key_value,
                       COUNT(*) AS n,
                       MIN(COALESCE(json_extract(data, '$.original_start_at'),
                                    created_at)) AS first_at,
                       MAX(COALESCE(json_extract(data, '$.original_start_at'),
                                    created_at)) AS last_at
                FROM entity
                WHERE type = ? AND superseded_by IS NULL
                  AND {col} IN ({placeholders})
                GROUP BY {col}""",
            [entity_type, *batch],
        ).fetchall()
        for r in rows:
            stats[r["key_value"]] = {
                "count": r["n"],
                "first": r["first_at"],
                "last": r["last_at"],
            }
    return stats
