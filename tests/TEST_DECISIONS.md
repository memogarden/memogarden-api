# Test Decisions and Rationale

This document records architectural and procedural decisions about testing in MemoGarden API.
When making changes to test approach or fixtures, update this file to document the rationale.

---

## Table of Contents

1. [Testing Philosophy](#testing-philosophy)
2. [Project Directory Guard](#project-directory-guard)
3. [Database Testing Strategy](#database-testing-strategy)
4. [Test Isolation](#test-isolation)
5. [Fixtures and Scopes](#fixtures-and-scopes)
6. [Authentication Testing](#authentication-testing)

---

## Testing Philosophy

### Core Principles

1. **No Mocking** - Tests use real database and system code
   - Rationale: Mocks can hide integration bugs and require maintenance
   - Exception: Only unittest.mock.patch for test fixture setup (patching constructors, not behavior)

2. **Integration Testing** - Tests verify the full API stack
   - Rationale: Catches issues at layer boundaries (API → Core → Database)
   - Trade-off: Slower than unit tests, but higher confidence

3. **In-Memory Databases** - Tests use `:memory:` or shared in-memory databases
   - Rationale: Fast execution, automatic cleanup, perfect isolation
   - Trade-off: Doesn't catch file-permissions issues (acceptable for API tests)

4. **Determinism** - Tests should produce same results on every run
   - Rationale: Flaky tests waste time and erode trust
   - Implementation: UUID-based database names, transaction rollbacks

---

## Project Directory Guard

### Decision

Tests must not create files in the project directory. The `guard_project_dir` fixture
enforces this by failing the test suite if new files are detected.

### Implementation

Located in `conftest.py`, the `guard_project_dir` fixture:
- Runs automatically at session start/end (autouse=True)
- Snapshots project directory before tests run
- Checks for new files after tests complete
- Ignores known patterns (.git, __pycache__, etc.)
- Raises RuntimeError if pollution detected

### Rationale

**Problem**: Test failures can leave orphaned files in the project directory.
- Example: SQLite URI bug created files like `file:memogarden_soil_*.db`
- These files clutter git status and risk accidental commits
- Manual cleanup is tedious and error-prone

**Solution**: Fail fast when pollution is detected.
- Forces developers to fix the root cause (not just clean up symptoms)
- Prevents accumulation of test artifacts over time
- Acts as continuous enforcement of testing best practices

### Allowed Patterns

The following file patterns are ignored by the guard:
- Version control: `.git`, `.gitignore`
- Python artifacts: `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`
- Dependencies: `node_modules`, `.venv`, `venv`
- Project files: `pyproject.toml`, `poetry.lock`
- Project directories: `api/`, `system/`, `tests/`, `scripts/`, `docs/`, `plan/`
- Temporary databases: `*.db`, `*.db-shm`, `*.db-wal` (in case of bugs)

### Adding New Ignore Patterns

If tests legitimately need to create files in the project directory (rare!):
1. Consider if a temp directory would be better (likely yes)
2. If truly necessary, add pattern to `ignore_patterns` in `guard_project_dir`
3. Document why the exception is needed in this section

---

## Database Testing Strategy

### Core Database (MemoGarden Core API)

**Approach**: Named in-memory databases with shared cache

```python
# Each test gets unique database name
_test_db_name = f"file:memogarden_test_{uuid.uuid4()}?mode=memory&cache=shared"

# Shared cache allows multiple connections to same in-memory database
conn = sqlite3.connect(_test_db_name, uri=True)
```

**Rationale**:
- **Shared cache**: Flask app and test code can use separate connections
- **Named database**: Database persists across connections (unlike `:memory:` alone)
- **Unique per test**: Perfect isolation between tests
- **In-memory**: Fast, automatic cleanup

### Soil Database (Immutable Facts)

**Approach**: Same shared in-memory pattern as Core database

```python
soil_db_name = f"file:memogarden_soil_{uuid.uuid4()}?mode=memory&cache=shared"
soil_conn = _create_sqlite_connection(soil_db_name)
```

**Rationale**:
- Soil needs schema initialization (soil.sql)
- Tests need to query items created by API handlers
- Shared cache enables cross-connection queries

### URI Path Handling

**Critical**: SQLite URIs require `uri=True` parameter:

```python
# CORRECT - enables URI syntax
conn = sqlite3.connect("file:dbname?mode=memory&cache=shared", uri=True)

# WRONG - treats URI as filename, creates malformed files
conn = sqlite3.connect("file:dbname?mode=memory&cache=shared")
```

**Bug History**: Prior to fix, Soil class didn't pass `uri=True`, causing:
- Malformed files: `file:memogarden_soil_*.db` created in project directory
- Test failures: `sqlite3.OperationalError: no such table: item`

**Lesson**: When using SQLite URIs, always verify `uri=True` is set.

---

## Test Isolation

### Goal

Each test should be independent:
- Can run in any order
- Doesn't depend on other tests
- Doesn't affect other tests

### Implementation

1. **Function-scoped fixtures** - Most fixtures are `scope="function"`
   - Fresh database for each test
   - Fresh Flask app for each test

2. **Transaction rollback** - Tests don't commit to database
   - Core operations use transactions
   - Test failures trigger rollback

3. **Unique names** - Random UUIDs prevent conflicts
   - Database names include UUIDs
   - Test entities use generated UUIDs

### Exceptions

- **Session-scoped fixtures** - Used for expensive setup:
  - `guard_project_dir` - One check per test session
  - Future: Test temp directory creation

---

## Fixtures and Scopes

### Fixture Scope Guide

| Scope | Use Case | Example |
|-------|----------|---------|
| `function` | Per-test isolation (default) | `flask_app`, `client`, `db_conn` |
| `session` | Expensive one-time setup | `guard_project_dir` |
| `module` | Shared state within module | (rarely used) |
| `class` | Shared state within test class | (rarely used) |

### Key Fixtures

#### `flask_app` (function-scoped)
Creates Flask app with fresh database.
- Initializes schema (core.sql + auth tables)
- Patches `Soil.__init__` to use test database
- Returns app instance

#### `client` (function-scoped)
Flask test client for making HTTP requests.
- Wraps `flask_app`
- Used by most API tests

#### `auth_headers` (function-scoped)
JWT authentication headers for API requests.
- Creates test user
- Generates JWT token
- Returns `{"Authorization": "Bearer <token>"}`

#### `db_conn` (function-scoped)
Direct database connection for setup/verification.
- Uses temp file database (not in-memory)
- Useful for creating test data without going through API
- **Note**: Don't use for tests that need database isolation

---

## Authentication Testing

### JWT Tokens

**Approach**: Generate valid JWT tokens for test users

```python
@pytest.fixture
def auth_headers(test_user_app):
    token = _create_jwt_token(
        user_id=test_user_app["id"],
        username=test_user_app["username"],
        is_admin=True
    )
    return {"Authorization": f"Bearer {token}"}
```

**Rationale**:
- Tests real JWT validation logic
- No need to mock authentication middleware
- Easy to create different user roles (admin vs non-admin)

### API Keys

**Approach**: Create API key in database, return raw key

```python
@pytest.fixture
def auth_headers_apikey(test_user_app):
    raw_key = secret.generate_api_key()
    # Store hash in database...
    return {"X-API-Key": raw_key}
```

**Rationale**:
- Tests API key hash verification
- Tests prefix-based key lookup
- Ensures key rotation works

### Test User Creation

**Two fixtures** for different contexts:

1. **`test_user`** - For `db_conn` fixture users
   - Direct database insertion
   - Used in unit tests without Flask app

2. **`test_user_app`** - For `flask_app` fixture users
   - Uses Flask app's database connection
   - Used in integration tests with API

---

## Common Pitfalls

### 1. Using File Databases in Tests

**Bad**:
```python
conn = sqlite3.connect("test.db")  # Leaves file in project dir
```

**Good**:
```python
conn = sqlite3.connect(":memory:")  # Auto-cleanup
# OR
conn = sqlite3.connect("file:test?mode=memory&cache=shared", uri=True)
```

### 2. Forgetting `uri=True` with SQLite URIs

**Bad**:
```python
conn = sqlite3.connect("file:db?mode=memory&cache=shared")  # URI ignored!
```

**Good**:
```python
conn = sqlite3.connect("file:db?mode=memory&cache=shared", uri=True)
```

### 3. Creating Files in Project Directory

**Bad**:
```python
with open("test-output.txt", "w") as f:  # Fails guard_project_dir
    f.write(data)
```

**Good**:
```python
import tempfile
with tempfile.NamedTemporaryFile(mode="w", delete=True) as f:
    f.write(data)
    # File auto-deleted on close
```

### 4. Tests That Depend on Execution Order

**Bad**:
```python
def test_create_transaction():
    # Creates transaction
    ...

def test_get_transaction():
    # Assumes transaction exists (fails if run alone!)
    ...
```

**Good**:
```python
def test_get_transaction(client, auth_headers):
    # Create transaction within test
    create_response = client.post("/transactions", json={...})
    transaction_id = create_response.json["id"]

    # Now test get
    get_response = client.get(f"/transactions/{transaction_id}")
    ...
```

---

## Future Considerations

### Potential Improvements

1. **Parallel Test Execution**
   - Currently: Tests run sequentially
   - Future: Use `pytest-xdist` for parallel runs
   - Challenge: Shared in-memory databases don't work across processes
   - Solution: Use `/tmp` databases with process IDs

2. **Test Temp Directory**
   - Currently: Tests use in-memory databases (ideal)
   - Future: Dedicated temp directory for file-based tests
   - Implementation: Session-scoped temp dir fixture

3. **Coverage Goals**
   - Current: No formal coverage target
   - Future: Set minimum coverage percentage (e.g., 80%)
   - Enforcement: `pytest --cov --cov-fail-under=80`

4. **Property-Based Testing**
   - Current: Example-based tests (specific inputs/outputs)
   - Future: Use `hypothesis` for property-based tests
   - Benefit: Catches edge cases that examples miss

---

## References

- **conftest.py** - Test fixtures and configuration
- **architecture.md** - Testing philosophy (in memogarden-core/docs)
- **test-regression-soil-uri-bug.md** - Root cause analysis of URI bug
- **pytest documentation** - https://docs.pytest.org/

---

**Last Updated**: 2026-02-11
**For**: MemoGarden API test suite maintainers
