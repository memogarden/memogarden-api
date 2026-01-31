"""Authentication service for user operations.

This module provides user authentication operations:
- Password hashing and verification using bcrypt
- User creation and retrieval
- User authentication and credential verification

Confines bcrypt dependency to this module only - all password operations
go through this service's public API.
"""

import sqlite3

import bcrypt

from .schemas import UserCreate, UserResponse
from ..config import settings
from system.utils import isodatetime, uid

# ============================================================================
# Password Hashing and Verification
# ============================================================================


def _get_bcrypt_work_factor() -> int:
    """Get bcrypt work factor from config.

    Reads from settings each time to allow test fixture to override.
    """
    return settings.bcrypt_work_factor


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Uses a work factor of 12 for good security/performance balance.
    The hash includes a random salt automatically.

    Args:
        password: Plain text password

    Returns:
        Bcrypt hash as a string (includes salt and algorithm info)

    Example:
    ```python
    hashed = hash_password("SecurePass123")
    # Returns: "$2b$12$..." (60 character hash)
    ```
    """
    password_bytes = password.encode("utf-8")
    work_factor = _get_bcrypt_work_factor()
    salt = bcrypt.gensalt(rounds=work_factor)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against a bcrypt hash.

    Args:
        password: Plain text password to verify
        password_hash: Bcrypt hash to compare against

    Returns:
        True if password matches hash, False otherwise

    Example:
    ```python
    if verify_password("user_input", stored_hash):
        print("Password correct")
    ```
    """
    password_bytes = password.encode("utf-8")
    hash_bytes = password_hash.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hash_bytes)


# ============================================================================
# User CRUD Operations
# ============================================================================


def create_user(conn: sqlite3.Connection, data: UserCreate, is_admin: bool = True) -> UserResponse:
    """
    Create a new user in the database.

    Args:
        conn: Database connection
        data: User creation data (username, password)
        is_admin: Admin status (default True for MVP - only admin registration)

    Returns:
        Created user response with id, username, is_admin, created_at

    Raises:
        sqlite3.IntegrityError: Username already exists

    Example:
    ```python
    user_data = UserCreate(username="admin", password="SecurePass123")
    user = create_user(conn, user_data, is_admin=True)
    print(f"Created user: {user.id}")
    ```
    """
    user_id = uid.generate_uuid()
    now = isodatetime.now()
    password_hash = hash_password(data.password)

    # Create entity registry entry first (same ID as user)
    conn.execute(
        """INSERT INTO entity (id, type, created_at, updated_at)
        VALUES (?, 'users', ?, ?)""",
        (user_id, now, now)
    )

    # Create user entity (foreign key to entity)
    conn.execute(
        """INSERT INTO users (id, username, password_hash, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (user_id, data.username, password_hash, 1 if is_admin else 0, now)
    )

    return UserResponse(
        id=user_id,
        username=data.username,
        is_admin=is_admin,
        created_at=isodatetime.to_datetime(now)
    )


def get_user_by_username(conn: sqlite3.Connection, username: str) -> UserResponse | None:
    """
    Get a user by username (case-insensitive).

    Args:
        conn: Database connection
        username: Username to look up (case-insensitive)

    Returns:
        User response if found, None otherwise

    Example:
    ```python
    user = get_user_by_username(conn, "admin")
    if user:
        print(f"Found user: {user.id}")
    ```
    """
    cursor = conn.execute(
        """SELECT id, username, is_admin, created_at
        FROM users
        WHERE username = ? COLLATE NOCASE""",
        (username,)
    )
    row = cursor.fetchone()

    if row is None:
        return None

    return UserResponse(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        created_at=isodatetime.to_datetime(row["created_at"])
    )


def get_user_with_password(conn: sqlite3.Connection, username: str) -> tuple[UserResponse, str] | None:
    """
    Get a user with their password hash (for authentication).

    Returns both the user response and password hash for credential verification.
    This should only be used internally for authentication - never expose password hash.

    Args:
        conn: Database connection
        username: Username to look up (case-insensitive)

    Returns:
        Tuple of (UserResponse, password_hash) if found, None otherwise

    Example:
    ```python
    result = get_user_with_password(conn, "admin")
    if result:
        user, password_hash = result
        if verify_password(input_password, password_hash):
            print("Authentication successful")
    ```
    """
    cursor = conn.execute(
        """SELECT id, username, password_hash, is_admin, created_at
        FROM users
        WHERE username = ? COLLATE NOCASE""",
        (username,)
    )
    row = cursor.fetchone()

    if row is None:
        return None

    user = UserResponse(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        created_at=isodatetime.to_datetime(row["created_at"])
    )

    return (user, row["password_hash"])


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> UserResponse | None:
    """
    Get a user by ID.

    Args:
        conn: Database connection
        user_id: User UUID to look up

    Returns:
        User response if found, None otherwise

    Example:
    ```python
    user = get_user_by_id(conn, "550e8400-e29b-41d4-a716-446655440000")
    if user:
        print(f"Found user: {user.username}")
    ```
    """
    cursor = conn.execute(
        """SELECT id, username, is_admin, created_at
        FROM users
        WHERE id = ?""",
        (user_id,)
    )
    row = cursor.fetchone()

    if row is None:
        return None

    return UserResponse(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        created_at=isodatetime.to_datetime(row["created_at"])
    )


def count_users(conn: sqlite3.Connection) -> int:
    """
    Count total number of users in the database.

    Args:
        conn: Database connection

    Returns:
        Number of users

    Example:
    ```python
    user_count = count_users(conn)
    if user_count == 0:
        print("No users exist - admin registration available")
    ```
    """
    cursor = conn.execute("SELECT COUNT(*) as count FROM users")
    row = cursor.fetchone()
    return row["count"]


def has_admin_user(conn: sqlite3.Connection) -> bool:
    """
    Check if any admin users exist in the database.

    Args:
        conn: Database connection

    Returns:
        True if at least one admin user exists, False otherwise

    Example:
    ```python
    if not has_admin_user(conn):
        print("No admin user - registration required")
    ```
    """
    cursor = conn.execute("SELECT COUNT(*) as count FROM users WHERE is_admin = 1")
    row = cursor.fetchone()
    return row["count"] > 0


# ============================================================================
# Authentication Verification
# ============================================================================


def verify_credentials(conn: sqlite3.Connection, username: str, password: str) -> UserResponse | None:
    """
    Verify user credentials and return user if valid.

    Args:
        conn: Database connection
        username: Username to verify
        password: Password to verify

    Returns:
        User response if credentials are valid, None otherwise

    Example:
    ```python
    user = verify_credentials(conn, "admin", "SecurePass123")
    if user:
        print(f"Login successful for {user.username}")
    else:
        print("Invalid credentials")
    ```
    """
    result = get_user_with_password(conn, username)
    if result is None:
        return None

    user, password_hash = result
    if verify_password(password, password_hash):
        return user

    return None
