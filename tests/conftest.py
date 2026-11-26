"""
Pytest configuration and fixtures for memogarden-api tests.

This module provides fixtures for:
- Flask app with in-memory SQLite database
- Database initialization (Core and Soil)
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
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Set environment variables BEFORE importing anything from the app
os.environ["DATABASE_PATH"] = ":memory:"
os.environ["JWT_SECRET_KEY"] = "test-secret-key"
os.environ["BYPASS_LOCALHOST_CHECK"] = "true"
# Provider-run tests must not wake a real embed daemon installed at the
# default socket path (best-effort ping would cold-start torch for
# nothing) — point the runtime ping at a socket that does not exist;
# ping-specific tests and fake_daemon_factory override this per-test.
os.environ.setdefault(
    "MEMOGARDEN_EMBED_SOCK",
    str(Path(tempfile.gettempdir()) / "memogarden-tests-no-embed-daemon" / "embed.sock"),
)
# The api#91 index-lag signal reads the semantic index's state file. Set
# unconditionally (not setdefault): an exported MEMOGARDEN_SEARCH_CACHE
# from the developer's shell would point tests at the real index. No state
# file there = no lag reported; the lag tests point this at their tmp_path.
os.environ["MEMOGARDEN_SEARCH_CACHE"] = str(
    Path(tempfile.gettempdir()) / "memogarden-tests-no-search-cache")

# Import isodatetime for test fixtures (must be after env vars are set)
from utils import isodatetime  # noqa: E402

# ============================================================================
# Project Directory Guard
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def guard_project_dir():
    """Fail if tests pollute the project directory with temporary files.

    This fixture runs automatically at the start and end of each test session
    to detect if tests have created files in the project directory (where
    this conftest.py is located).

    Files matching known ignore patterns (.git, __pycache__, etc.) are excluded
    from the check.

    Raises:
        RuntimeError: If new files are detected in the project directory after
            the test session completes.

    See Also:
        TEST_DECISIONS.md - Rationale and documentation for this guard.
    """
    project_dir = Path(__file__).parent.parent

    # Patterns for files that are allowed to exist in project directory
    ignore_patterns = {
        ".git", ".gitignore", "pyproject.toml", "poetry.lock",
        "__pycache__", ".pytest_cache", ".coverage", "*.pyc",
        ".ruff_cache", ".venv", "venv", "node_modules",
        # Subdirectories that are part of the project
        "api", "system", "tests", "scripts", "docs", "plan",
        # Malformed SQLite URIs from test failures (when uri=True is missing)
        "file:*",
        # Database files (may be created during path resolution tests)
        "*.db",
        # Created by api.main._setup_logging at first app import
        "logs",
        # Logs directory contents
        "memogarden.log*",
    }

    def is_ignored(path: Path) -> bool:
        """Check if a path matches any of the ignore patterns."""
        # Check if path name matches any pattern
        for pattern in ignore_patterns:
            if path.match(pattern):
                return True
            # Check if parent directory name matches
            if path.parent.name == pattern.lstrip("*"):
                return True
        return False

    # Snapshot existing files in project directory (non-recursive)
    before = {
        f for f in project_dir.iterdir()
        if not is_ignored(f)
    }

    yield

    # Check for new files after all tests complete
    after = {
        f for f in project_dir.iterdir()
        if not is_ignored(f)
    }

    new_files = after - before

    if new_files:
        # Format list of new files for error message
        file_list = "\n  - " + "\n  - ".join(sorted(f.name for f in new_files))
        raise RuntimeError(
            f"Tests polluted project directory with {len(new_files)} file(s):{file_list}\n\n"
            f"Tests must not create files in the project directory. "
            f"Use temp directories (tempfile.mkdtemp) or in-memory databases instead."
        )


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
        db_path: Path to database file (use "file::memory:?mode=memory&cache=shared" for shared in-memory DB)

    Returns:
        SQLite connection with custom functions registered
    """
    conn = sqlite3.connect(db_path, uri=True, timeout=30)  # Increase timeout for locked databases
    conn.row_factory = sqlite3.Row
    # Foreign keys disabled during schema creation, enabled afterward
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")  # Wait up to 30 seconds for locks
    # Register sha256 function for migrations
    conn.create_function("sha256", 1, _sha256_hex)
    return conn


# Authentication tables (users and api_keys) — not in core.sql yet;
# will be migrated to the entity table in the future. Shared by the
# flask_app and db_conn fixtures.
_AUTH_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (id) REFERENCES entity(uuid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

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


# ============================================================================
# Flask App Fixture
# ============================================================================

@pytest.fixture(scope="function")
def flask_app():
    """
    Create a Flask app for testing.

    Each test gets a fresh temp-file SQLite database (WAL) — perfect
    isolation with production locking semantics. The database is
    initialized with the full schema on startup and removed on teardown.

    History: Session 5 moved this fixture from a shared temp file to a
    shared-cache in-memory database (file:...?mode=memory&cache=shared)
    to eliminate locking failures. That traded them for a worse failure
    mode: shared-cache SQLite coordinates with TABLE-level locks, so an
    unconsumed SELECT (fetchone() leaves the statement open) on one
    connection makes a concurrent writer on the same table fail
    immediately with SQLITE_LOCKED — bypassing busy_timeout under the
    documented shared-cache deadlock semantics. Production runs WAL on
    real files, where readers never block writers, so that interleaving
    does not exist there — exactly why the account run+poll tests failed
    intermittently in suites (api#75) but never in production.

    2026-11-20 (api#75): back to per-test temp files. Same isolation
    (unique directory per test), same connection-per-call lifecycle,
    production locking semantics, and the file — not a keeper connection —
    keeps the database alive.

    Returns:
        Flask app instance configured for testing
    """
    import shutil
    import tempfile

    test_dir = tempfile.mkdtemp(prefix="memogarden_flask_app_")
    test_db_path = os.path.join(test_dir, "memogarden_test.db")
    soil_db_path = os.path.join(test_dir, "memogarden_soil.db")

    from system.schemas import get_sql_schema

    # Initialize the core schema once; this bootstrap connection closes
    # immediately and the file keeps the database alive across connections.
    bootstrap = _create_sqlite_connection(test_db_path)
    bootstrap.executescript(get_sql_schema("core"))
    bootstrap.executescript(_AUTH_TABLES_SQL)
    bootstrap.commit()
    bootstrap.close()

    # Patch _create_connection to hand out connections to the test file —
    # the same connection-per-call lifecycle as production get_core().
    def _mock_create_connection():
        return _create_sqlite_connection(test_db_path)

    # Initialize Soil database for tests (same treatment: temp file + WAL)
    soil_bootstrap = _create_sqlite_connection(soil_db_path)
    soil_bootstrap.executescript(get_sql_schema("soil"))
    soil_bootstrap.commit()
    soil_bootstrap.close()

    # Save original Soil.__init__ before patching
    from system.soil.database import Soil
    original_soil_init = Soil.__init__

    # Create a custom Soil.__init__ that uses our test database
    # pyright: ignore[reportIncompatibleVariableOverride]
    def _mock_soil_init(self, db_path: str | Path = "soil.db"):
        # Always use this test's soil database file, ignore the passed db_path
        original_soil_init(self, soil_db_path)

    with patch('system.core._create_connection', _mock_create_connection):
        # Patch Soil.__init__ to use test database
        with patch.object(Soil, '__init__', _mock_soil_init):
            from api.main import create_app

            app = create_app(test_config={"TESTING": True})

            yield app

    shutil.rmtree(test_dir, ignore_errors=True)


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

    # Load schema via the system resolver (matches production schema)
    from system.schemas import get_sql_schema

    final_schema_sql = get_sql_schema("core")
    conn.executescript(final_schema_sql)

    # Add authentication tables (users and api_keys) which are not in core.sql yet
    # These will be migrated to the entity table in the future
    conn.executescript(_AUTH_TABLES_SQL)
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

# Override bcrypt work factor for faster tests (4 vs 12 = ~16x faster)
# This reduces per-test auth fixture time from ~300ms to ~20ms
settings.bcrypt_work_factor = 4


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

    from api.middleware.service import hash_password

    user_id = str(uuid.uuid4())
    username = "testuser"
    password = "TestPass123"
    password_hash = hash_password(password)  # Use default work factor

    now = isodatetime.now()

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

    from api.middleware.service import hash_password
    from system.core import _create_connection
    from utils import hash_chain

    user_id = str(uuid.uuid4())
    username = "testuser"
    password = "TestPass123"
    password_hash = hash_password(password)  # Use default work factor

    now = isodatetime.now()

    # Get the Flask app's connection
    conn = _create_connection()

    try:
        # Create entity for user with proper hash
        entity_hash = hash_chain.compute_entity_hash(
            entity_type="User",
            created_at=now,  # isodatetime.now() already returns ISO string
            updated_at=now,
            previous_hash=None
        )
        conn.execute(
            """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, "User", entity_hash, 1, now, now, json.dumps({}))
        )

        # Create user (id references entity.uuid)
        conn.execute(
            """INSERT INTO users (id, username, password_hash, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, password_hash, True, now)
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

    from utils import isodatetime

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

    from api.middleware.api_keys import get_api_key_prefix, hash_api_key
    from system.core import _create_connection
    from utils import hash_chain, secret

    # Generate API key
    api_key_id = str(uuid.uuid4())
    raw_key = secret.generate_api_key()
    key_hash = hash_api_key(raw_key)
    key_prefix = get_api_key_prefix(raw_key)

    now = isodatetime.now()

    # Get the Flask app's connection
    conn = _create_connection()

    try:
        # Create entity for API key with proper hash
        entity_hash = hash_chain.compute_entity_hash(
            entity_type="ApiKey",
            created_at=now,  # isodatetime.now() already returns ISO string
            updated_at=now,
            previous_hash=None
        )
        conn.execute(
            """INSERT INTO entity (uuid, type, hash, version, created_at, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (api_key_id, "ApiKey", entity_hash, 1, now, now, json.dumps({}))
        )

        # Create API key (id references entity.uuid)
        conn.execute(
            """INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (api_key_id, test_user_app["id"], "test-key", key_hash, key_prefix, now)
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


# ============================================================================
# Core Database Fixture
# ============================================================================

@pytest.fixture
def core(flask_app):
    """
    Get a Core instance for testing database operations directly.

    This fixture provides direct access to the same database used by the Flask app,
    ensuring integration between unit and integration tests.

    Session 5 Fix: Uses named in-memory database with shared cache.
    Each fixture gets its own connection, but all connections see the same database.

    Args:
        flask_app: Flask app fixture (ensures database is initialized)

    Returns:
        Core instance with autocommit semantics
    """
    from system.core import Core, _create_connection

    # Get a connection to the shared in-memory database
    conn = _create_connection()

    try:
        # Create Core instance and mark it as in context for testing
        core_instance = Core(conn)
        core_instance._in_context = True  # Mark as already in context
        yield core_instance
    finally:
        # Explicitly close the connection to prevent database lock issues
        conn.close()


@pytest.fixture
def sample_entity(core):
    """
    Create a sample entity for testing relations.

    Args:
        core: Core fixture

    Returns:
        UUID of created entity
    """
    entity_uuid = core.entity.create("Artifact")
    return entity_uuid


# ---------------------------------------------------------------------------
# api#95 Phase 2: fake embed daemon (no torch) for semantic/hybrid tests
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402
import socket as _socket  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


@pytest.fixture
def fake_daemon_factory(monkeypatch, tmp_path):
    """Factory for a minimal embed-daemon protocol server.

    Speaks just enough of the triage-model daemon protocol ({"op":
    "search"} -> {"results": [...]}) over a unix socket, points the
    search_daemon client env at it, and never spawns real torch. The
    factory takes the results list to serve; responses are stateless.
    `rerank_results` (optional) answers the rerank op — pass [] or a
    pair list to exercise rerank composition; None makes rerank requests
    fall back to the search payload (pre-rerank daemon behavior). Every
    request is recorded in `fake_daemon_factory.seen_requests` so tests
    can assert what the api actually sent (e.g. boundary-filtered uuids
    never reaching the model layer).
    """
    started = []

    def _factory(results, rerank_results=None):
        sock_path = tmp_path / f"fake-embed-{len(started)}.sock"
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(4)
        seen = []
        _factory.seen_requests = seen

        def _serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                # the real daemon does ONE recv (payloads fit 4MB) and
                # responds without waiting for EOF — recv-until-EOF would
                # deadlock against the client's open read
                req = _json.loads(conn.recv(1 << 22).decode())
                seen.append(req)
                if req.get("op") == "rerank" and rerank_results is not None:
                    out = {"results": rerank_results}
                else:
                    out = {"results": results}
                conn.sendall(_json.dumps(out).encode())
                conn.close()

        t = _threading.Thread(target=_serve, daemon=True)
        t.start()
        started.append(srv)
        monkeypatch.setenv("MEMOGARDEN_EMBED_SOCK", str(sock_path))
        monkeypatch.setenv("MEMOGARDEN_TRIAGE_MODEL_DIR", str(tmp_path))
        return sock_path

    yield _factory
    for s in started:
        s.close()


class RecordingPingServer:
    """Minimal embed-daemon stand-in that records request payloads.

    Companion to fake_daemon_factory for the embed-on-ingest ping: it
    records raw request bytes (the ping contract is the payload, not the
    reply), answers {"ok": true} like the real daemon does for a ping,
    and closes — the client drains one recv and moves on.
    """

    def __init__(self, sock_path):
        self.payloads = []
        self._lock = _threading.Lock()
        self._srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(4)
        self._thread = _threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            try:
                req = conn.recv(1 << 20)
                with self._lock:
                    self.payloads.append(req)
                conn.sendall(_json.dumps({"ok": True}).encode())
            finally:
                conn.close()

    def wait_for_ping(self, timeout=5.0):
        """Block until a request arrives (poll — the ping fires on the
        run thread just after finish_run, later than the run manifest
        flips to a terminal status)."""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            with self._lock:
                if self.payloads:
                    return self.payloads[0]
            _time.sleep(0.02)
        raise AssertionError(f"no ping arrived within {timeout}s")

    def close(self):
        self._srv.close()


@pytest.fixture
def ping_server(tmp_path, monkeypatch):
    """RecordingPingServer wired to MEMOGARDEN_EMBED_SOCK."""
    import uuid

    sock_path = tmp_path / f"ping-{uuid.uuid4().hex[:8]}.sock"
    monkeypatch.setenv("MEMOGARDEN_EMBED_SOCK", str(sock_path))
    server = RecordingPingServer(sock_path)
    yield server
    server.close()
