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
from utils import isodatetime, uid

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
        scope_policy = manifest.get("scope_policy")
        resolve_scope = _make_scope_resolver()
        with get_soil() as soil:
            for yielded in provider.sync(since=since):
                fact = _to_fact(yielded)
                # RFC-011 §4.8 (item 4): providers label at creation per the
                # operator's policy — the framework applies it, providers
                # cannot forget (SG-7: providers are never scope-gated).
                _apply_scope_policy(fact, scope_policy, resolve_scope)
                fact_uuid = soil.create_fact(fact)
                # Commit before create_relations: providers hold their own
                # Soil instance (get_soil() is not cached), and a separate
                # connection cannot see this transaction's uncommitted rows —
                # same-run relation targets (a note's task, a reply's parent)
                # would resolve to nothing.
                soil.commit()
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


def _make_scope_resolver():
    """Resolve a policy scope reference to a plain scope uuid, cached per run.

    A reference is a core_-prefixed uuid, a bare uuid, a builtin alias
    ("triage"/"private" — mapped to the builtin labels), or a Scope label.
    Unknown references resolve to None — the item is left unscoped and the
    miss is logged (visible failure beats silent mislabeling).
    """
    cache: dict = {}
    # Builtin aliases (§4.1): the runtime's policies address the builtin
    # scopes by short name; ensure_builtin_scopes seeds them under labels.
    BUILTIN_ALIASES = {"triage": "Triage / Untrusted", "private": "Private"}

    def resolve(ref):
        if ref in cache:
            return cache[ref]
        from system.core import get_core

        plain = uid.strip_prefix(ref)
        label = BUILTIN_ALIASES.get(ref, ref)
        with get_core() as core:
            row = core._conn.execute(
                "SELECT uuid FROM entity WHERE uuid IN (?, ?) AND type = 'Scope'",
                (plain, uid.add_core_prefix(plain)),
            ).fetchone()
            if row is None:
                row = core._conn.execute(
                    "SELECT uuid FROM entity WHERE type = 'Scope' "
                    "AND json_extract(data, '$.label') = ?",
                    (label,),
                ).fetchone()
        uuid_val = row["uuid"] if row else None
        if uuid_val is None:
            logger.warning(
                "scope policy references unknown scope %r — item left unscoped", ref
            )
        cache[ref] = uuid_val
        return uuid_val

    return resolve


def _apply_scope_policy(fact: Fact, policy: dict | None, resolve_scope) -> None:
    """RFC-011 §4.8 (item 4): apply the operator's scope policy to a fact.

    Rules-based: ordered {match, scope} rules evaluated first-match-wins
    against a merged view of the fact's data and metadata; the default
    catch-all routes unmatched items to triage (ratified 2026-11-12 —
    fail-closed). A scope of null on a matching rule (or a null default)
    delivers the item unscoped. A provider-set metadata.scopes wins over
    the policy.
    """
    metadata = dict(fact.metadata or {})
    if metadata.get("scopes"):
        return  # provider labeled it — provider wins

    merged = {**(fact.data or {}), **metadata}
    scope_ref = None
    for rule in (policy or {}).get("rules", []):
        match = rule.get("match", {})
        if all(merged.get(field) == value for field, value in match.items()):
            scope_ref = rule.get("scope")
            break
    else:
        scope_ref = (policy or {}).get("default", "triage")

    if scope_ref is None:
        return  # explicitly unscoped
    scope_uuid = resolve_scope(scope_ref)
    if scope_uuid:
        metadata["scopes"] = [scope_uuid]
        fact.metadata = metadata
