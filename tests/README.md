# MemoGarden API Tests

Testing approach and patterns for MemoGarden API integration tests.

## Testing Philosophy

### No Mocking
Tests use real `memogarden-system` code without mocking. This ensures tests catch real integration issues that mocked tests would miss.

### In-Memory Database
Each test gets a fresh `:memory:` SQLite database for perfect isolation:
- No shared state between tests
- No database locking issues
- Fast execution (no disk I/O)
- Automatic cleanup (no manual state reset needed)

### Behavior-Focused
Tests verify external behavior (API requests/responses) rather than implementation details. This makes tests more maintainable and resilient to refactoring.

## Test Database Strategy (Session 5)

### Decision: In-Memory Database (`:memory:`)

**Previous Approach:** Shared temp file across all tests
- **Problem:** Database locking when tests run concurrently
- **Problem:** Test pollution (state from one test affects another)
- **Problem:** Flaky, order-dependent test failures
- **Problem:** Hard to debug (can't reproduce in isolation)

**Current Approach:** Fresh in-memory database per test
```python
@pytest.fixture(scope="function")  # Key: fresh database per test
def flask_app():
    conn = _create_sqlite_connection(":memory:")  # In-memory
    # Initialize schema
    yield app
    # Auto-cleanup on exit
```

**Benefits:**
- ✅ Perfect isolation - each test gets clean database
- ✅ Deterministic - tests pass/fail regardless of order
- ✅ No database locking - tests can run concurrently
- ✅ Fast - no disk I/O overhead
- ✅ Simple - automatic cleanup, no file management

**Trade-offs:**
- Schema initialization per test (acceptable overhead for test reliability)
- Can't inspect database file after test (use test assertions instead)

### Why This Matters

Before Session 5, integration tests would fail with:
```
sqlite3.OperationalError: database is locked
```

This happened because multiple tests tried to write to the same temp file. With `:memory:`, each test gets its own database, eliminating the locking issue entirely.

## Test Fixtures

### Primary Fixtures

| Fixture | Purpose | Scope | Database |
|---------|---------|-------|----------|
| `flask_app` | Flask app with initialized database | `function` (per test) | `:memory:` Core + Soil |
| `client` | Flask test client for API requests | `function` (per test) | Uses flask_app's database |
| `core` | Direct Core API access | `function` (per test) | Uses flask_app's database |
| `db_conn` | Direct database connection | `function` (per test) | `:memory:` |

### Handler Decorators (Session 5)

**Problem:** Database locking in sequential Flask requests

**Root Cause:**
- Each Flask request creates new Core/Soil instance via `get_core()`/`get_soil()`
- Connections rely on garbage collection to close (non-deterministic)
- Uncommitted changes rolled back when connection closes

**Solution:** Handler decorators in [`api/handlers/decorators.py`](../api/handlers/decorators.py)
- `@with_core_cleanup` - For Core handlers (enter, leave, focus)
- `@with_soil_cleanup` - For Soil handlers (add, amend)

**Pattern:**
```python
@with_core_cleanup
def handle_verb(request: Request, actor: str, core) -> dict:
    # Handler logic here
    # Decorator injects `core` parameter
    # No need for explicit commit/close
    return {...}
```

**Benefits:**
- ✅ Prevents database locks (deterministic connection closure)
- ✅ Ensures data persistence (explicit commit before close)
- ✅ Cleaner handler code (no boilerplate try/finally)
- ✅ Atomic transactions (rollback on exception, commit on success)

**Handler Implementation Rules:**
- ✅ Use public API methods: `core.entity.create()`, `soil.create_item()`, etc.
- ❌ Never access private connections: `core._conn.execute()` or `soil._get_connection().execute()`
- ⚠️ If public API doesn't support needed operation, add TODO comment and create issue

### Authentication Fixtures

| Fixture | Purpose | Returns |
|---------|---------|---------|
| `test_user` | Create test user in db_conn | User data dict |
| `test_user_app` | Create test user in flask_app's database | User data dict |
| `auth_headers` | JWT authentication headers | Headers dict |
| `auth_headers_apikey` | API key authentication headers | Headers dict |

### Test Data Fixtures

| Fixture | Purpose |
|---------|---------|
| `sample_transaction_data` | Sample transaction for testing |
| `sample_recurrence_data` | Sample recurrence for testing |
| `sample_entity` | Create sample entity for relation tests |

## Running Tests

### ⚠️ Standard Test Execution (MUST follow)

**IMPORTANT:** Always use the standardized `run_tests.sh` script for test execution. This ensures consistent behavior across environments and provides grep-able output for automation.

```bash
# From project root
./memogarden-api/run_tests.sh

# Or change to API directory first
cd memogarden-api && ./run_tests.sh
```

**Standard Commands:**

| Task | Command |
|------|---------|
| Run all tests | `./run_tests.sh` |
| Run with verbose output | `./run_tests.sh -xvs` |
| Run specific test file | `./run_tests.sh tests/test_semantic_api.py` |
| Run specific test | `./run_tests.sh tests/test_context.py::test_enter_scope_adds_to_active_set -xvs` |
| Run with coverage | `./run_tests.sh --cov=api --cov-report=html` |
| Stop on first failure | `./run_tests.sh -x` |
| Get summary only (for agents) | `./run_tests.sh --tb=no -q 2>&1 | tail -n 6` |

**Why use run_tests.sh:**
- Ensures correct Poetry environment is used
- Works from any directory (changes to project dir automatically)
- Provides grep-able output with test run ID and summary
- Last 6 lines always contain summary (use `tail -n 6` for quick status check)

**For quick status check (agents):**
```bash
# Get just the summary (6 lines) without full test output
./run_tests.sh --tb=no -q 2>&1 | tail -n 6
```

**Example output summary:**
```
╔═══════════════════════════════════════════════════════════╗
║  Test Summary                                               ║
╠═══════════════════════════════════════════════════════════╣
║  Status: PASSED                                            ║
║  Tests: 165 passed                                      ║
║  Duration: 8.14s                                        ║
║  Test Run ID: 20260213-064712                                  ║
╚═══════════════════════════════════════════════════════════╝
```

**Legacy method (deprecated):**
The old `scripts/test.sh` script is still available but deprecated. Use `run_tests.sh` instead.

## Writing Tests

### Test Structure

```python
def test_feature_description(client, auth_headers):
    """
    Brief description of what's being tested.
    """
    # Setup: Create test data
    response = client.post("/mg", json={"op": "create", ...}, headers=auth_headers)
    entity_uuid = response.get_json()["result"]["uuid"]

    # Exercise: Test the feature
    response = client.post("/mg", json={"op": "get", "target": entity_uuid}, headers=auth_headers)

    # Verify: Check expected behavior
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["result"]["uuid"] == entity_uuid
```

### Test Naming Convention

- Use descriptive names: `test_verb_expected_outcome`
- Examples:
  - `test_enter_scope_adds_to_active_set`
  - `test_leave_scope_clears_primary_if_leaving_primary`
  - `test_focus_scope_not_active_raises`

### Assertions

- **Status codes:** Always assert HTTP status codes
- **Response envelope:** Check `ok`, `error`, or `result` fields
- **Data integrity:** Verify UUIDs, types, and relationships
- **Invariants:** Test RFC invariants explicitly (e.g., `test_one_context_per_owner`)

## Known Issues & Limitations

### Database Locking (FIXED in Session 5)
- **Status:** ✅ Resolved
- **Fix:** Switched from shared temp file to `:memory:` database
- **Impact:** All integration tests now pass reliably

### Context Frame View Timeline (DEFERRED)
- **Status:** ⚠️ Known limitation
- **Issue:** `view_timeline` not persisted to database
- **Impact:** Tests don't verify cross-session timeline persistence
- **Planned Fix:** Session 6+ - Add view_timeline column to context_frame table

## Test Coverage Goals

### Current Coverage
- Unit tests (Core operations): ✅ Excellent (37/37 passing)
- Integration tests (Semantic API): ✅ Good (45/51 passing, 6 failures from old temp file approach)
- RFC-003 Context invariants: ✅ Complete (INV-8, INV-11, INV-11a, INV-11b, INV-20)

### Target Coverage
- All Semantic API verbs: ⏳ In progress
- Error handling paths: ⏳ Partial
- Edge cases: ⏳ Good

## References

- [Testing Philosophy](../../memogarden-core/docs/architecture.md#testing) - Core testing principles
- [RFC-003 Context Mechanism](../../plan/rfc_003_context_mechanism_v4.md) - Context invariants
- [RFC-005 Semantic API](../../plan/rfc-005_semantic_api_v7.md) - API specification
- [Implementation Plan](../../plan/memogarden-implementation-plan.md) - Session progress

---

**Last Updated:** 2026-02-08 (Session 5: Fixed database locking with `:memory:` database)
