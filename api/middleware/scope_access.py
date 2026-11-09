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
"""

import os

from flask import g

from system.exceptions import PermissionDenied
from utils import isodatetime

_TRUTHY = {"1", "true", "yes", "on"}


def enforcement_enabled() -> bool:
    """Whether scope filtering is active for this deployment (RFC-011 §8)."""
    return os.environ.get("MEMOGARDEN_SCOPE_ENFORCEMENT", "").strip().lower() in _TRUTHY


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


def item_visible(item_scopes: list | None, active_scope: str | None) -> bool:
    """The §4.5 access predicate for one item.

    Args:
        item_scopes: The item's metadata.scopes labels (empty/None = unscoped)
        active_scope: The requester's active scope (None = unscoped operation)
    """
    if not item_scopes:
        return True
    return active_scope is not None and active_scope in item_scopes


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
    return [
        item for item in items
        if item_visible(get_scopes(item), active_scope)
    ]
