"""Provider runtime: executes registered providers inside the server process.

This is the piece that makes providers first-class server residents (PRD
client-server process model): providers run in-process, using in-process
Core/Soil operations, instead of writing to the databases from a sidecar.

Execution model:
- A run is claimed by INSERTing a 'running' row into provider_runs; the
  partial unique index makes this a single-flight lock that works across
  gunicorn workers (second claim fails with IntegrityError → ConflictError).
- The run executes in a daemon thread; the HTTP handler returns 202
  immediately with the run id.
- Provider contract: cls(config: dict) constructs the provider from its
  registry config; sync(since) yields Facts — either system.soil.Fact
  instances or dicts with {type, data, metadata[, canonical_at]} keys.
  Optional create_relations(fact) returns SystemRelation objects to store.
"""

import json
import logging
import threading

from system.exceptions import LockConflictError
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime

from . import provider_registry
from .provider_registry import (
    claim_run,
    finish_run,
    get_due_providers,
    get_last_success_at,
    get_provider,
    import_provider_module,
    validate_provider_origin,
)

logger = logging.getLogger(__name__)


def start_run(name: str, triggered_by: str = "manual") -> dict:
    """Start a provider run in a background thread.

    Args:
        name: Registered provider name
        triggered_by: 'heartbeat' or 'manual'

    Returns:
        {"name", "run_id", "status": "running"} — poll GET /providers/{name}
        for last_status / next_due_at.

    Raises:
        ResourceNotFound: Unknown provider
        LockConflictError: A run is already in flight for this provider
    """
    manifest = get_provider(name)  # 404 if unknown
    run_id = claim_run(name, triggered_by)
    since = get_last_success_at(name)

    thread = threading.Thread(
        target=_execute_run,
        args=(
            run_id,
            name,
            manifest["module"],
            manifest["class"],
            manifest["config"],
            since,
        ),
        daemon=True,
        name=f"provider-run-{name}",
    )
    thread.start()
    logger.info("Provider run started: %s run_id=%s triggered_by=%s", name, run_id, triggered_by)
    return {"name": name, "run_id": run_id, "status": "running"}


def run_all_due(triggered_by: str = "heartbeat") -> dict:
    """Run every enabled provider whose next_due_at has passed.

    The systemd heartbeat timer pokes POST /api/v1/providers/run; the server
    decides due-ness from each provider's interval against last_run_at.

    Returns:
        {"triggered": [{name, run_id, status}...], "skipped": [{name, reason}...]}
    """
    triggered, skipped = [], []
    for manifest in get_due_providers():
        try:
            triggered.append(start_run(manifest["name"], triggered_by))
        except LockConflictError:
            skipped.append({"name": manifest["name"], "reason": "already running"})
    return {"triggered": triggered, "skipped": skipped}


def _execute_run(
    run_id: str,
    name: str,
    module: str,
    class_name: str,
    config: dict,
    since: str | None,
) -> None:
    """Thread body: instantiate the provider, sync, store facts, record outcome."""
    manifest = get_provider(name)
    started_at = isodatetime.now()

    try:
        mod = import_provider_module(module)
        # Defense in depth: roots may have tightened since registration —
        # a run must not execute code that is no longer origin-valid.
        validate_provider_origin(module, mod)
        cls = getattr(mod, class_name)
        provider = cls(config=config)

        facts_inserted = 0
        relations_created = 0
        with get_soil() as soil:
            for yielded in provider.sync(since=since):
                fact = _to_fact(yielded)
                fact_uuid = soil.create_fact(fact)
                facts_inserted += 1
                if callable(getattr(provider, "create_relations", None)):
                    for relation in (provider.create_relations(fact) or []):
                        soil.create_relation(relation)
                        relations_created += 1

        detail = {"facts_inserted": facts_inserted, "relations_created": relations_created}
        finish_run(run_id, name, started_at, "success", detail, manifest["interval"])
        logger.info("Provider run succeeded: %s run_id=%s %s", name, run_id, detail)

    except Exception as e:  # noqa: BLE001 — a failing provider must not crash the server
        logger.exception("Provider run failed: %s run_id=%s", name, run_id)
        detail = {"error": f"{type(e).__name__}: {e}"}
        try:
            finish_run(run_id, name, started_at, "failed", detail, manifest["interval"])
        except Exception:  # noqa: BLE001 — bookkeeping must never mask the original error
            logger.exception("Failed to record run outcome: %s run_id=%s", name, run_id)


def _to_fact(yielded) -> Fact:
    """Normalize a provider-yielded item into a Soil Fact."""
    if isinstance(yielded, Fact):
        return yielded
    if isinstance(yielded, dict):
        now = isodatetime.now()
        return Fact(
            uuid=generate_soil_uuid(),
            _type=yielded["type"],
            realized_at=yielded.get("realized_at", now),
            canonical_at=yielded.get("canonical_at", now),
            data=yielded.get("data", {}),
            metadata=yielded.get("metadata", {}),
            integrity_hash=None,  # computed by create_fact
            fidelity="full",
        )
    raise TypeError(
        f"provider yielded {type(yielded).__name__}; expected system.soil.Fact "
        "or dict with {type, data, metadata}"
    )
