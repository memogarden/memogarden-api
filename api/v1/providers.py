"""Provider registry endpoints (RFC-004 §12.2).

All routes hang off the ApiV1 blueprint (authentication enforced by its
before_request handler). The `run` provider name is reserved at registration
so POST /providers/run (the heartbeat entry point) stays unambiguous.
"""

import logging

from flask import Blueprint, jsonify, request

from .. import provider_registry, provider_runtime
from ..middleware.decorators import _authenticate_jwt
from ..schemas.provider import (
    RegisterAccountRequest,
    RegisterProviderRequest,
    ScopePolicyRequest,
    UpdateAccountRequest,
    UpdateProviderRequest,
)
from ..validation import validate_request

logger = logging.getLogger(__name__)

providers_bp = Blueprint("providers", __name__, url_prefix="/providers")


@providers_bp.post("")
@validate_request
def register_provider(data: RegisterProviderRequest):
    """Register a data provider.

    The server validates the registration at the door: module must be
    importable in the server environment, class must conform to the Provider
    Protocol; derived capabilities are stored in the manifest.

    Returns:
        201: Stored manifest (echo of payload + server-assigned fields:
             capabilities, registered_at, last_run_id, last_run_at,
             last_status, next_due_at)
        400: Validation error (bad schema, unimportable module, Protocol
             mismatch, reserved name)
        409: Name already registered
    """
    return jsonify(provider_registry.register_provider(data)), 201


@providers_bp.get("")
def list_providers():
    """List registered providers with status."""
    providers = provider_registry.list_providers()
    return jsonify({"providers": providers, "count": len(providers)})


@providers_bp.get("/<name>")
def get_provider(name: str):
    """Get a single provider manifest by name."""
    return jsonify(provider_registry.get_provider(name))


@providers_bp.patch("/<name>")
@validate_request
def update_provider(name: str, data: UpdateProviderRequest):
    """Update mutable provider fields (enabled, interval, description, config).

    module and class are immutable — re-register instead (once deletion
    exists; today a duplicate name 409s, so treat rename as future work).
    """
    return jsonify(provider_registry.update_provider(name, data))


@providers_bp.get("/<name>/runs")
def list_provider_runs(name: str):
    """List recent runs for a provider, newest first (RFC-015 §3.2).

    Read-only operational visibility (RFC-015 §2.2 — the operator's
    verification instrument for delegated run triggers). Matches provider
    GET's principal class: any authenticated principal may read.

    Query params:
        account_id: filter to one account ('' selects provider-level runs)
        status: 'running' | 'success' | 'failed'
        limit: row cap (default 50, hard cap 200)

    Returns:
        200: {"runs": [...], "count": n}
        404: Unknown provider
        400: Bad status filter
    """
    account_id = request.args.get("account_id")
    status = request.args.get("status")
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        from system.exceptions import ValidationError

        raise ValidationError(
            f"limit must be an integer, got {raw_limit!r}",
            {"limit": raw_limit},
        ) from None
    runs = provider_registry.list_runs(
        name, account_id=account_id, status=status, limit=limit
    )
    return jsonify({"runs": runs, "count": len(runs)})


@providers_bp.put("/<name>/scope-policy")
@validate_request
def set_scope_policy(name: str, data: ScopePolicyRequest):
    """Set a provider's scope policy (RFC-011 §4.8, item 4). Operator-gated:
    JWT only — a provider's labeling policy is boundary configuration, and
    boundary configuration is operator-governed (D1's invariant applied to
    those who shape it).

    The policy applies at fact creation in the provider runtime: ordered
    first-match-wins rules over the created fact's data+metadata fields,
    default catch-all → triage when omitted (ratified default — fail-closed).

    Returns:
        Updated manifest including the stored scope_policy
    """
    _authenticate_jwt()
    policy = {"rules": [rule.model_dump() for rule in data.rules]}
    if data.default is not None or "default" in data.model_fields_set:
        policy["default"] = data.default
    return jsonify(provider_registry.set_scope_policy(name, policy))


@providers_bp.post("/run")
def run_due_providers():
    """Run all due work (heartbeat entry point).

    The systemd ingest timer pokes this endpoint; the server decides
    due-ness from intervals against last_run_at — provider-level for
    account-less providers, per-account for account-backed ones (RFC-012
    §4.5). Runs execute in background threads; this returns immediately.
    """
    return jsonify(provider_runtime.run_all_due(triggered_by="heartbeat")), 202


@providers_bp.post("/<name>/run")
def run_provider(name: str):
    """Trigger runs for a specific provider (manual, even if not yet due).

    Account-less providers start one provider-level run. Account-backed
    providers start every active account in parallel (RFC-012 §4.5).

    Returns:
        202: Provider-level {"name", "run_id", "status": "running"} or
             account-backed {"name", "triggered": [...], "skipped": [...]}
        404: Unknown provider
        409: Single-flight lock held (provider-level only; account runs
             report collisions per account inside "skipped")
    """
    return jsonify(provider_runtime.start_provider_run(name, triggered_by="manual")), 202


# ============================================================================
# Provider accounts (RFC-012 §6.1)
# ============================================================================


@providers_bp.post("/<name>/accounts")
@validate_request
def register_account(name: str, data: RegisterAccountRequest):
    """Bind an upstream account to a provider (RFC-012).

    Tokens never transit the API: consent happens CLI-local and the secret
    lives in HACM under `credential_ref` (default {platform}:{subject}).
    The subject must already be verified against that credential by the
    linking flow (INV-3) — the server records the binding, it does not
    attest it.

    Returns:
        201: Stored account manifest
        400: Bad schema, or platform rejected by the provider class
        404: Unknown provider
        409: (provider, subject) already registered
    """
    return jsonify(provider_registry.register_account(name, data)), 201


@providers_bp.get("/<name>/accounts")
def list_accounts(name: str):
    """List a provider's account bindings with status (RFC-012)."""
    accounts = provider_registry.list_accounts(name)
    return jsonify({"accounts": accounts, "count": len(accounts)})


@providers_bp.get("/<name>/accounts/<account_id>")
def get_account(name: str, account_id: str):
    """Get one account binding (RFC-012)."""
    return jsonify(provider_registry.get_account(name, account_id))


@providers_bp.patch("/<name>/accounts/<account_id>")
@validate_request
def update_account(name: str, account_id: str, data: UpdateAccountRequest):
    """Update mutable account fields (label, status, config, interval).

    platform/subject/credential_ref are immutable — remove and re-register
    instead (the credential they reference is operator-managed in HACM).
    """
    return jsonify(provider_registry.update_account(name, account_id, data))


@providers_bp.delete("/<name>/accounts/<account_id>")
def remove_account(name: str, account_id: str):
    """Delete an account binding (RFC-012).

    Removes the registry row only — the HACM credential it references is
    operator-managed (custody operations stay on the host side, INV-2).
    """
    provider_registry.remove_account(name, account_id)
    return "", 204


@providers_bp.post("/<name>/accounts/<account_id>/verify")
def verify_account(name: str, account_id: str):
    """Probe an account's credential health server-side (RFC-012 §6.1).

    Delegates to the provider's verify_account hook. A passing probe
    re-activates a needs_reauth account; a failing probe parks it there.

    Returns:
        200: Updated account manifest (status/error/last_verified_at)
        400: Provider has no verify_account hook
        404: Unknown provider or account
    """
    return jsonify(provider_runtime.verify_account(name, account_id))


@providers_bp.post("/<name>/accounts/<account_id>/run")
def run_account(name: str, account_id: str):
    """Trigger a run for one specific account (manual, RFC-012 §6.1).

    Returns:
        202: {"name", "account_id", "subject", "run_id", "status": "running"}
        404: Unknown provider or account
        409: A run is already in flight for this (provider, account)
    """
    return jsonify(
        provider_runtime.start_run(name, triggered_by="manual", account_id=account_id)
    ), 202
