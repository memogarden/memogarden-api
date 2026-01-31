"""API key service for programmatic authentication.

This module provides API key operations:
- API key CRUD operations
- API key verification for authentication

Confines bcrypt dependency for API key hashing to this module only.
"""

import sqlite3
from datetime import datetime

from .schemas import APIKeyCreate, APIKeyListResponse, APIKeyResponse
from system.utils import isodatetime, secret
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

    # Create entity registry entry first (same ID as API key)
    conn.execute(
        """INSERT INTO entity (id, type, created_at, updated_at)
        VALUES (?, 'api_keys', ?, ?)""",
        (api_key_id, now, now)
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
        """INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (api_key_id, user_id, data.name, key_hash, key_prefix, expires_at_str, now)
    )

    return APIKeyResponse(
        id=api_key_id,
        name=data.name,
        key=plain_key,  # Full key only shown on creation
        prefix=key_prefix,
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
        """SELECT id, name, key_prefix, expires_at, created_at, last_seen, revoked_at
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
        """SELECT id, name, key_prefix, expires_at, created_at, last_seen, revoked_at
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


def verify_api_key_and_get_user(conn: sqlite3.Connection, plain_key: str) -> tuple[str, str] | None:
    """
    Verify an API key and return the user ID and API key ID.

    Used by authentication middleware to authenticate API key requests.

    Args:
        conn: Database connection
        plain_key: Plain text API key from Authorization header

    Returns:
        Tuple of (user_id, api_key_id) if valid, None otherwise

    Example:
    ```python
    result = verify_api_key_and_get_user(conn, plain_key)
    if result:
        user_id, api_key_id = result
        print(f"Authenticated as user {user_id} with API key {api_key_id}")
    ```
    """
    # Get all active API keys and check each one
    # (We can't hash the input key and look it up because bcrypt has random salts)
    cursor = conn.execute(
        """SELECT id, user_id, key_hash
        FROM api_keys
        WHERE revoked_at IS NULL"""
    )

    for row in cursor.fetchall():
        if verify_api_key(plain_key, row["key_hash"]):
            # Update last_seen timestamp
            update_last_seen(conn, row["id"])
            return (row["user_id"], row["id"])

    return None
