"""Provider runtime: executes registered providers inside the server process.

This is the piece that makes providers first-class server residents (PRD
client-server process model): providers run in-process, using in-process
Core/Soil operations, instead of writing to the databases from a sidecar.

Execution model:
- A run is claimed by INSERTing a 'running' row into provider_runs; the
  partial unique index makes this a single-flight lock per (provider,
  account) that works across gunicorn workers (second claim fails with
  IntegrityError → ConflictError).
- The run executes in a daemon thread; the HTTP handler returns 202
  immediately with the run id.
- Provider contract: cls(config: dict[, account: dict]) constructs the
  provider from its registry config (account-backed providers additionally
  receive the account context — detected via the __init__ signature);
  sync(since) yields Facts — either system.soil.Fact instances or dicts
  with {type, data, metadata[, canonical_at]} keys. Optional
  create_relations(fact) returns SystemRelation objects to store. Optional
  PLATFORMS class attribute restricts accepted account platforms. Optional
  verify_account(account, config) probes credential health server-side.
- Account context (RFC-012): account-backed providers run once per
  (provider, account); a raised exception with a truthy `auth_failure`
  attribute parks the account in needs_reauth instead of hammering it on
  every heartbeat.
"""

import json
import logging
import threading
from contextlib import ExitStack

from system.core import get_core
from system.exceptions import LockConflictError, ValidationError
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime, uid

from . import provider_registry
from .provider_registry import (
    accepts_account_cls,
    claim_run,
    finish_run,
    get_due_accounts,
    get_due_providers,
    get_last_success_at,
    get_provider,
    import_provider_module,
    validate_provider_origin,
)

logger = logging.getLogger(__name__)


def _account_context(account: dict) -> dict:
    """The context dict handed to account-backed providers (RFC-012 §4.5).

    Registry fields only — there are no secrets in the account row, but
    keep this an explicit projection so the provider contract stays stable.
    """
    return {
        "id": account["id"],
        "platform": account["platform"],
        "subject": account["subject"],
        "label": account["label"],
        "credential_ref": account["credential_ref"],
        "config": account.get("config") or {},
    }


def start_run(
    name: str,
    triggered_by: str = "manual",
    account_id: str = "",
) -> dict:
    """Start a provider run in a background thread.

    Args:
        name: Registered provider name
        triggered_by: 'heartbeat' or 'manual'
        account_id: '' for a provider-level run; else run this specific
            account binding (RFC-012 §4.5)

    Returns:
        {"name", "run_id", "status": "running"} (plus "account_id"/"subject"
        for account runs) — poll GET /providers/{name} or the accounts
        endpoints for last_status / next_due_at.

    Raises:
        ResourceNotFound: Unknown provider or account
        LockConflictError: A run is already in flight for this provider
            (provider-level) or (provider, account)
    """
    manifest = get_provider(name)  # 404 if unknown
    account = None
    config = manifest["config"]
    interval = manifest["interval"]
    if account_id:
        account = provider_registry.get_account(name, account_id)
        config = {**config, **(account.get("config") or {})}
        interval = account.get("interval") or manifest["interval"]

    run_id = claim_run(name, triggered_by, account_id=account_id)
    since = get_last_success_at(name, account_id=account_id)

    thread = threading.Thread(
        target=_execute_run,
        args=(
            run_id,
            name,
            manifest["module"],
            manifest["class"],
            config,
            since,
            interval,
            _account_context(account) if account else None,
        ),
        daemon=True,
        name=f"provider-run-{name}" + (f"-{account['subject']}" if account else ""),
    )
    thread.start()
    logger.info(
        "Provider run started: %s run_id=%s triggered_by=%s%s",
        name, run_id, triggered_by,
        f" account={account['subject']}" if account else "",
    )
    result = {"name": name, "run_id": run_id, "status": "running"}
    if account:
        result["account_id"] = account["id"]
        result["subject"] = account["subject"]
    return result


def start_provider_run(name: str, triggered_by: str = "manual") -> dict:
    """Trigger runs for a provider now (manual, even if not yet due).

    Account-less providers start one provider-level run (RFC-004 behavior).
    Account-backed providers start every active account in parallel —
    single-flight is per (provider, account), so each 202s or 409s
    independently.
    """
    get_provider(name)  # 404 if unknown
    accounts = provider_registry.list_accounts(name)
    if not accounts:
        return start_run(name, triggered_by)

    triggered, skipped = [], []
    for account in accounts:
        if account["status"] != "active":
            skipped.append({
                "account_id": account["id"],
                "subject": account["subject"],
                "reason": f"status is {account['status']}",
            })
            continue
        try:
            started = start_run(name, triggered_by, account_id=account["id"])
            triggered.append(started)
        except LockConflictError:
            skipped.append({
                "account_id": account["id"],
                "subject": account["subject"],
                "reason": "already running",
            })
    return {"name": name, "triggered": triggered, "skipped": skipped}


def run_all_due(triggered_by: str = "heartbeat") -> dict:
    """Run every due work item: account-less providers plus due accounts.

    The systemd heartbeat timer pokes POST /api/v1/providers/run; the server
    decides due-ness from intervals against last_run_at — provider-level for
    account-less providers, per-account for account-backed ones (RFC-012
    §4.5). Accounts in needs_reauth/disabled are skipped by the scheduler,
    not by error handling.

    Returns:
        {"triggered": [{name[, account, subject], run_id, status}...],
         "skipped": [{name[, account, subject], reason}...]}
    """
    triggered, skipped = [], []
    for manifest in get_due_providers():
        try:
            triggered.append(start_run(manifest["name"], triggered_by))
        except LockConflictError:
            skipped.append({"name": manifest["name"], "reason": "already running"})
    for account in get_due_accounts():
        label = {"name": account["provider_name"], "account": account["subject"]}
        try:
            started = start_run(
                account["provider_name"], triggered_by, account_id=account["id"]
            )
            triggered.append(started)
        except LockConflictError:
            skipped.append({**label, "reason": "already running"})
    return {"triggered": triggered, "skipped": skipped}


def verify_account(name: str, account_id: str) -> dict:
    """Probe an account's credential health server-side (RFC-012 §6.1).

    Delegates to the provider class's verify_account(account, config) hook
    — credential mechanics are platform knowledge the API layer does not
    own. A passing probe re-activates a needs_reauth account; a failing
    probe parks it there (the heartbeat skips it until re-linked).

    Returns:
        The updated account manifest
    """
    manifest = get_provider(name)
    account = provider_registry.get_account(name, account_id)

    mod = import_provider_module(manifest["module"])
    cls = getattr(mod, manifest["class"], None)
    hook = getattr(cls, "verify_account", None) if cls is not None else None
    if not callable(hook):
        raise ValidationError(
            f"provider '{name}' does not support server-side credential "
            "verification (no verify_account hook)",
            {"provider": name},
        )

    config = {**manifest["config"], **(account.get("config") or {})}
    try:
        outcome = hook(_account_context(account), config)
        if not isinstance(outcome, dict):
            outcome = {"ok": bool(outcome)}
        ok = bool(outcome.get("ok"))
        error = None if ok else str(outcome.get("error") or "verification failed")
    except Exception as e:  # noqa: BLE001 — a broken hook is a failed probe
        logger.exception("Account verify failed: %s/%s", name, account_id)
        ok, error = False, f"{type(e).__name__}: {e}"

    logger.info(
        "Account verify %s: %s/%s subject=%s",
        "ok" if ok else "failed", name, account_id, account["subject"],
    )
    return provider_registry.mark_account_verified(name, account_id, ok, error)


def _execute_run(
    run_id: str,
    name: str,
    module: str,
    class_name: str,
    config: dict,
    since: str | None,
    interval: int,
    account: dict | None = None,
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
        if account is not None and accepts_account_cls(cls):
            provider = cls(config=config, account=account)
        else:
            provider = cls(config=config)

        facts_inserted = 0
        entities_created = 0
        entities_updated = 0
        entities_noop = 0
        relations_created = 0
        scope_policy = manifest.get("scope_policy")
        resolve_scope = _make_scope_resolver()
        capabilities = manifest.get("capabilities") or []
        with get_soil() as soil, ExitStack() as stack:
            core = None

            def ensure_core():
                """Open the Core connection lazily — only when the provider
                actually yields an entity upsert (a fact-only run pays no
                Core connection)."""
                nonlocal core
                if core is None:
                    core = stack.enter_context(get_core())
                return core

            for yielded in provider.sync(since=since):
                if isinstance(yielded, dict) and yielded.get("op") == "upsert_entity":
                    # RFC-014 §3: providers yield, MemoGarden stores. A
                    # provider that yields entities without declaring
                    # ENTITY_UPSERTS is a contract violation — fail the run.
                    if "upsert_entities" not in capabilities:
                        raise ValidationError(
                            f"provider '{name}' yielded an entity upsert but does "
                            "not declare ENTITY_UPSERTS (capability "
                            "'upsert_entities' missing from registration)",
                            {"provider": name},
                        )
                    core_ctx = ensure_core()
                    outcome = _upsert_entity(
                        yielded, soil, core_ctx, name, run_id,
                        scope_policy, resolve_scope,
                    )
                    if outcome == "created":
                        entities_created += 1
                    elif outcome == "updated":
                        entities_updated += 1
                    else:
                        entities_noop += 1
                    # Core-first commit ordering (RFC-008): a crash between
                    # the two commits can lose an EntityDelta (recovered on
                    # the next upsert) but must never orphan one — an
                    # EntityDelta in Soil without its entity in Core is a
                    # consistency-check failure; the reverse is not checked.
                    core_ctx._conn.commit()
                    soil.commit()
                    continue
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

        detail = {
            "facts_inserted": facts_inserted,
            "entities_created": entities_created,
            "entities_updated": entities_updated,
            "entities_noop": entities_noop,
            "relations_created": relations_created,
        }
        finish_run(run_id, name, started_at, "success", detail, interval,
                   account_id=account["id"] if account else "")
        logger.info("Provider run succeeded: %s run_id=%s %s", name, run_id, detail)

    except Exception as e:  # noqa: BLE001 — a failing provider must not crash the server
        logger.exception("Provider run failed: %s run_id=%s", name, run_id)
        detail = {"error": f"{type(e).__name__}: {e}"}
        try:
            # RFC-012 §4.2: an auth failure on an account run parks the
            # account in needs_reauth — the heartbeat then skips it until
            # re-linked, instead of hammering a dead credential hourly.
            if account is not None and getattr(e, "auth_failure", False):
                provider_registry.set_account_status(
                    name, account["id"], "needs_reauth", error=detail["error"]
                )
            finish_run(run_id, name, started_at, "failed", detail, interval,
                       account_id=account["id"] if account else "")
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


def _match_policy_scope(merged: dict, policy: dict | None):
    """First-match-wins rule evaluation over a merged data view.

    Returns the matched scope reference, the policy default, or None for
    explicitly unscoped. Absent policy = the ratified fail-closed default
    (triage, 2026-11-12). Shared by the fact path (_apply_scope_policy)
    and the entity upsert path so both label with the exact same rule
    semantics (RFC-011 §4.8 item 4 / RFC-014 §10).
    """
    for rule in (policy or {}).get("rules", []):
        match = rule.get("match", {})
        if all(merged.get(field) == value for field, value in match.items()):
            return rule.get("scope")
    return (policy or {}).get("default", "triage")


def _apply_entity_scopes(target: dict, scope_policy: dict | None, resolve_scope) -> None:
    """Entity analogue of _apply_scope_policy (RFC-014 §10).

    Same rule semantics (shared _match_policy_scope); the label lands in
    `data.scopes` instead of metadata.scopes, because Core entities have
    a single JSON data column. A provider-set `scopes` wins (SG-7).
    """
    if target.get("scopes"):
        return  # provider labeled it — provider wins
    scope_ref = _match_policy_scope(target, scope_policy)
    if scope_ref is None:
        return  # explicitly unscoped
    scope_uuid = resolve_scope(scope_ref)
    if scope_uuid:
        target["scopes"] = [scope_uuid]


def _upsert_entity(
    yielded: dict,
    soil,
    core,
    provider_name: str,
    run_id: str,
    scope_policy: dict | None,
    resolve_scope,
) -> str:
    """Store one provider-yielded entity upsert (RFC-014 §3).

    Storage mapping: Core entities have a single JSON data column, so the
    merged view of yielded data + metadata is stored flat in entity.data
    (data/metadata must not collide — the merge would be ambiguous).

    Update semantics: incoming top-level keys merge OVER the stored data
    (absent keys keep stored values; an explicit null clears), then the
    operator's scope policy is re-applied to the merged result — labels
    follow the live policy, a provider-set `scopes` wins (SG-7). Google
    resources arrive complete on change, so merge is faithful; the merge
    exists for tombstones, whose payloads guarantee only ids — the stamps
    must not clobber stored belief fields (RFC-014 §4.5).

    No-op rule: stored etag AND source_status both unchanged → skip (an
    hourly run over an unchanged calendar stays cheap, and the delta
    stream stays signal, RFC-014 §4.4).

    Returns "created" | "updated" | "noop". Writes are NOT committed here —
    the run loop commits Core before Soil (RFC-008 orphan-delta ordering).
    """
    entity_type = yielded.get("type")
    data = yielded.get("data") or {}
    metadata = yielded.get("metadata") or {}
    if not entity_type or not isinstance(data, dict) or not isinstance(metadata, dict):
        raise ValidationError(
            "entity upsert requires {op, type, key, data[, metadata]} with dict data",
            {"provider": provider_name, "got_type": type(entity_type).__name__},
        )
    collisions = sorted(set(data) & set(metadata))
    if collisions:
        raise ValidationError(
            f"entity upsert data/metadata key collision: {collisions}",
            {"provider": provider_name, "keys": collisions},
        )

    incoming = {**data, **metadata}
    key = yielded.get("key")
    if not isinstance(key, dict) or not key:
        raise ValidationError(
            "entity upsert requires a non-empty natural key",
            {"provider": provider_name},
        )

    stored = core.entity.find_by_data(entity_type, key)
    if stored is None:
        # A create must carry its full natural key in the payload — the
        # stored data is what every future lookup matches against. A merge
        # (update) may be stamp-only: tombstone payloads guarantee only
        # ids (RFC-014 §4.5), so requiring key fields there would make
        # removal stamps impossible.
        missing = [field for field in key if field not in incoming]
        if missing:
            raise ValidationError(
                f"natural key fields missing from the upsert payload: {missing}",
                {"provider": provider_name, "missing": missing},
            )
        nulls = [field for field in key if incoming.get(field) is None]
        if nulls:
            # json_extract = NULL never matches (NULL is NULL in SQL), so a
            # null key field would create a duplicate entity every run.
            raise ValidationError(
                f"natural key fields must not be null: {nulls}",
                {"provider": provider_name, "nulls": nulls},
            )
    if stored is not None:
        stored_data = stored["data"]
        if (
            stored_data.get("etag") == incoming.get("etag")
            and stored_data.get("source_status") == incoming.get("source_status")
        ):
            return "noop"

    # Scope label (RFC-014 §10): provider-set wins; otherwise the live
    # policy applies. Labels ride `data.scopes` — the entity analogue of
    # fact metadata.scopes (entities have no metadata column; the future
    # entity read-path enforcement reads this).
    if stored is None:
        _apply_entity_scopes(incoming, scope_policy, resolve_scope)
        core.entity.create(entity_type=entity_type, data=incoming)
        return "created"

    entity_id = stored["uuid"]
    previous_hash = stored["hash"]
    # Merge semantics (RFC-014 §3): incoming top-level keys overwrite,
    # absent keys keep stored values — Google resources arrive complete
    # on change, and tombstones are stamp-only payloads that must not
    # clobber stored belief fields. Stored runtime labels don't survive
    # the merge; the live policy re-labels the merged result (identical
    # labels therefore produce no delta noise — the changed-field diff
    # below compares against what was stored).
    merged = {k: v for k, v in stored_data.items() if k != "scopes"}
    merged.update(incoming)
    _apply_entity_scopes(merged, scope_policy, resolve_scope)
    changed = {
        field: {"from": stored_data.get(field), "to": value}
        for field, value in merged.items()
        if stored_data.get(field) != value
    }
    if not changed:
        # Etag churned but content converged (Google etag noise) — nothing
        # to record, and an empty delta would be noise in the stream.
        return "noop"
    core.entity.update_data(entity_id, merged)
    new_hash = core.entity.get_current_hash(entity_id)
    soil.create_fact(
        Fact(
            uuid=generate_soil_uuid(),
            _type="EntityDelta",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={
                # entity_uuid per the entitydelta schema (core_-prefixed);
                # entity_id mirrors it plain because the RFC-008 orphan
                # checker reads data.entity_id.
                "entity_uuid": uid.add_core_prefix(entity_id),
                "entity_id": entity_id,
                "entity_type": entity_type,
                "commit": new_hash,
                "parent": previous_hash,
                "ops": {"changed": changed},
            },
            metadata={
                "provider": provider_name,
                "run_id": run_id,
                "source": "provider_runtime",
            },
            integrity_hash=None,  # computed by create_fact
            fidelity="full",
        )
    )
    return "updated"


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
    scope_ref = _match_policy_scope(merged, policy)
    if scope_ref is None:
        return  # explicitly unscoped
    scope_uuid = resolve_scope(scope_ref)
    if scope_uuid:
        metadata["scopes"] = [scope_uuid]
        fact.metadata = metadata
