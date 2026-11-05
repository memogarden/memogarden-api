"""Provider registry endpoints (RFC-004 §12.2).

All routes hang off the ApiV1 blueprint (authentication enforced by its
before_request handler). The `run` provider name is reserved at registration
so POST /providers/run (the heartbeat entry point) stays unambiguous.
"""

import logging

from flask import Blueprint, jsonify

from .. import provider_registry, provider_runtime
from ..schemas.provider import RegisterProviderRequest, UpdateProviderRequest
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


@providers_bp.post("/run")
def run_due_providers():
    """Run all due providers (heartbeat entry point).

    The systemd ingest timer pokes this endpoint; the server decides
    due-ness from each provider's interval against last_run_at. Runs execute
    in background threads; this returns immediately.
    """
    return jsonify(provider_runtime.run_all_due(triggered_by="heartbeat")), 202


@providers_bp.post("/<name>/run")
def run_provider(name: str):
    """Trigger a run for a specific provider (manual, even if not yet due).

    Returns:
        202: {"name", "run_id", "status": "running"}
        404: Unknown provider
        409: A run is already in flight (single-flight lock)
    """
    return jsonify(provider_runtime.start_run(name, triggered_by="manual")), 202
