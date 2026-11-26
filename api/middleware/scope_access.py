"""Scope access helpers (RFC-011).

The API layer is THE enforcement point (RFC-011 §4.9): every principal
request flows through here, while providers write via the system layer
directly — which is exactly the SG-7 exemption (providers are not
principals, they label at creation per operator policy).

Model (RFC-011 §4.5):
- Unscoped items are the global space — visible to every authenticated
  principal (SG-1).
- Scoped items require the requester's ACTIVE scope to be one of the
  item's metadata.scopes labels (SG-5: attention-gated; a grant held
  without entering authorizes nothing).
- Grant checks happen at `enter` (SG-4); JWT operator sessions bypass
  all gates (SG-11 sovereignty).

Enforcement activates via MEMOGARDEN_SCOPE_ENFORCEMENT (default off,
RFC-011 §8 rollout: provision grants first, then flip).

Labeling at creation (RFC-011 §4.6) lives here too, and is deliberately
NOT gated on that flag — labels have to be right before enforcement turns
on. SG-9: a fact derived from a source item (metadata.derived_from)
inherits that source's current scope labels, so text extracted from a
scoped document never becomes a plaintext side-channel around the model.
MEMOGARDEN_DERIVE_INHERIT_SCOPE=off is the operator's escape hatch.
"""

import json
import os

from flask import g

from system.exceptions import ConsistencyError, PermissionDenied, ResourceNotFound
from system.soil import resolve_live
from utils import isodatetime, uid

_TRUTHY = {"1", "true", "yes", "on"}
_DERIVE_INHERIT_OFF = {"0", "false", "no", "off"}


def enforcement_enabled() -> bool:
    """Whether scope filtering is active for this deployment (RFC-011 §8)."""
    return os.environ.get("MEMOGARDEN_SCOPE_ENFORCEMENT", "").strip().lower() in _TRUTHY


def normalize_scopes(values: list) -> list:
    """Scope labels are stored as plain uuids (DB convention, matching the
    active scope on ContextFrames) — callers may hand them over prefixed.
    Normalize at the API boundary so the predicate never compares a
    prefixed label against a plain one."""
    return [uid.strip_prefix(v) if isinstance(v, str) else v for v in values]


def requester_has_all_scopes() -> bool:
    """SG-11: JWT operator sessions carry implicit all-grants.

    flask.g.grants is None for JWT sessions and a list for API keys
    (set by the auth middleware during verify).
    """
    return getattr(g, "grants", None) is None


def _grant_active(entry: dict) -> bool:
    """Expiry check; ISO 8601 UTC strings compare lexicographically."""
    expires_at = entry.get("expires_at")
    return bool(expires_at) is False or expires_at > isodatetime.now()


def require_scope_grant(scope_uuid: str) -> None:
    """SG-4: the presenting principal must hold a grant on the scope.

    Called by the enter verb handler at the API layer. Denials raise
    PermissionDenied (403) and are audited by the @with_audit wrapper.

    Args:
        scope_uuid: Scope uuid (plain, unprefixed)
    """
    if requester_has_all_scopes():
        return
    for entry in getattr(g, "grants", None) or []:
        if entry.get("scope_uuid") == scope_uuid and _grant_active(entry):
            return
    raise PermissionDenied(
        "No grant for this scope — ask the operator for a grant "
        "(RFC-011 §4.4: entering requires a grant)",
        {"scope_uuid": scope_uuid, "code": "scope_grant_required"},
    )


def public_scope_uuids() -> frozenset:
    """Plain uuids of live scopes carrying visibility: "public" (visibility
    addendum). READ paths only — public visibility relaxes reads from any
    frame, never writes (grants govern entry/writes; ratified 2026-11-19).
    Empty when enforcement is off (callers are flag-gated anyway) — the
    predicate keeps today's behavior.

    Read paths resolve this ONCE per request and hand it to the predicate;
    it short-circuits per label: any public label makes the item readable
    from any frame (most permissive wins on mixed labels).
    """
    if not enforcement_enabled():
        return frozenset()
    from system.core import get_core

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT uuid FROM entity WHERE type = 'Scope' "
            "AND superseded_by IS NULL "
            "AND json_extract(data, '$.visibility') = 'public'"
        ).fetchall()
    return frozenset(row["uuid"] for row in rows)


def item_visible(
    item_scopes: list | None,
    active_scope: str | None,
    public_scopes: frozenset = frozenset(),
) -> bool:
    """The §4.5 access predicate for one item.

    Args:
        item_scopes: The item's metadata.scopes labels (empty/None = unscoped)
        active_scope: The requester's active scope (None = unframed)
        public_scopes: Plain uuids of public-visibility scopes (read paths
            only). Any public label short-circuits visible — most permissive
            wins on mixed public/private labels (visibility addendum).
            WRITE gates deliberately omit this argument: visibility relaxes
            reads only, writes still require being inside the frame.
    """
    if not item_scopes:
        return True
    if public_scopes and any(label in public_scopes for label in item_scopes):
        return True
    return active_scope is not None and active_scope in item_scopes


def gate_labels_for_item(soil, item) -> list | None:
    """The labels the §4.5 read gate applies to one item: its own
    metadata.scopes — or, when the item is superseded, its LIVE
    successor's (visibility addendum §6 item 1, option (a) "inherit",
    api#97). A relabel's decision lives on the live version; history
    stays readable, gated by the successor — an unlabelled predecessor
    of a privately-relabeled item is NOT the global space.

    Returns:
        The effective labels (empty = unscoped), or None when no live
        version exists (cyclic or dangling superseded_by) — callers
        treat None as invisible.
    """
    if not getattr(item, "superseded_by", None):
        return (item.metadata or {}).get("scopes") or []
    try:
        live, _chain = resolve_live(soil, item.uuid)
    except ConsistencyError:
        return None
    fact = soil.get_fact(live)
    if fact is None:
        return None
    return (fact.metadata or {}).get("scopes") or []


def active_scope_for(actor: str) -> str | None:
    """The actor's active scope from their ContextFrame (None if unframed).

    v1: activation lives on the operator frame (matches the enter verb).
    """
    from system.core import get_core
    from system.exceptions import ResourceNotFound

    try:
        with get_core() as core:
            frame = core.context.get_context_frame(
                owner=actor, owner_type="operator", create_if_missing=False
            )
            return frame.active_scope
    except ResourceNotFound:
        return None


def filter_visible(items: list, actor: str, get_scopes) -> list:
    """Filter a fetched result list down to what the requester may see (SG-10).

    Applied AFTER fetch and BEFORE building the response: scoped items
    outside the requester's active scope never surface. (The strict
    pre-match SQL filter is the optimization path once labels are
    indexed; exclusion before response satisfies SG-10's truthful-
    presentation requirement for v1.)

    Args:
        items: Fetched items/rows
        actor: Authenticated actor uuid
        get_scopes: Callable extracting the item's scope labels (its
            metadata.scopes); may return None
    """
    if not enforcement_enabled() or requester_has_all_scopes():
        return items
    active_scope = active_scope_for(actor)
    public_scopes = public_scope_uuids()
    return [
        item for item in items
        if item_visible(get_scopes(item), active_scope, public_scopes)
    ]


# ============================================================================
# Labeling at creation — RFC-011 §4.6 / SG-9 "derived inherits"
# ============================================================================

def derive_inherit_enabled() -> bool:
    """SG-9 switch: a fact derived from a source item inherits that source's
    scope labels.

    Default ON — without it, text extracted from a scoped document is a
    plaintext side-channel around the whole model, so
    MEMOGARDEN_DERIVE_INHERIT_SCOPE=off is the operator's escape hatch, not
    the default. Anything but an explicit falsy value means on.
    """
    value = os.environ.get("MEMOGARDEN_DERIVE_INHERIT_SCOPE", "").strip().lower()
    return value not in _DERIVE_INHERIT_OFF


def row_labels(raw) -> list:
    """Scope labels out of a row's JSON `data` / `metadata` value (dict,
    JSON text or None), as plain uuids."""
    if isinstance(raw, str):
        raw = json.loads(raw) if raw else {}
    return [
        uid.strip_prefix(s) for s in ((raw or {}).get("scopes") or [])
        if isinstance(s, str)
    ]


def current_labels(soil, core, ref) -> list | None:
    """One referenced item's CURRENT scope labels, read through the stores'
    public APIs: `resolve_live` + `Soil.get_fact` for a fact (bare or
    soil_-prefixed uuid), `core.entity.get_by_id` for an entity.

    A superseded reference resolves to its live version: a relabel is a new
    version, so the operator's decision lives there, not on the row a
    caller happened to hold. Labels come back as plain uuids.

    Args:
        soil: open Soil handle.
        core: open Core handle, or None to resolve facts only.
        ref: the item's uuid.

    Returns:
        [] for an item with no labels; None when nothing matches, or an
        entity chain is cyclic. A cyclic FACT chain raises
        ConsistencyError — the system layer's policy for single lookups.
    """
    if not isinstance(ref, str) or not ref:
        return None
    if core is not None and not ref.startswith("soil_"):
        try:
            entity = core.entity.get_by_id(ref)
        except ResourceNotFound:
            entity = None
        if entity is not None:
            seen = set()
            while entity.get("superseded_by") and entity["superseded_by"] not in seen:
                seen.add(entity["superseded_by"])
                entity = core.entity.get_by_id(entity["superseded_by"])
            if entity.get("superseded_by") in seen:
                return None  # cyclic chain — no live version to name
            return row_labels(entity.get("data"))
    live, _chain = resolve_live(soil, ref)
    fact = soil.get_fact(live)
    return row_labels(fact.metadata) if fact is not None else None


def source_item_scopes(soil, ref) -> list | None:
    """A source FACT's current scope labels (SG-9). Facts only: a Core
    entity as a derivation source is not covered — entity lineage is a
    separate mechanism.

    Args:
        soil: open Soil handle.
        ref: the source fact's uuid (metadata.derived_from).

    Returns:
        [] for a fact with no labels; None when no such fact exists.
    """
    return current_labels(soil, None, ref)


def live_labels(refs) -> dict:
    """Current scope labels for each referenced item — a fact or a Core
    entity — read from committed state.

    Feeds the RFC-011 §4.9(5) SSE suppress for items a publisher does not
    hold in hand (relation endpoints, frame containers). Empty when
    enforcement is off: the suppress is a no-op then, so skip the reads.
    A reference that resolves to nothing maps to [] — the global space,
    which the predicate treats as visible.
    """
    if not refs or not enforcement_enabled():
        return {}
    from system.core import get_core
    from system.soil import get_soil

    out = {}
    with get_soil() as soil, get_core() as core:
        for ref in refs:
            if isinstance(ref, str):
                out[ref] = current_labels(soil, core, ref) or []
    return out


def inherit_derived_scopes(metadata: dict | None, soil) -> bool:
    """SG-9 for the provider lane: copy the source's current labels onto a
    fact that declares `metadata.derived_from`, verbatim.

    Providers are not principals (SG-7), so there is no grant to narrow by
    here — they label at creation per operator policy, and a labeled source
    outranks both the parent rule and that policy.

    The source's labels are inherited whatever they are, the triage scope
    included. A document still waiting in triage is restricted to triage
    members; text extracted from it must not reach anyone else — not even
    through a covering email that was already filed elsewhere. The rule has
    one job: a derived fact is never more visible than its source.

    Args:
        metadata: the fact's metadata dict, mutated in place.
        soil: open Soil handle.

    Returns:
        True when labels were applied. False when the switch is off, the
        fact is already labeled (the creator labeled it deliberately), no
        source is declared, or the source is unlabeled — those fall through
        to the other creation-time defaults.
    """
    if not derive_inherit_enabled() or not metadata or metadata.get("scopes"):
        return False
    labels = source_item_scopes(soil, metadata.get("derived_from"))
    if not labels:
        return False
    metadata["scopes"] = labels
    return True


def derived_scopes_for_requester(soil, ref) -> list:
    """SG-9 for the API lane: the labels a fact derived from `ref` may carry
    for the CURRENT requester.

    The source's current labels — narrowed, under enforcement, to the ones
    a non-sovereign requester holds a live grant for. Inheritance narrows
    visibility; it must never become a way to write into a scope the
    requester cannot enter (RFC-011 D2, symmetric grants). Advisory mode
    inherits verbatim so the corpus is labeled correctly before the flip.

    Returns:
        Labels to apply (empty = use the ordinary creation default: the
        switch is off, no source is named, or none of its labels survive).
    """
    if not derive_inherit_enabled():
        return []
    labels = source_item_scopes(soil, ref)
    if not labels:
        return []
    if enforcement_enabled() and not requester_has_all_scopes():
        granted = {
            uid.strip_prefix(entry["scope_uuid"])
            for entry in (getattr(g, "grants", None) or [])
            if entry.get("scope_uuid") and _grant_active(entry)
        }
        labels = [label for label in labels if label in granted]
    return labels
