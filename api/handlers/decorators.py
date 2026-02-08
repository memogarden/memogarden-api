"""Shared decorators for Semantic API verb handlers.

Provides connection lifecycle management decorators that ensure:
- Changes are committed before connection closes (persistence)
- Connections are closed even if handler raises exception (cleanup)

This prevents database locks in sequential Flask requests.

Session 5 Fix: Database Locking
-------------------------------
Without these decorators, sequential Flask requests can experience:
- Database locks (connections not promptly closed)
- Data loss (uncommitted changes rolled back on close)

Root Cause:
- Each Flask request creates a new Core/Soil instance via get_core()/get_soil()
- Instances rely on garbage collection to close connections (non-deterministic)
- Sequential requests have overlapping connections causing "database is locked" errors
- Core/Soil don't enable autocommit, so uncommitted changes are rolled back on close

Solution:
- Decorators explicitly commit and close connections in finally blocks
- Provides deterministic resource management regardless of handler success/failure

Implementation Notes: Rollback Detection
-----------------------------------------
The decorators use `sys.exc_info()` to detect whether an exception occurred:
- `exc_type is None` → No exception, commit the transaction
- `exc_type is not None` → Exception occurred, rollback the transaction

Limitations:
- Only catches Exception subclasses (caught by `except Exception`)
- Does NOT catch BaseException subclasses (e.g., KeyboardInterrupt, SystemExit)
- For BaseException, the exception propagates before `sys.exc_info()` is set
- This is acceptable for Flask handlers (KeyboardExit should terminate anyway)

The rollback behavior matches Core's context manager pattern in `system/core/__init__.py`.
"""

from functools import wraps

from system.core import get_core
from system.soil import get_soil


def with_core_cleanup(handler_func):
    """Decorator that ensures Core connection is committed and closed after handler.

    Wraps Semantic API verb handlers to provide deterministic Core connection
    lifecycle management. Injects Core instance as third parameter.

    Follows Core's context manager semantics (see Core.__exit__):
    - Success → commit changes, close connection
    - Exception → rollback changes, close connection
    - Always → close connection

    Usage:
        @with_core_cleanup
        def handle_verb(request: Request, actor: str, core) -> dict:
            # Handler logic here
            # Core instance provided as third parameter
            # No need for explicit commit/close
            return {...}

    The decorator will:
    1. Create Core instance via get_core()
    2. Call handler with (request, actor, core)
    3. If successful: commit changes in finally block
    4. If exception: rollback changes in finally block
    5. Always: close connection in finally block

    Args:
        handler_func: Verb handler function to wrap

    Returns:
        Wrapped handler function that manages Core connection lifecycle
    """
    @wraps(handler_func)
    def wrapper(request, actor):
        core = get_core()
        try:
            result = handler_func(request, actor, core)
            # Success: will commit in finally block
            return result
        except Exception:
            # Exception: will rollback in finally block
            raise
        finally:
            try:
                # Check if we're in exception context
                # sys.exc_info() returns (None, None, None) if no exception
                import sys
                exc_type, _, _ = sys.exc_info()
                if exc_type is None:
                    # No exception - commit the transaction
                    core._conn.commit()
                else:
                    # Exception occurred - rollback the transaction
                    core._conn.rollback()
            finally:
                # Always close connection to prevent database locks
                core._conn.close()
    return wrapper


def with_soil_cleanup(handler_func):
    """Decorator that ensures Soil connection is committed and closed after handler.

    Wraps Semantic API verb handlers to provide deterministic Soil connection
    lifecycle management. Injects Soil instance as third parameter.

    Follows atomic transaction semantics:
    - Success → commit changes, close connection
    - Exception → rollback changes, close connection
    - Always → close connection

    Usage:
        @with_soil_cleanup
        def handle_verb(request: Request, actor: str, soil) -> dict:
            # Handler logic here
            # Soil instance provided as third parameter
            # No need for explicit commit/close
            return {...}

    The decorator will:
    1. Create Soil instance via get_soil()
    2. Call handler with (request, actor, soil)
    3. If successful: commit changes in finally block
    4. If exception: rollback changes in finally block
    5. Always: close connection in finally block

    Args:
        handler_func: Verb handler function to wrap

    Returns:
        Wrapped handler function that manages Soil connection lifecycle
    """
    @wraps(handler_func)
    def wrapper(request, actor):
        soil = get_soil()
        try:
            result = handler_func(request, actor, soil)
            # Success: will commit in finally block
            return result
        except Exception:
            # Exception: will rollback in finally block
            raise
        finally:
            try:
                # Check if we're in exception context
                # sys.exc_info() returns (None, None, None) if no exception
                import sys
                exc_type, _, _ = sys.exc_info()
                if exc_type is None:
                    # No exception - commit the transaction
                    soil._get_connection().commit()
                else:
                    # Exception occurred - rollback the transaction
                    soil._get_connection().rollback()
            finally:
                # Always close connection to prevent database locks
                soil._get_connection().close()
    return wrapper
