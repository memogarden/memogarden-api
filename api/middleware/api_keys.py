"""API key and agent identity service (RFC-010).

This module provides:
- API key CRUD operations (optionally bound to an Agent entity)
- Agent entity issuance (operator-gated identity, RFC-010)
- API key verification for authentication (returns the agent binding)
- Agent bind / re-bind / unbind for existing keys

Confines bcrypt dependency for API key hashing to this module only.
"""

import json
import sqlite3
from datetime import datetime

from system.exceptions import (
    LockConflictError,
    PermissionDenied,
    ResourceNotFound,
    ValidationError,
)
from utils import hash_chain, isodatetime, secret, uid

from .schemas import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    APIKeyCreate,
    APIKeyListResponse,
    APIKeyResponse,
)
from .service import hash_password  # Reuse password hashing for API keys

# ============================================================================
# API Key Generation (wrapper around utils.secret)
# ============================================================================


def generate_api_key() -> str:
    """
    Generate a new API key for agent authentication.

    This is a convenience wrapper around utils.secret.generate_api_key().

    Returns:
        API key string (e.g., "mg_sk_agent_abc123def456...")

    Example:
    ```python
    key = generate_api_key()
    # Returns: "mg_sk_agent_9a2b8c7d..."
    ```

    Note:
        This uses utils.secret.generate_api_key() internally.
        All API key generation is confined to utils.secret module.
    """
    return secret.generate_api_key()


def get_api_key_prefix(api_key: str) -> str:
    """
    Extract the prefix from an API key for display.

    This is a convenience wrapper around utils.secret.get_api_key_prefix().

    Args:
        api_key: Full API key

    Returns:
        API key prefix (first 12 characters)

    Example:
    ```python
    prefix = get_api_key_prefix("mg_sk_agent_abc123def456...")
    # Returns: "mg_sk_agent_"
    ```

    Note:
        This uses utils.secret.get_api_key_prefix() internally.
    """
    return secret.get_api_key_prefix(api_key)


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key using bcrypt (reusing password hashing).

    Args:
        api_key: Plain text API key

    Returns:
        Bcrypt hash as a string (60 characters)

    Example:
    ```python
    hashed = hash_api_key("mg_sk_agent_abc123...")
    # Returns: "$2b$12$..." (60 character hash)
    ```
    """
    return hash_password(api_key)


def verify_api_key(api_key: str, key_hash: str) -> bool:
    """
    Verify an API key against its hash.

    Args:
        api_key: Plain text API key to verify
        key_hash: Bcrypt hash to compare against

    Returns:
        True if API key matches hash, False otherwise

    Example:
    ```python
    if verify_api_key(input_key, stored_hash):
        print("API key valid")
    ```
    """
    from .service import verify_password
    return verify_password(api_key, key_hash)


# ============================================================================
# API Key CRUD Operations
# ============================================================================


def create_api_key(conn: sqlite3.Connection, user_id: str, data: APIKeyCreate) -> APIKeyResponse:
    """
    Create a new API key for a user.

    Args:
        conn: Database connection
        user_id: User ID who owns the API key
        data: API key creation data (name, optional expires_at)

    Returns:
        Created API key response with full key (only shown once)

    Raises:
        sqlite3.IntegrityError: User doesn't exist

    Example:
    ```python
    data = APIKeyCreate(name="claude-code", expires_at=None)
    api_key = create_api_key(conn, user_id, data)
    print(f"Created API key: {api_key.key}")  # Full key shown only here
    ```
    """
    api_key_id = secret.generate_uuid()
    now = isodatetime.now()
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)
    key_prefix = get_api_key_prefix(plain_key)

    # RFC-010: validate the agent binding (explicit, operator-gated)
    agent_entity_id = None
    if data.agent_id is not None:
        agent_entity_id = _validate_agent_for_user(conn, data.agent_id, user_id)

    # Create entity registry entry first (same ID as API key)
    # Include required fields for new schema (hash, version, data)
    entity_hash = hash_chain.compute_entity_hash(
        entity_type="ApiKey",
        created_at=now,
        updated_at=now,
        previous_hash=None  # Initial entity has no previous hash
    )
    conn.execute(
        """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
        VALUES (?, 'ApiKey', ?, ?, ?, ?, ?)""",
        (api_key_id, entity_hash, 1, now, now, json.dumps({}))
    )

    # Convert expires_at datetime to ISO string if present
    expires_at_str = None
    if data.expires_at:
        if isinstance(data.expires_at, datetime):
            expires_at_str = isodatetime.to_timestamp(data.expires_at)
        else:
            expires_at_str = data.expires_at

    # Create API key entity (foreign key to entity)
    conn.execute(
        """INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, agent_entity_id, capabilities, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (api_key_id, user_id, data.name, key_hash, key_prefix, agent_entity_id,
         json.dumps(data.capabilities or []), expires_at_str, now)
    )

    # RFC-011 §6: a key created bound to an agent inherits the agent's
    # grant envelope at bind (creation) time
    if agent_entity_id:
        _merge_agent_envelope(conn, api_key_id, agent_entity_id)

    return APIKeyResponse(
        id=api_key_id,
        name=data.name,
        key=plain_key,  # Full key only shown on creation
        prefix=key_prefix,
        agent_id=agent_entity_id,
        capabilities=data.capabilities or [],
        expires_at=data.expires_at if isinstance(data.expires_at, datetime) else (isodatetime.to_datetime(data.expires_at) if data.expires_at else None),
        created_at=isodatetime.to_datetime(now),
        last_seen=None,
        revoked_at=None
    )


def list_api_keys(conn: sqlite3.Connection, user_id: str) -> list[APIKeyListResponse]:
    """
    List all API keys for a user (full key never shown).

    Args:
        conn: Database connection
        user_id: User ID to list API keys for

    Returns:
        List of API key responses (without full keys)

    Example:
    ```python
    api_keys = list_api_keys(conn, user_id)
    for api_key in api_keys:
        print(f"{api_key.name}: {api_key.prefix}...")  # Prefix only
    ```
    """
    cursor = conn.execute(
        """SELECT id, name, key_prefix, agent_entity_id, capabilities, expires_at, created_at, last_seen, revoked_at
        FROM api_keys
        WHERE user_id = ?
        ORDER BY created_at DESC""",
        (user_id,)
    )

    results = []
    for row in cursor.fetchall():
        results.append(APIKeyListResponse(
            id=row["id"],
            name=row["name"],
            prefix=row["key_prefix"],
            agent_id=row["agent_entity_id"],
            capabilities=json.loads(row["capabilities"] or "[]"),
            expires_at=isodatetime.to_datetime(row["expires_at"]) if row["expires_at"] else None,
            created_at=isodatetime.to_datetime(row["created_at"]),
            last_seen=isodatetime.to_datetime(row["last_seen"]) if row["last_seen"] else None,
            revoked_at=isodatetime.to_datetime(row["revoked_at"]) if row["revoked_at"] else None,
        ))

    return results


def revoke_api_key(conn: sqlite3.Connection, api_key_id: str, user_id: str) -> bool:
    """
    Revoke an API key by setting revoked_at timestamp (soft delete).

    Args:
        conn: Database connection
        api_key_id: API key ID to revoke
        user_id: User ID who owns the API key (for authorization)

    Returns:
        True if API key was revoked, False if not found

    Example:
    ```python
    success = revoke_api_key(conn, api_key_id, user_id)
    if success:
        print("API key revoked")
    ```
    """
    now = isodatetime.now()
    cursor = conn.execute(
        """UPDATE api_keys
        SET revoked_at = ?
        WHERE id = ? AND user_id = ? AND revoked_at IS NULL""",
        (now, api_key_id, user_id)
    )
    return cursor.rowcount > 0


def get_api_key_by_id(conn: sqlite3.Connection, api_key_id: str) -> APIKeyListResponse | None:
    """
    Get an API key by ID (without full key).

    Args:
        conn: Database connection
        api_key_id: API key UUID

    Returns:
        API key response if found, None otherwise

    Example:
    ```python
    api_key = get_api_key_by_id(conn, api_key_id)
    if api_key:
        print(f"API key: {api_key.name} ({api_key.prefix}...)")
    ```
    """
    cursor = conn.execute(
        """SELECT id, name, key_prefix, agent_entity_id, capabilities, expires_at, created_at, last_seen, revoked_at
        FROM api_keys
        WHERE id = ?""",
        (api_key_id,)
    )
    row = cursor.fetchone()

    if row is None:
        return None

    return APIKeyListResponse(
        id=row["id"],
        name=row["name"],
        prefix=row["key_prefix"],
        agent_id=row["agent_entity_id"],
        capabilities=json.loads(row["capabilities"] or "[]"),
        expires_at=isodatetime.to_datetime(row["expires_at"]) if row["expires_at"] else None,
        created_at=isodatetime.to_datetime(row["created_at"]),
        last_seen=isodatetime.to_datetime(row["last_seen"]) if row["last_seen"] else None,
        revoked_at=isodatetime.to_datetime(row["revoked_at"]) if row["revoked_at"] else None,
    )


def update_last_seen(conn: sqlite3.Connection, api_key_id: str) -> None:
    """
    Update the last_seen timestamp for an API key.

    Called when an API key is successfully used for authentication.

    Args:
        conn: Database connection
        api_key_id: API key ID to update

    Example:
    ```python
    update_last_seen(conn, api_key_id)
    ```
    """
    now = isodatetime.now()
    conn.execute(
        """UPDATE api_keys
        SET last_seen = ?
        WHERE id = ?""",
        (now, api_key_id)
    )


# ============================================================================
# API Key Authentication
# ============================================================================


def verify_api_key_and_get_user(conn: sqlite3.Connection, plain_key: str) -> tuple[str, str, str | None] | None:
    """
    Verify an API key and return the user ID, API key ID, and agent binding.

    Used by authentication middleware to authenticate API key requests.
    The agent binding (RFC-010) is resolved here — read-only — so the
    middleware can attribute the request: agent_entity_id is the bound
    Agent entity uuid, or None for unbound keys (attribution falls back
    to the owning username with actor_type "api_key").

    Args:
        conn: Database connection
        plain_key: Plain text API key from the X-API-Key header

    Returns:
        Tuple of (user_id, api_key_id, agent_entity_id, grants) if valid,
        None otherwise. grants is the parsed list of RFC-011 scope grants
        (possibly empty); the middleware stores it on flask.g — a JWT
        session instead gets None, meaning operator sovereignty (SG-11:
        implicit all-grants).

    Example:
    ```python
    result = verify_api_key_and_get_user(conn, plain_key)
    if result:
        user_id, api_key_id, agent_entity_id, grants, capabilities = result
        print(f"Authenticated user {user_id}, agent binding: {agent_entity_id}")
    ```
    """
    # Get all active API keys and check each one
    # (We can't hash the input key and look it up because bcrypt has random salts)
    cursor = conn.execute(
        """SELECT id, user_id, key_hash, agent_entity_id, grants, capabilities
        FROM api_keys
        WHERE revoked_at IS NULL"""
    )

    for row in cursor.fetchall():
        if verify_api_key(plain_key, row["key_hash"]):
            # Update last_seen timestamp
            update_last_seen(conn, row["id"])
            grants = json.loads(row["grants"]) if row["grants"] else []
            capabilities = json.loads(row["capabilities"]) if row["capabilities"] else []
            return (row["user_id"], row["id"], row["agent_entity_id"], grants, capabilities)

    return None


# ============================================================================
# Agent Identity (RFC-010)
# ============================================================================


def _validate_agent_for_user(conn: sqlite3.Connection, agent_id: str, user_id: str) -> str:
    """
    Validate that agent_id references an Agent entity owned by user_id.

    Read-only validation helper shared by key creation and binding.
    Accepts the uuid with or without the canonical "core_" prefix and
    returns the canonical (prefixed) form as stored in entity.uuid.

    Args:
        conn: Database connection
        agent_id: Agent entity uuid (core_... or bare)
        user_id: Owning user id

    Returns:
        Canonical agent entity uuid (core_...)

    Raises:
        ResourceNotFound: No Agent entity with that uuid
        ValidationError: Entity exists but is not an Agent or not owned
    """
    canonical = uid.add_core_prefix(agent_id)
    row = conn.execute(
        "SELECT type, data FROM entity WHERE uuid = ?",
        (canonical,)
    ).fetchone()

    if row is None:
        raise ResourceNotFound(
            "Agent not found",
            {"agent_id": canonical}
        )
    if row["type"] != "Agent":
        raise ValidationError(
            "Entity exists but is not an Agent",
            {"agent_id": canonical, "type": row["type"]}
        )

    owner = json.loads(row["data"]).get("owner")
    if owner != user_id:
        raise PermissionDenied(
            "Agent is owned by a different user",
            {"agent_id": canonical}
        )

    return canonical


def create_agent(conn: sqlite3.Connection, user_id: str, data: AgentCreate) -> AgentResponse:
    """
    Mint an Agent entity (RFC-010 identity issuance).

    The ONLY creation path for agent identity — callers must be
    JWT-authenticated operator requests (enforced at the endpoint).
    Authentication never creates agents (RFC-010 INV-1).

    Args:
        conn: Database connection
        user_id: Owning user id
        data: AgentCreate (label, optional description)

    Returns:
        AgentResponse for the minted agent

    Raises:
        LockConflictError: Label already used by another agent (409)

    Example:
    ```python
    agent = create_agent(conn, user_id, AgentCreate(label="claude-code"))
    print(agent.agent_id)  # core_...
    ```
    """
    now = isodatetime.now()
    agent_id = uid.add_core_prefix(secret.generate_uuid())

    # Label uniqueness among agents (operator-facing; uuids are the real
    # ids). Check-then-insert has a theoretical TOCTOU window under
    # concurrent workers — acceptable at personal scale (RFC-010 Open
    # Question 1); a partial unique index is the eventual fix.
    collision = conn.execute(
        "SELECT 1 FROM entity WHERE type = 'Agent' AND json_extract(data, '$.label') = ?",
        (data.label,)
    ).fetchone()
    if collision:
        raise LockConflictError(
            "Agent label already in use",
            {"label": data.label}
        )

    entity_data = {
        "label": data.label,
        "owner": user_id,
    }
    if data.description is not None:
        entity_data["description"] = data.description

    entity_hash = hash_chain.compute_entity_hash(
        entity_type="Agent",
        created_at=now,
        updated_at=now,
        previous_hash=None  # Initial entity has no previous hash
    )
    conn.execute(
        """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
        VALUES (?, 'Agent', ?, ?, ?, ?, ?)""",
        (agent_id, entity_hash, 1, now, now, json.dumps(entity_data))
    )

    return AgentResponse(
        agent_id=agent_id,
        label=data.label,
        description=data.description,
        owner=user_id,
        created_at=isodatetime.to_datetime(now),
    )


def get_agent(conn: sqlite3.Connection, agent_id: str, user_id: str) -> AgentResponse:
    """
    Get an Agent entity by uuid (ownership-checked).

    Args:
        conn: Database connection
        agent_id: Agent entity uuid
        user_id: Owning user id

    Returns:
        AgentResponse

    Raises:
        ResourceNotFound: No Agent entity with that uuid
        ValidationError: Not an Agent or not owned by user_id
    """
    canonical = _validate_agent_for_user(conn, agent_id, user_id)
    row = conn.execute(
        "SELECT data, created_at FROM entity WHERE uuid = ?",
        (canonical,)
    ).fetchone()
    data = json.loads(row["data"])

    return AgentResponse(
        agent_id=canonical,
        label=data["label"],
        description=data.get("description"),
        owner=data["owner"],
        created_at=isodatetime.to_datetime(row["created_at"]),
    )


def list_agents(conn: sqlite3.Connection, user_id: str) -> list[AgentListResponse]:
    """
    List Agent entities owned by a user with their bound key prefixes.

    Management listing only — agents as semantic-layer data are readable
    through the Semantic API like any entity (RFC-010 §6.2).

    Args:
        conn: Database connection
        user_id: Owning user id

    Returns:
        List of AgentListResponse (with bound key prefixes/counts)
    """
    cursor = conn.execute(
        """SELECT e.uuid, e.data, e.created_at,
               (SELECT COUNT(*) FROM api_keys k
                WHERE k.agent_entity_id = e.uuid AND k.revoked_at IS NULL) AS bound_count,
               (SELECT GROUP_CONCAT(k.key_prefix) FROM api_keys k
                WHERE k.agent_entity_id = e.uuid AND k.revoked_at IS NULL) AS prefixes
        FROM entity e
        WHERE e.type = 'Agent' AND json_extract(e.data, '$.owner') = ?
        ORDER BY e.created_at""",
        (user_id,)
    )

    results = []
    for row in cursor.fetchall():
        data = json.loads(row["data"])
        results.append(AgentListResponse(
            agent_id=row["uuid"],
            label=data["label"],
            description=data.get("description"),
            owner=data["owner"],
            created_at=isodatetime.to_datetime(row["created_at"]),
            key_prefixes=(row["prefixes"].split(",") if row["prefixes"] else []),
            bound_key_count=row["bound_count"],
        ))

    return results


def bind_agent_to_key(
    conn: sqlite3.Connection,
    api_key_id: str,
    user_id: str,
    agent_id: str | None,
) -> APIKeyListResponse:
    """
    Bind, re-bind, or unbind an Agent entity on an existing API key.

    Explicit operator action (RFC-010): this is the migration path for
    keys that predate agent identity. Binding a revoked key is rejected;
    attribution history lives in Action facts, never rewritten.

    Args:
        conn: Database connection
        api_key_id: API key to update
        user_id: Owning user id (authorization)
        agent_id: Agent entity uuid to bind, or None to unbind

    Returns:
        Updated APIKeyListResponse

    Raises:
        ResourceNotFound: Key or agent not found
        ValidationError: Ownership/type violation
        LockConflictError: Key is revoked (409)
    """
    row = conn.execute(
        "SELECT revoked_at FROM api_keys WHERE id = ? AND user_id = ?",
        (api_key_id, user_id)
    ).fetchone()

    if row is None:
        raise ResourceNotFound(
            "API key not found",
            {"api_key_id": api_key_id}
        )
    if row["revoked_at"] is not None:
        raise LockConflictError(
            "Cannot bind a revoked API key",
            {"api_key_id": api_key_id}
        )

    agent_entity_id = None
    if agent_id is not None:
        agent_entity_id = _validate_agent_for_user(conn, agent_id, user_id)
        # RFC-011 §6: a bound key inherits the agent's grant envelope at
        # bind time — the union of grants held by the agent's other keys.
        _merge_agent_envelope(conn, api_key_id, agent_entity_id)

    conn.execute(
        "UPDATE api_keys SET agent_entity_id = ? WHERE id = ? AND user_id = ?",
        (agent_entity_id, api_key_id, user_id)
    )

    key = get_api_key_by_id(conn, api_key_id)
    assert key is not None  # row existence verified above
    return key


def set_key_capabilities(
    conn: sqlite3.Connection,
    api_key_id: str,
    user_id: str,
    capabilities: list[str] | None,
) -> APIKeyListResponse:
    """Replace an API key's capability set (E3 restricted key classes).

    Explicit operator action, same guard shape as bind_agent_to_key:
    ownership enforced, revoked keys refused. Empty list = unrestricted
    (legacy behavior); validation of capability ids happened in the
    request schema.

    Args:
        conn: Database connection
        api_key_id: API key to update
        user_id: Owning user id (authorization)
        capabilities: Replacement capability list (possibly empty)

    Returns:
        Updated APIKeyListResponse

    Raises:
        ResourceNotFound: Key not found (or not owned by user_id)
        LockConflictError: Key is revoked (409)
    """
    row = conn.execute(
        "SELECT revoked_at FROM api_keys WHERE id = ? AND user_id = ?",
        (api_key_id, user_id)
    ).fetchone()

    if row is None:
        raise ResourceNotFound(
            "API key not found",
            {"api_key_id": api_key_id}
        )
    if row["revoked_at"] is not None:
        raise LockConflictError(
            "Cannot update a revoked API key",
            {"api_key_id": api_key_id}
        )

    conn.execute(
        "UPDATE api_keys SET capabilities = ? WHERE id = ? AND user_id = ?",
        (json.dumps(capabilities or []), api_key_id, user_id)
    )

    key = get_api_key_by_id(conn, api_key_id)
    assert key is not None  # row existence verified above
    return key


# ============================================================================
# Scope Grants (RFC-011 §4.3, §6)
# ============================================================================


def _load_grants(conn: sqlite3.Connection, api_key_id: str) -> list[dict]:
    """Read a key's grants array (parsed)."""
    row = conn.execute(
        "SELECT grants FROM api_keys WHERE id = ?", (api_key_id,)
    ).fetchone()
    if row is None:
        raise ResourceNotFound("API key not found", {"api_key_id": api_key_id})
    return json.loads(row["grants"]) if row["grants"] else []


def _save_grants(conn: sqlite3.Connection, api_key_id: str, grants: list[dict]) -> None:
    """Persist a key's grants array."""
    conn.execute(
        "UPDATE api_keys SET grants = ? WHERE id = ?",
        (json.dumps(grants), api_key_id)
    )


def _agent_grant_envelope(
    conn: sqlite3.Connection, agent_id: str, exclude_key_id: str | None = None
) -> list[dict]:
    """Union of grants across the keys bound to an agent.

    The agent's grant envelope is DERIVED (RFC-011 §4.3) — enforcement
    reads key rows; the envelope is the union of what its keys hold, used
    for envelope inheritance at bind time and for introspection.
    agent_id may come prefixed or bare (canonicalized here).
    """
    rows = conn.execute(
        "SELECT grants FROM api_keys "
        "WHERE agent_entity_id = ? AND revoked_at IS NULL AND id != ?",
        (uid.add_core_prefix(agent_id), exclude_key_id or "")
    ).fetchall()
    envelope: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        for g in json.loads(row["grants"]) if row["grants"] else []:
            if g["scope_uuid"] not in seen:
                seen.add(g["scope_uuid"])
                envelope.append(g)
    return envelope


def _merge_agent_envelope(
    conn: sqlite3.Connection, api_key_id: str, agent_entity_id: str
) -> None:
    """RFC-011 §6: union the agent's envelope into a key's grants.

    Called when a key is created bound to an agent or (re-)bound to one —
    future bindings inherit the envelope at bind time. Existing grants on
    the key are preserved (union, deduped by scope).
    """
    envelope = _agent_grant_envelope(
        conn, agent_entity_id, exclude_key_id=api_key_id
    )
    if not envelope:
        return
    grants = _load_grants(conn, api_key_id)
    held = {g["scope_uuid"] for g in grants}
    merged = grants + [g for g in envelope if g["scope_uuid"] not in held]
    _save_grants(conn, api_key_id, merged)


def grant_scope_to_key(
    conn: sqlite3.Connection,
    user_id: str,
    api_key_id: str,
    scope_uuid: str,
    expires_at: str | None = None,
) -> dict:
    """Grant a scope to one API key (RFC-011 §6 POST /grants).

    Appends a grant entry ({grant_id, scope_uuid, granted_at, expires_at}).
    Idempotent per scope: re-granting a held scope refreshes nothing and
    returns the existing entry (revoke + re-grant to change expiry).

    Returns:
        The grant entry as stored.
    """
    row = conn.execute(
        "SELECT user_id FROM api_keys WHERE id = ?", (api_key_id,)
    ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise ResourceNotFound("API key not found", {"api_key_id": api_key_id})

    grants = _load_grants(conn, api_key_id)
    for g in grants:
        if g["scope_uuid"] == scope_uuid:
            return g

    entry = {
        "grant_id": uid.generate_uuid(),
        "scope_uuid": scope_uuid,
        "granted_at": isodatetime.now(),
        "expires_at": expires_at,
    }
    grants.append(entry)
    _save_grants(conn, api_key_id, grants)
    return entry


def grant_scope_to_agent(
    conn: sqlite3.Connection,
    user_id: str,
    agent_id: str,
    scope_uuid: str,
    expires_at: str | None = None,
) -> list[dict]:
    """Grant a scope to an agent — i.e. to ALL keys bound to it (RFC-011 §6).

    The operator thinks in agents; enforcement reads keys, so the grant is
    written onto every currently-bound key. The agent's envelope (union of
    its keys' grants) reflects the grant automatically. Future bindings
    inherit at bind time (see bind_agent_to_key).

    agent_id may come prefixed or bare; api_keys.agent_entity_id stores
    the canonical (prefixed) form — validated via RFC-010's helper.

    Returns:
        List of grant entries created (one per bound key; empty if the
        agent has no bound keys — the grant lands on the next binding).
    """
    canonical_agent_id = _validate_agent_for_user(conn, agent_id, user_id)

    rows = conn.execute(
        "SELECT id, grants FROM api_keys "
        "WHERE agent_entity_id = ? AND user_id = ? AND revoked_at IS NULL",
        (canonical_agent_id, user_id)
    ).fetchall()

    created: list[dict] = []
    for row in rows:
        grants = json.loads(row["grants"]) if row["grants"] else []
        if any(g["scope_uuid"] == scope_uuid for g in grants):
            continue
        entry = {
            "grant_id": uid.generate_uuid(),
            "scope_uuid": scope_uuid,
            "granted_at": isodatetime.now(),
            "expires_at": expires_at,
        }
        grants.append(entry)
        _save_grants(conn, row["id"], grants)
        created.append(entry)
    return created


def revoke_grant(conn: sqlite3.Connection, user_id: str, grant_id: str) -> dict:
    """Revoke a grant by id across the user's keys (RFC-011 §6 DELETE).

    Immediate for future operations: grants are re-read per request, so
    the next gated call fails. An in-flight session is not retroactively
    ejected (RFC-011 §7); revoke the key itself for immediate ejection.

    Returns:
        {"api_key_id": ..., "grant": removed_entry}

    Raises:
        ResourceNotFound: No grant with this id on any of the user's keys.
    """
    rows = conn.execute(
        "SELECT id, grants FROM api_keys WHERE user_id = ?", (user_id,)
    ).fetchall()
    for row in rows:
        grants = json.loads(row["grants"]) if row["grants"] else []
        kept = [g for g in grants if g.get("grant_id") != grant_id]
        if len(kept) != len(grants):
            removed = [g for g in grants if g.get("grant_id") == grant_id][0]
            _save_grants(conn, row["id"], kept)
            return {"api_key_id": row["id"], "grant": removed}
    raise ResourceNotFound("Grant not found", {"grant_id": grant_id})


def list_grants(
    conn: sqlite3.Connection,
    user_id: str,
    key_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict]:
    """List grants for a key or an agent (RFC-011 §6 GET /grants).

    Entries are flattened with their key attribution: each item is
    {grant_id, scope_uuid, granted_at, expires_at, key_id}. For an agent,
    the union over its bound keys (the envelope, attributed per key).
    agent_id may come prefixed or bare (canonicalized internally).
    """
    if key_id:
        row = conn.execute(
            "SELECT user_id FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise ResourceNotFound("API key not found", {"api_key_id": key_id})
        return [
            {**g, "key_id": key_id} for g in _load_grants(conn, key_id)
        ]

    if agent_id:
        canonical_agent_id = _validate_agent_for_user(conn, agent_id, user_id)
        rows = conn.execute(
            "SELECT id, grants FROM api_keys "
            "WHERE agent_entity_id = ? AND user_id = ? AND revoked_at IS NULL",
            (canonical_agent_id, user_id)
        ).fetchall()
        return [
            {**g, "key_id": row["id"]}
            for row in rows
            for g in (json.loads(row["grants"]) if row["grants"] else [])
        ]

    raise ValidationError("Specify key_id or agent_id")
