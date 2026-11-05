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
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from system.core import get_core
from system.exceptions import LockConflictError, ResourceNotFound, ValidationError
from utils import isodatetime

from .schemas.provider import RegisterProviderRequest, UpdateProviderRequest

logger = logging.getLogger(__name__)

# Reserved names that would collide with /api/v1/providers/run routing
RESERVED_NAMES = {"run"}

# Columns selected for manifests, in row order
_COLUMNS = (
    "name, module, class, interval, enabled, description, config, "
    "capabilities, registered_at, last_run_id, last_run_at, last_status, next_due_at"
)


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
    return {
        "name": row["name"],
        "module": row["module"],
        "class": row["class"],
        "interval": row["interval"],
        "enabled": bool(row["enabled"]),
        "description": row["description"],
        "config": config,
        "capabilities": capabilities,
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


def get_due_providers(now: str | None = None) -> list[dict]:
    """Get enabled providers whose next_due_at is NULL (due now) or past."""
    if now is None:
        now = isodatetime.now()
    core = get_core()
    rows = core._conn.execute(
        f"""SELECT {_COLUMNS} FROM providers
            WHERE enabled = 1 AND (next_due_at IS NULL OR next_due_at <= ?)
            ORDER BY name ASC""",
        (now,),
    ).fetchall()
    return [_row_to_manifest(row) for row in rows]


def get_last_success_at(name: str) -> str | None:
    """Get the time of the provider's last successful run, if any."""
    core = get_core()
    row = core._conn.execute(
        "SELECT last_run_at FROM providers WHERE name = ? AND last_status = 'success'",
        (name,),
    ).fetchone()
    return row["last_run_at"] if row else None


def claim_run(name: str, triggered_by: str) -> str:
    """Claim a run slot for the provider by inserting a 'running' row.

    Single-flight is enforced by the partial unique index on
    provider_runs(provider_name) WHERE status = 'running' — safe across
    gunicorn workers.

    Args:
        name: Provider name
        triggered_by: 'heartbeat' or 'manual'

    Returns:
        The run id

    Raises:
        ConflictError: If a run is already in flight for this provider
    """
    run_id = str(uuid.uuid4())
    now = isodatetime.now()
    core = get_core()
    try:
        core._conn.execute(
            """INSERT INTO provider_runs (id, provider_name, status, started_at, triggered_by)
               VALUES (?, ?, 'running', ?, ?)""",
            (run_id, name, now, triggered_by),
        )
        core._conn.commit()
    except sqlite3.IntegrityError:
        raise LockConflictError(
            f"provider '{name}' already has a run in progress",
            {"name": name},
        ) from None
    return run_id


def finish_run(
    run_id: str,
    name: str,
    started_at: str,
    status: str,
    detail: dict,
    interval: int,
) -> None:
    """Record a finished run and update the provider's bookkeeping.

    Sets last_run_at to the run's start time and next_due_at to
    finished_at + interval (uniform retry cadence for success and failure;
    the run detail carries the outcome).
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
    core._conn.execute(
        """UPDATE provider_runs
           SET status = ?, finished_at = ?, detail = ?
           WHERE id = ?""",
        (status, finished_at, json.dumps(detail), run_id),
    )
    core._conn.execute(
        """UPDATE providers
           SET last_run_id = ?, last_run_at = ?, last_status = ?, next_due_at = ?
           WHERE name = ?""",
        (run_id, started_at, status, next_due_at, name),
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
