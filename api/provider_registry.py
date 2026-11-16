"""Provider registry: storage and registration validation (RFC-004 §12.2).

The registry lives in the `providers` table in core.db — an infrastructure
table alongside api_keys, deliberately outside the entity/delta machinery.
This module owns CRUD and registration-time validation:

- the provider module must be importable in the server environment (sys.path
  first, then loaded as a plugin file from the provider roots), and
- its origin must resolve under an allowlisted provider root (the origin
  pin: registration activates operator-placed code, it does not load
  arbitrary code), and
- its class must conform to the Provider Protocol (PRD: sync() yielding
  Facts; create_relations optional).

Connection handling follows the api_keys middleware precedent: raw SQL via
get_core()._conn (see api/middleware/api.py).
"""

import importlib
import importlib.util
import inspect
import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from system.core import get_core
from system.exceptions import LockConflictError, ResourceNotFound, ValidationError
from utils import isodatetime

from .schemas.provider import (
    RegisterAccountRequest,
    RegisterProviderRequest,
    UpdateAccountRequest,
    UpdateProviderRequest,
)

logger = logging.getLogger(__name__)

# Reserved names that would collide with /api/v1/providers/run routing
RESERVED_NAMES = {"run"}

# Columns selected for manifests, in row order
_COLUMNS = (
    "name, module, class, interval, enabled, description, config, "
    "capabilities, scope_policy, registered_at, last_run_id, last_run_at, "
    "last_status, next_due_at"
)

# Columns selected for account manifests (RFC-012), in row order
_ACCOUNT_COLUMNS = (
    "id, provider_name, platform, subject, label, status, credential_ref, "
    "config, capabilities, error, interval, next_due_at, last_run_id, "
    "last_run_at, last_status, last_verified_at, created_at, updated_at"
)

# Run bookkeeping runs in parallel threads (one per account, RFC-012 §4.5);
# claim/since/finish are microsecond writes that must not interleave —
# serialize them process-wide rather than relying on SQLite lock arbitration.
_RUNS_LOCK = threading.Lock()


def provider_roots() -> list[Path]:
    """Allowlisted provider roots for the origin pin (RFC-004 §12.2).

    Resolution order:
    1. ``MEMOGARDEN_PROVIDER_ROOTS`` (os.pathsep-separated) — overrides all
    2. ``$MEMOGARDEN_DATA_DIR/providers`` — when the data dir is pinned
       (production units pin it)
    3. ``[]`` — unpinned: development/pytest mode, registration accepts any
       importable module
    """
    env_roots = os.environ.get("MEMOGARDEN_PROVIDER_ROOTS")
    if env_roots:
        return [Path(p).expanduser() for p in env_roots.split(os.pathsep) if p.strip()]
    data_dir = os.environ.get("MEMOGARDEN_DATA_DIR")
    if data_dir:
        return [Path(data_dir).expanduser() / "providers"]
    return []


def import_provider_module(module: str):
    """Import a provider module, falling back to the provider roots.

    Tries the plain sys.path import first (installed packages, Poetry
    path-dep checkouts). If the provider module itself is missing, loads it
    as a plugin file from the provider roots: ``<root>/<module>.py`` or
    ``<root>/<module>/__init__.py`` — the operator drops a provider file
    into a root and registers it by bare name.

    Raises:
        ValidationError: If the module cannot be imported or loaded
    """

    def _not_importable(detail: str) -> ValidationError:
        return ValidationError(
            f"module '{module}' is not importable in the server environment "
            "(not on sys.path and not found under the provider roots)",
            {"module": module, "detail": detail},
        )

    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        # A missing dependency inside the provider is a real error; only a
        # missing provider module (same top-level name) falls through to the
        # roots loader.
        if (e.name or "").split(".")[0] != module.split(".")[0]:
            raise _not_importable(str(e)) from e
    except Exception as e:
        raise _not_importable(str(e)) from e

    for root in provider_roots():
        for candidate in (root / f"{module}.py", root / module / "__init__.py"):
            if not candidate.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(module, candidate)
                if spec is None or spec.loader is None:
                    continue
                loaded = importlib.util.module_from_spec(spec)
                sys.modules[module] = loaded
                spec.loader.exec_module(loaded)
                return loaded
            except Exception as e:
                raise _not_importable(
                    f"found at {candidate} but failed to load: {e}"
                ) from e
    raise _not_importable("not found on sys.path or under any provider root")


def validate_provider_origin(module: str, mod) -> None:
    """Origin pin: the module's source must live under a provider root.

    Unpinned (no roots configured) accepts anything — development mode.
    Realpath both sides so symlinked editable installs resolve correctly.

    Raises:
        ValidationError: If the module has no origin or lies outside every root
    """
    roots = provider_roots()
    if not roots:
        return
    origin = getattr(mod, "__file__", None)
    if not origin:
        raise ValidationError(
            f"module '{module}' has no source file (namespace package) — "
            "provider modules must live under a provider root",
            {"module": module, "roots": [str(r) for r in roots]},
        )
    origin_real = Path(origin).resolve()
    for root in roots:
        root_real = root.resolve()
        if origin_real == root_real or root_real in origin_real.parents:
            return
    raise ValidationError(
        f"module '{module}' is outside the provider roots — registration "
        "only activates operator-placed code (RFC-004 §12.2 origin pin)",
        {"module": module, "origin": str(origin_real), "roots": [str(r) for r in roots]},
    )


def validate_provider_class(module: str, class_name: str) -> list[str]:
    """Import the module and check Provider Protocol conformance.

    Args:
        module: Importable module name (or bare name of a file under a
            provider root)
        class_name: Class within the module

    Returns:
        Derived capabilities list, e.g. ["sync"] or ["sync", "create_relations"]

    Raises:
        ValidationError: If the module is not importable or outside the
            provider roots, or the class does not conform to the Protocol
    """
    mod = import_provider_module(module)
    validate_provider_origin(module, mod)

    cls = getattr(mod, class_name, None)
    if cls is None:
        raise ValidationError(
            f"module '{module}' has no class '{class_name}'",
            {"module": module, "class": class_name},
        )
    if not callable(cls):
        raise ValidationError(
            f"'{module}.{class_name}' is not callable",
            {"module": module, "class": class_name},
        )
    if not callable(getattr(cls, "sync", None)):
        raise ValidationError(
            f"'{module}.{class_name}' does not implement the Provider Protocol "
            "(missing callable sync)",
            {"module": module, "class": class_name},
        )

    capabilities = ["sync"]
    if callable(getattr(cls, "create_relations", None)):
        capabilities.append("create_relations")
    if getattr(cls, "ENTITY_UPSERTS", False):
        # RFC-014 §3: the provider yields entity upserts alongside facts;
        # the runtime refuses entity yields from providers that do not
        # declare this (defense in depth at run time).
        capabilities.append("upsert_entities")
    return capabilities


def _row_to_manifest(row) -> dict:
    """Convert a providers table row to an API manifest dict."""
    try:
        config = json.loads(row["config"]) if row["config"] else {}
    except (json.JSONDecodeError, TypeError):
        config = {}
    try:
        capabilities = json.loads(row["capabilities"]) if row["capabilities"] else []
    except (json.JSONDecodeError, TypeError):
        capabilities = []
    try:
        scope_policy = json.loads(row["scope_policy"]) if row["scope_policy"] else None
    except (json.JSONDecodeError, TypeError):
        scope_policy = None
    return {
        "name": row["name"],
        "module": row["module"],
        "class": row["class"],
        "interval": row["interval"],
        "enabled": bool(row["enabled"]),
        "description": row["description"],
        "config": config,
        "capabilities": capabilities,
        "scope_policy": scope_policy,
        "registered_at": row["registered_at"],
        "last_run_id": row["last_run_id"],
        "last_run_at": row["last_run_at"],
        "last_status": row["last_status"],
        "next_due_at": row["next_due_at"],
    }


def register_provider(data: RegisterProviderRequest) -> dict:
    """Validate and register a provider.

    Args:
        data: Registration payload

    Returns:
        Stored manifest including server-assigned fields

    Raises:
        ValidationError: On reserved name or failed Protocol validation
        ConflictError: If the name is already registered
    """
    if data.name in RESERVED_NAMES:
        raise ValidationError(
            f"provider name '{data.name}' is reserved",
            {"name": data.name, "reserved": sorted(RESERVED_NAMES)},
        )

    capabilities = validate_provider_class(data.module, data.class_name)
    now = isodatetime.now()

    core = get_core()
    try:
        core._conn.execute(
            """INSERT INTO providers
               (name, module, class, interval, enabled, description, config,
                capabilities, registered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.name,
                data.module,
                data.class_name,
                data.interval,
                1 if data.enabled else 0,
                data.description,
                json.dumps(data.config),
                json.dumps(capabilities),
                now,
            ),
        )
        core._conn.commit()
    except sqlite3.IntegrityError:
        raise LockConflictError(
            f"provider '{data.name}' is already registered",
            {"name": data.name},
        ) from None

    logger.info("Provider registered: %s (module=%s, capabilities=%s)",
                data.name, data.module, capabilities)
    return get_provider(data.name)


def list_providers() -> list[dict]:
    """List all registered providers with status."""
    core = get_core()
    rows = core._conn.execute(
        f"SELECT {_COLUMNS} FROM providers ORDER BY name ASC"
    ).fetchall()
    return [_row_to_manifest(row) for row in rows]


def get_provider(name: str) -> dict:
    """Get a single provider manifest by name.

    Raises:
        ResourceNotFound: If no provider with that name is registered
    """
    core = get_core()
    row = core._conn.execute(
        f"SELECT {_COLUMNS} FROM providers WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ResourceNotFound(f"provider '{name}' is not registered", {"name": name})
    return _row_to_manifest(row)


def update_provider(name: str, data: UpdateProviderRequest) -> dict:
    """Update an existing provider's mutable fields.

    Args:
        name: Registered provider name
        data: Patch payload; omitted fields stay unchanged

    Returns:
        Updated manifest

    Raises:
        ResourceNotFound: If no provider with that name is registered
    """
    get_provider(name)  # 404 if unknown

    fields = data.model_dump(exclude_unset=True)
    if fields:
        assignments, params = [], []
        for key, value in fields.items():
            if key == "enabled":
                assignments.append("enabled = ?")
                params.append(1 if value else 0)
            elif key == "config":
                assignments.append("config = ?")
                params.append(json.dumps(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        params.append(name)
        core = get_core()
        core._conn.execute(
            f"UPDATE providers SET {', '.join(assignments)} WHERE name = ?",
            params,
        )
        core._conn.commit()

    return get_provider(name)


def set_scope_policy(name: str, policy: dict | None) -> dict:
    """Set (or clear) a provider's scope policy (RFC-011 §4.8, item 4).

    Shape: {"rules": [{"match": {<metadata-or-data field>: value, ...},
    "scope": "<uuid | builtin alias>" | null}, ...],
    "default": "<uuid | alias>" | null}. Evaluation is first-match-wins;
    the default catch-all routes everything unmatched to triage when
    unconfigured (ratified 2026-11-12, fail-closed). scope null on a
    matching rule (or a null default) = deliver unscoped.

    Args:
        name: Registered provider name
        policy: The policy dict, or None to clear (back to ratified default)

    Returns:
        Updated manifest
    """
    core = get_core()
    row = core._conn.execute(
        "SELECT name FROM providers WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ResourceNotFound(f"provider '{name}' is not registered", {"name": name})

    core._conn.execute(
        "UPDATE providers SET scope_policy = ? WHERE name = ?",
        (json.dumps(policy) if policy is not None else None, name),
    )
    core._conn.commit()
    return get_provider(name)


def _row_to_account(row) -> dict:
    """Convert a provider_accounts row to an API manifest dict (RFC-012)."""
    try:
        config = json.loads(row["config"]) if row["config"] else {}
    except (json.JSONDecodeError, TypeError):
        config = {}
    try:
        capabilities = json.loads(row["capabilities"]) if row["capabilities"] else []
    except (json.JSONDecodeError, TypeError):
        capabilities = []
    return {
        "id": row["id"],
        "provider_name": row["provider_name"],
        "platform": row["platform"],
        "subject": row["subject"],
        "label": row["label"],
        "status": row["status"],
        "credential_ref": row["credential_ref"],
        "config": config,
        "capabilities": capabilities,
        "error": row["error"],
        "interval": row["interval"],
        "next_due_at": row["next_due_at"],
        "last_run_id": row["last_run_id"],
        "last_run_at": row["last_run_at"],
        "last_status": row["last_status"],
        "last_verified_at": row["last_verified_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_account_row(provider_name: str, account_id: str):
    core = get_core()
    row = core._conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM provider_accounts "
        "WHERE provider_name = ? AND id = ?",
        (provider_name, account_id),
    ).fetchone()
    if row is None:
        raise ResourceNotFound(
            f"account '{account_id}' is not registered for provider '{provider_name}'",
            {"provider": provider_name, "account_id": account_id},
        )
    return row


def provider_platforms(name: str) -> list[str] | None:
    """Platforms the provider's class accepts, or None if undeclared.

    Reads the class PLATFORMS attribute (RFC-012 §6.1); a class that does
    not declare it accepts any platform (back-compatibility with
    account-less providers that predate the attribute).
    """
    manifest = get_provider(name)
    mod = import_provider_module(manifest["module"])
    cls = getattr(mod, manifest["class"], None)
    platforms = getattr(cls, "PLATFORMS", None) if cls is not None else None
    if platforms is None:
        return None
    return [str(p) for p in platforms]


def accepts_account_cls(cls) -> bool:
    """Whether a provider class constructor takes an account context.

    The runtime passes the account context only to classes whose __init__
    declares an `account` parameter (RFC-012 §4.5); the same check gates
    account registration, so a provider can never be registered for
    accounts it would silently ignore.
    """
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False
    return "account" in params


def provider_accepts_account(name: str) -> bool:
    """Whether the registered provider class is account-backed."""
    manifest = get_provider(name)
    mod = import_provider_module(manifest["module"])
    cls = getattr(mod, manifest["class"], None)
    return cls is not None and accepts_account_cls(cls)


def register_account(provider_name: str, data: RegisterAccountRequest) -> dict:
    """Register an account binding for a provider (RFC-012 §4.1/§6.1).

    Validates that the provider exists, is account-backed, and (when its
    class declares PLATFORMS) that the account's platform is accepted. The
    credential reference is recorded as given — the server cannot and does
    not verify it against HACM (INV-2); mis references surface at run time
    as refresh failures → needs_reauth, and the runtime re-checks the
    subject against the credential's mailbox at every sync (INV-3).

    Raises:
        ResourceNotFound: Unknown provider
        ValidationError: Provider not account-backed, or platform rejected
        LockConflictError: (provider, subject) already registered
    """
    get_provider(provider_name)  # 404 if unknown

    if not provider_accepts_account(provider_name):
        raise ValidationError(
            f"provider '{provider_name}' is not account-backed — its class "
            "constructor does not accept an `account` context (RFC-012 §4.5)",
            {"provider": provider_name},
        )

    platforms = provider_platforms(provider_name)
    if platforms is not None and data.platform not in platforms:
        raise ValidationError(
            f"provider '{provider_name}' does not accept platform '{data.platform}' "
            f"(accepts: {', '.join(platforms)})",
            {"provider": provider_name, "platform": data.platform, "accepted": platforms},
        )

    account_id = str(uuid.uuid4())
    now = isodatetime.now()
    core = get_core()
    try:
        core._conn.execute(
            """INSERT INTO provider_accounts
               (id, provider_name, platform, subject, label, status,
                credential_ref, config, capabilities, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
            (
                account_id,
                provider_name,
                data.platform,
                data.subject,
                data.label,
                data.credential_ref or f"{data.platform}:{data.subject}",
                json.dumps(data.config),
                json.dumps(data.capabilities),
                now,
                now,
            ),
        )
        core._conn.commit()
    except sqlite3.IntegrityError:
        raise LockConflictError(
            f"provider '{provider_name}' already has an account for subject "
            f"'{data.subject}'",
            {"provider": provider_name, "subject": data.subject},
        ) from None

    logger.info(
        "Provider account registered: %s/%s (platform=%s, subject=%s)",
        provider_name, account_id, data.platform, data.subject,
    )
    return get_account(provider_name, account_id)


def list_accounts(provider_name: str) -> list[dict]:
    """List a provider's account bindings (RFC-012 §6.1)."""
    get_provider(provider_name)  # 404 if unknown
    core = get_core()
    rows = core._conn.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM provider_accounts "
        "WHERE provider_name = ? ORDER BY subject ASC",
        (provider_name,),
    ).fetchall()
    return [_row_to_account(row) for row in rows]


def get_account(provider_name: str, account_id: str) -> dict:
    """Get one account binding (RFC-012 §6.1).

    Raises:
        ResourceNotFound: Unknown provider or account
    """
    get_provider(provider_name)  # 404 if unknown
    return _row_to_account(_get_account_row(provider_name, account_id))


def update_account(
    provider_name: str, account_id: str, data: UpdateAccountRequest
) -> dict:
    """Update an account's mutable fields (label, status, config, interval).

    Returns:
        Updated manifest
    """
    _get_account_row(provider_name, account_id)  # 404 if unknown

    fields = data.model_dump(exclude_unset=True)
    if fields:
        assignments, params = [], []
        for key, value in fields.items():
            if key == "config":
                assignments.append("config = ?")
                params.append(json.dumps(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(isodatetime.now())
        params.extend([provider_name, account_id])
        core = get_core()
        core._conn.execute(
            f"UPDATE provider_accounts SET {', '.join(assignments)} "
            "WHERE provider_name = ? AND id = ?",
            params,
        )
        core._conn.commit()

    return get_account(provider_name, account_id)


def remove_account(provider_name: str, account_id: str) -> None:
    """Delete an account binding (RFC-012 §6.1).

    Removes the registry row only. The HACM credential it references is
    operator-managed and deliberately untouched (INV-2: the server records
    references; custody operations stay on the host side).

    Raises:
        ResourceNotFound: Unknown provider or account
    """
    _get_account_row(provider_name, account_id)  # 404 if unknown
    core = get_core()
    core._conn.execute(
        "DELETE FROM provider_accounts WHERE provider_name = ? AND id = ?",
        (provider_name, account_id),
    )
    core._conn.commit()
    logger.info("Provider account removed: %s/%s", provider_name, account_id)


def set_account_status(provider_name: str, account_id: str, status: str,
                       error: str | None = None) -> dict:
    """Set an account's lifecycle status (RFC-012 §4.2).

    Written by exactly three paths: run outcomes (needs_reauth on an auth
    failure), the verify endpoint, and operator actions. `error` records
    the failure summary; pass error=None to clear it.

    Raises:
        ResourceNotFound: Unknown provider or account
    """
    _get_account_row(provider_name, account_id)  # 404 if unknown
    core = get_core()
    core._conn.execute(
        """UPDATE provider_accounts
           SET status = ?, error = ?, updated_at = ?
           WHERE provider_name = ? AND id = ?""",
        (status, error, isodatetime.now(), provider_name, account_id),
    )
    core._conn.commit()
    return get_account(provider_name, account_id)


def mark_account_verified(provider_name: str, account_id: str, ok: bool,
                          error: str | None = None) -> dict:
    """Record a credential verification outcome (RFC-012 §4.2/§6.1).

    A passing probe re-activates a needs_reauth account; a failing probe
    parks it in needs_reauth so the heartbeat skips it until re-linked.
    Operator-set holds are respected: a disabled or revoked account keeps
    its status (verification records the outcome only).
    """
    row = _get_account_row(provider_name, account_id)
    if row["status"] in ("disabled", "revoked"):
        status = row["status"]
    else:
        status = "active" if ok else "needs_reauth"
    core = get_core()
    core._conn.execute(
        """UPDATE provider_accounts
           SET status = ?, error = ?, last_verified_at = ?, updated_at = ?
           WHERE provider_name = ? AND id = ?""",
        (status, None if ok else error, isodatetime.now(), isodatetime.now(),
         provider_name, account_id),
    )
    core._conn.commit()
    return get_account(provider_name, account_id)


def get_due_accounts(now: str | None = None) -> list[dict]:
    """Get active accounts of enabled providers whose next_due_at is due.

    Scheduling for account-backed providers lives here (RFC-012 §4.5):
    account next_due_at NULL = due now; account interval NULL = inherit
    the provider interval (returned as `effective_interval`).
    """
    if now is None:
        now = isodatetime.now()
    qual = ", ".join(f"a.{c.strip()}" for c in _ACCOUNT_COLUMNS.split(","))
    core = get_core()
    rows = core._conn.execute(
        f"""SELECT {qual}, p.interval AS provider_interval
            FROM provider_accounts a
            JOIN providers p ON p.name = a.provider_name
            WHERE p.enabled = 1 AND a.status = 'active'
              AND (a.next_due_at IS NULL OR a.next_due_at <= ?)
            ORDER BY a.provider_name ASC, a.subject ASC""",
        (now,),
    )
    accounts = []
    for row in rows.fetchall():
        account = _row_to_account(row)
        account["effective_interval"] = row["interval"] or row["provider_interval"]
        accounts.append(account)
    return accounts


def get_due_providers(now: str | None = None) -> list[dict]:
    """Get enabled providers whose next_due_at is NULL (due now) or past.

    Account-backed providers are excluded — their due-ness lives on the
    accounts (get_due_accounts, RFC-012 §4.5).
    """
    if now is None:
        now = isodatetime.now()
    core = get_core()
    rows = core._conn.execute(
        f"""SELECT {_COLUMNS} FROM providers
            WHERE enabled = 1 AND (next_due_at IS NULL OR next_due_at <= ?)
              AND NOT EXISTS (
                  SELECT 1 FROM provider_accounts
                  WHERE provider_accounts.provider_name = providers.name
              )
            ORDER BY name ASC""",
        (now,),
    ).fetchall()
    return [_row_to_manifest(row) for row in rows]


def get_last_success_at(name: str, account_id: str = "") -> str | None:
    """Get the time of the last successful run, if any.

    Account-level watermark (RFC-012 §4.5): reads the provider_runs history
    for the (provider, account) pair. Provider-level (account_id '') keeps
    the providers aggregate read — the pre-RFC-012 behavior account-less
    providers rely on.
    """
    core = get_core()
    if account_id == "":
        row = core._conn.execute(
            "SELECT last_run_at FROM providers WHERE name = ? AND last_status = 'success'",
            (name,),
        ).fetchone()
        return row["last_run_at"] if row else None
    with _RUNS_LOCK:
        row = core._conn.execute(
            """SELECT MAX(finished_at) AS last_success
               FROM provider_runs
               WHERE provider_name = ? AND account_id = ? AND status = 'success'""",
            (name, account_id),
        ).fetchone()
    return row["last_success"] if row else None


def claim_run(name: str, triggered_by: str, account_id: str = "") -> str:
    """Claim a run slot by inserting a 'running' row (RFC-012 §4.4).

    Single-flight is enforced by the partial unique index on
    provider_runs(provider_name, account_id) WHERE status = 'running' —
    safe across gunicorn workers, and per account: one wedged account
    cannot block another account's run.

    Args:
        name: Provider name
        triggered_by: 'heartbeat' or 'manual'
        account_id: '' for a provider-level run, else the provider_accounts.id

    Returns:
        The run id

    Raises:
        ConflictError: If a run is already in flight for this provider
            (provider-level) or (provider, account)
    """
    run_id = str(uuid.uuid4())
    now = isodatetime.now()
    core = get_core()
    try:
        with _RUNS_LOCK:
            core._conn.execute(
                """INSERT INTO provider_runs
                   (id, provider_name, account_id, status, started_at, triggered_by)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (run_id, name, account_id, now, triggered_by),
            )
            core._conn.commit()
    except sqlite3.IntegrityError:
        where = f"'{name}'/{account_id}" if account_id else f"'{name}'"
        raise LockConflictError(
            f"provider {where} already has a run in progress",
            {"name": name, "account_id": account_id},
        ) from None
    return run_id


def finish_run(
    run_id: str,
    name: str,
    started_at: str,
    status: str,
    detail: dict,
    interval: int,
    account_id: str = "",
) -> None:
    """Record a finished run and update bookkeeping.

    Sets last_run_at to the run's start time and next_due_at to
    finished_at + interval (uniform retry cadence for success and failure;
    the run detail carries the outcome).

    Account runs (RFC-012 §4.5) additionally update the provider_accounts
    row — its own last_run_* bookkeeping, its next_due_at, and its error
    summary (cleared on success). The provider row's aggregate
    last_run_* fields reflect the latest run of any account; its
    next_due_at is left NULL because due-ness for account-backed providers
    lives on the accounts.
    """
    finished_at = isodatetime.now()
    try:
        next_due = (
            datetime.fromisoformat(finished_at) + timedelta(seconds=interval)
        )
        next_due_at = isodatetime.to_timestamp(next_due)
    except ValueError:
        next_due_at = None

    core = get_core()
    with _RUNS_LOCK:
        core._conn.execute(
            """UPDATE provider_runs
               SET status = ?, finished_at = ?, detail = ?
               WHERE id = ?""",
            (status, finished_at, json.dumps(detail), run_id),
        )
        if account_id == "":
            core._conn.execute(
                """UPDATE providers
                   SET last_run_id = ?, last_run_at = ?, last_status = ?, next_due_at = ?
                   WHERE name = ?""",
                (run_id, started_at, status, next_due_at, name),
            )
        else:
            core._conn.execute(
                """UPDATE provider_accounts
                   SET last_run_id = ?, last_run_at = ?, last_status = ?,
                       next_due_at = ?, error = ?, updated_at = ?
                   WHERE provider_name = ? AND id = ?""",
                (run_id, started_at, status, next_due_at,
                 detail.get("error"), isodatetime.now(), name, account_id),
            )
            core._conn.execute(
                """UPDATE providers
                   SET last_run_id = ?, last_run_at = ?, last_status = ?
                   WHERE name = ?""",
                (run_id, started_at, status, name),
            )
        core._conn.commit()


def cleanup_orphaned_runs() -> int:
    """Mark stale 'running' rows as failed (server restart interrupted them).

    Called once at server startup (PRD always-available-startup: a crashed
    run must not wedge the single-flight lock forever).

    Returns:
        Number of rows marked failed
    """
    now = isodatetime.now()
    core = get_core()
    cursor = core._conn.execute(
        """UPDATE provider_runs
           SET status = 'failed', finished_at = ?,
               detail = '{"error": "interrupted by server restart"}'
           WHERE status = 'running'""",
        (now,),
    )
    core._conn.commit()
    if cursor.rowcount:
        logger.warning("Marked %d orphaned provider run(s) as failed", cursor.rowcount)
    return cursor.rowcount
