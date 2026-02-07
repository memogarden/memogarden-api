"""
Pytest configuration and fixtures for memogarden-api tests.

This module provides fixtures for:
- Flask app with in-memory SQLite database
- Database initialization
- User authentication (JWT tokens and API keys)
- Test client with proper authentication headers

Testing Approach:
- No mocking: Tests use real memogarden-system code
- In-memory database: Each test gets a fresh :memory: database
- Integration testing: Tests the full API stack
"""

import hashlib
import os
import sqlite3
from typing import Any
from unittest.mock import patch

import pytest

# Set environment variables BEFORE importing anything from the app
os.environ["DATABASE_PATH"] = ":memory:"
os.environ["JWT_SECRET_KEY"] = "test-secret-key"
os.environ["BYPASS_LOCALHOST_CHECK"] = "true"


# ============================================================================
# SQLite Extension Functions
# ============================================================================

def _sha256_hex(data: Any) -> str:
    """SHA256 hash function for SQLite."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _create_sqlite_connection(db_path: str) -> sqlite3.Connection:
    """
    Create a SQLite connection with custom functions.

    Registers the sha256 function needed by migrations.

    Args:
        db_path: Path to database file

    Returns:
        SQLite connection with custom functions registered
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Foreign keys disabled during schema creation, enabled afterward
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    # Register sha256 function for migrations
    conn.create_function("sha256", 1, _sha256_hex)
    return conn


# ============================================================================
# Flask App Fixture
# ============================================================================

@pytest.fixture
def flask_app():
    """
    Create a Flask app for testing.

    The app uses an in-memory SQLite database for isolation.
    The database is initialized with the full schema on startup.

    Returns:
        Flask app instance configured for testing
    """
    # Track temp DB path for cleanup
    temp_db_path = None
    _schema_initialized = False

    # Now import and configure the app
    # Patch _create_connection to use our test database
    def _mock_create_connection():
        """Mock that returns our test database connection."""
        nonlocal temp_db_path, _schema_initialized

        # Create database if it doesn't exist
        if temp_db_path is None:
            import tempfile
            fd, temp_db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)

        # Create database connection with sha256 support
        conn = _create_sqlite_connection(temp_db_path)

        # Initialize schema on first connection
        if not _schema_initialized:
            # Load schema from the memogarden-system core.sql file
            # This ensures tests always match production schema
            from pathlib import Path

            # Find the core.sql file in memogarden-system
            # Try relative path from tests directory
            tests_dir = Path(__file__).parent
            project_root = tests_dir.parent.parent
            schema_path = project_root / "memogarden-system" / "system" / "schemas" / "sql" / "core.sql"

            if not schema_path.exists():
                raise FileNotFoundError(
                    f"Schema file not found at {schema_path}. "
                    f"Ensure memogarden-system repository is available."
                )

            final_schema_sql = schema_path.read_text()
            conn.executescript(final_schema_sql)

            # Add authentication tables (users and api_keys) which are not in core.sql yet
            auth_tables_sql = """
            -- Users table for authentication
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,

                FOREIGN KEY (id) REFERENCES entity(uuid) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

            -- API Keys table for authentication
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT,
                revoked_at TEXT,

                FOREIGN KEY (id) REFERENCES entity(uuid) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
            CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at) WHERE revoked_at IS NULL;
            """
            conn.executescript(auth_tables_sql)
            conn.commit()
            _schema_initialized = True
        return conn  # Return connection, but don't cache it

    # Patch _create_connection BEFORE importing the app
    with patch('system.core._create_connection', _mock_create_connection):
        from api.main import app

        app.config["TESTING"] = True

        # Skip initialize_database() in tests since the fixture already sets up the schema
        # The initialize_database() function uses sqlite3.connect() directly which
        # bypasses our mocked connection

        yield app

        # Cleanup temp DB
        if temp_db_path:
            try:
                os.unlink(temp_db_path)
            except OSError:
                pass  # Ignore cleanup failures in tests


@pytest.fixture
def client(flask_app):
    """
    Create a Flask test client.

    The test client can make requests to the app without running a server.

    Returns:
        Flask test client
    """
    return flask_app.test_client()


# ============================================================================
# Direct Database Access Fixture
# ============================================================================

@pytest.fixture
def db_conn():
    """
    Create a fresh database connection for direct database access.

    Useful for setting up test data or verifying database state.

    Returns:
        SQLite connection with row_factory set to sqlite3.Row
    """
    # Use temp file database for this fixture
    import tempfile
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = _create_sqlite_connection(db_path)

    # Load schema from the memogarden-system core.sql file
    # This ensures tests always match production schema
    from pathlib import Path

    # Find the core.sql file in memogarden-system
    tests_dir = Path(__file__).parent
    project_root = tests_dir.parent.parent
    schema_path = project_root / "memogarden-system" / "system" / "schemas" / "sql" / "core.sql"

    if not schema_path.exists():
        raise FileNotFoundError(
            f"Schema file not found at {schema_path}. "
            f"Ensure memogarden-system repository is available."
        )

    final_schema_sql = schema_path.read_text()
    conn.executescript(final_schema_sql)

    # Add authentication tables (users and api_keys) which are not in core.sql yet
    # These will be migrated to the entity table in the future
    auth_tables_sql = """
    -- Users table for authentication
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,

        FOREIGN KEY (id) REFERENCES entity(uuid) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

    -- API Keys table for authentication
    CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        last_seen TEXT,
        revoked_at TEXT,

        FOREIGN KEY (id) REFERENCES entity(uuid) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
    CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at) WHERE revoked_at IS NULL;
    """
    conn.executescript(auth_tables_sql)
    conn.commit()

    temp_db_path = db_path  # Store for cleanup
    yield conn

    conn.close()
    try:
        os.unlink(temp_db_path)
    except OSError:
        pass  # Ignore cleanup failures in tests


# ============================================================================
# Authentication Fixtures
# ============================================================================

from api.config import settings  # noqa: E402 (must import after db fixtures)


@pytest.fixture
def test_user(db_conn):
    """
    Create a test user in the database.

    Creates a user with username "testuser" and is_admin=True.
    Returns the user data including the generated password.

    Returns:
        dict with user data: id, username, password (plaintext), is_admin, created_at
    """
    import json
    import uuid
    from datetime import datetime

    from api.middleware.service import hash_password

    user_id = str(uuid.uuid4())
    username = "testuser"
    password = "TestPass123"
    password_hash = hash_password(password)  # Use default work factor

    now = datetime.utcnow()

    # Create entity for user (links to users table)
    # Include empty JSON object for data field (required by new schema)
    db_conn.execute(
        """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, "User", "test_hash", 1, now, now, json.dumps({}))
    )

    # Create user (id references entity.uuid)
    db_conn.execute(
        """INSERT INTO users (id, username, password_hash, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, password_hash, True, now)
    )
    db_conn.commit()

    return {
        "id": user_id,
        "username": username,
        "password": password,
        "is_admin": True,
        "created_at": now,
    }


@pytest.fixture
def test_user_app(flask_app):
    """
    Create a test user in the Flask app's database.

    This fixture uses the same database connection as the Flask app,
    ensuring that test data is visible to API endpoints.

    Returns:
        dict with user data: id, username, password (plaintext), is_admin, created_at
    """
    import json
    import uuid
    from datetime import datetime

    from api.middleware.service import hash_password
    from system.core import _create_connection
    from system.utils import hash_chain

    user_id = str(uuid.uuid4())
    username = "testuser"
    password = "TestPass123"
    password_hash = hash_password(password)  # Use default work factor

    now = datetime.utcnow()

    # Get the Flask app's connection
    conn = _create_connection()

    try:
        # Create entity for user with proper hash
        entity_hash = hash_chain.compute_entity_hash(
            entity_type="User",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            previous_hash=None
        )
        conn.execute(
            """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, "User", entity_hash, 1, now.isoformat(), now.isoformat(), json.dumps({}))
        )

        # Create user (id references entity.uuid)
        conn.execute(
            """INSERT INTO users (id, username, password_hash, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, password_hash, True, now.isoformat())
        )
        conn.commit()

        return {
            "id": user_id,
            "username": username,
            "password": password,
            "is_admin": True,
            "created_at": now,
        }
    finally:
        conn.close()


def _create_jwt_token(user_id: str, username: str, is_admin: bool = True) -> str:
    """Helper function to create a JWT token for testing."""
    import jwt

    from system.utils import isodatetime

    now_ts = isodatetime.now_unix()
    expiry_ts = now_ts + (30 * 24 * 60 * 60)  # 30 days

    payload = {
        "sub": user_id,
        "username": username,
        "is_admin": is_admin,
        "iat": now_ts,
        "exp": expiry_ts,
    }

    token = jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")
    return token


@pytest.fixture
def auth_headers(test_user_app):
    """
    Create authentication headers for API requests.

    Returns headers with JWT token for the test_user.

    Returns:
        dict with Authorization header for JWT authentication
    """
    token = _create_jwt_token(test_user_app["id"], test_user_app["username"], test_user_app["is_admin"])

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def auth_headers_apikey(test_user_app):
    """
    Create authentication headers with API key for API requests.

    Creates an API key for the test_user and returns headers with the API key.

    Returns:
        dict with X-API-Key header for API key authentication
    """
    import json
    import uuid
    from datetime import datetime

    from api.middleware.api_keys import get_api_key_prefix, hash_api_key
    from system.core import _create_connection
    from system.utils import hash_chain, secret

    # Generate API key
    api_key_id = str(uuid.uuid4())
    raw_key = secret.generate_api_key()
    key_hash = hash_api_key(raw_key)
    key_prefix = get_api_key_prefix(raw_key)

    now = datetime.utcnow()

    # Get the Flask app's connection
    conn = _create_connection()

    try:
        # Create entity for API key with proper hash
        entity_hash = hash_chain.compute_entity_hash(
            entity_type="ApiKey",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            previous_hash=None
        )
        conn.execute(
            """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (api_key_id, "ApiKey", entity_hash, 1, now.isoformat(), now.isoformat(), json.dumps({}))
        )

        # Create API key (id references entity.uuid)
        conn.execute(
            """INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (api_key_id, test_user_app["id"], "test-key", key_hash, key_prefix, now.isoformat())
        )
        conn.commit()

        return {
            "X-API-Key": raw_key,
            "Content-Type": "application/json",
        }
    finally:
        conn.close()


# ============================================================================
# Test Data Helpers
# ============================================================================

@pytest.fixture
def sample_transaction_data():
    """
    Sample transaction data for testing.

    Returns:
        dict with valid transaction create request data
    """

    return {
        "amount": -15.50,
        "currency": "SGD",
        "transaction_date": "2025-12-23",
        "description": "Coffee at Starbucks",
        "account": "Personal",
        "category": "Food",
        "notes": "Morning coffee with colleague"
    }


@pytest.fixture
def sample_recurrence_data():
    """
    Sample recurrence data for testing.

    Returns:
        dict with valid recurrence create request data
    """
    import json

    return {
        "rrule": "FREQ=MONTHLY;BYDAY=2FR",
        "entities": json.dumps([
            {
                "amount": -1500,
                "currency": "SGD",
                "description": "Rent",
                "account": "Household",
                "category": "Housing"
            }
        ]),
        "valid_from": "2025-01-01T00:00:00Z",
        "valid_until": None
    }
