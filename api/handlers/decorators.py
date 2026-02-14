"""Shared decorators for Semantic API verb handlers.

Session 6.6: ActionResult Schema Enhancement (RFC-005 v7.1)
------------------------------------------------------------
Structured error capture with error.code, error.message, error.details:
- error.code: Machine-readable error type (validation_error, not_found,
  lock_conflict, permission_denied, internal_error)
- error.message: Human-readable error description
- error.details: Optional structured error context
- error_type: Full exception class name (e.g., "system.exceptions.ResourceNotFound")
- error_traceback: Full Python traceback for debugging

Exception types map to error codes:
- ValidationError → validation_error
- ResourceNotFound → not_found
- LockConflictError → lock_conflict
- PermissionDenied → permission_denied
- All other exceptions → internal_error

Session 6.5: Context Manager Enforcement
-----------------------------------------
As of Session 6.5, Core and Soil enforce context manager usage at runtime.
Handlers MUST use `with get_core() as core:` and `with get_soil() as soil:`.
Connection lifecycle (commit/rollback/close) is managed by the context managers.

The old cleanup decorators (with_core_cleanup, with_soil_cleanup) have been removed.

Session 6: Audit Facts (RFC-005 v7 Section 7)
----------------------------------------------
The with_audit decorator adds audit logging for all Semantic API operations:
- Action fact: Created immediately when operation starts
- ActionResult fact: Created when operation completes (success/failure)
- result_of relation: Links ActionResult to Action
- bypass_semantic_api flag: Prevents recursion in audit logging
"""

import json
import time
import traceback
from functools import wraps

from system.core import get_core
from system.soil import Fact, SystemRelation, current_day, generate_soil_uuid, get_soil
from utils import datetime as isodatetime, uid
from system.exceptions import (
    MemoGardenError,
    ValidationError,
    ResourceNotFound,
    LockConflictError,
    PermissionDenied,
)


# ============================================================================
# Audit Decorator (Session 6)
# ============================================================================

def with_audit(handler_func):
    """Decorator that adds audit logging for Semantic API operations.

    Creates Action and ActionResult facts in Soil to track all operations.
    Per RFC-005 v7 Section 7, this provides complete audit trail.

    Creates:
    1. Action fact at operation start (actor, operation, params)
    2. ActionResult fact at operation end (status, duration, result/error)
    3. result_of relation linking ActionResult → Action

    Uses bypass_semantic_api flag to prevent recursion when creating audit facts.

    Args:
        handler_func: Verb handler function to wrap

    Returns:
        Wrapped handler function with audit logging
    """
    @wraps(handler_func)
    def wrapper(request, actor):
        # Check if audit logging is bypassed (to prevent recursion)
        # Use getattr to safely check request model
        request_model = request if hasattr(request, 'model_dump') else None
        bypass_audit = getattr(request_model, 'bypass_semantic_api', False) if request_model else False

        if bypass_audit:
            # Audit disabled for this request - call handler directly
            return handler_func(request, actor)

        action_uuid = None
        operation = None
        start_time = time.time()

        # Extract operation name before creating context
        if hasattr(request_model, 'op'):
            operation = request_model.op
        elif hasattr(request, 'op'):
            operation = request.op
        else:
            # Fallback: derive from handler name
            operation = handler_func.__name__.replace('handle_', '')

        # Generate request ID for correlation
        request_id = uid.generate_uuid()

        # Create Action fact immediately (separate context for immediate commit)
        action_uuid = generate_soil_uuid()
        action_item = Fact(
            uuid=action_uuid,
            _type="Action",
            realized_at=isodatetime.now(),
            canonical_at=isodatetime.now(),
            data={
                "actor": actor,
                "operation": operation,
                "params": _serialize_params(request_model),
                "context": getattr(request_model, 'context', None),
                "request_id": request_id,
                "parent_action": getattr(request_model, 'parent_action', None),
            }
        )

        # Use separate context for Action (immediate commit so other agents can see "in progress")
        with get_soil() as soil:
            soil.create_fact(action_item)
            # Commits on __exit__

        try:
            # Call the actual handler
            result = handler_func(request, actor)

            # Create ActionResult fact (success) - separate context
            duration_ms = int((time.time() - start_time) * 1000)
            actionresult_uuid = generate_soil_uuid()

            actionresult_item = Fact(
                uuid=actionresult_uuid,
                _type="ActionResult",
                realized_at=isodatetime.now(),
                canonical_at=isodatetime.now(),
                data={
                    "result": result if _is_json_serializable(result) else None,
                    "error": None,
                    "result_summary": _generate_result_summary(operation, result, success=True),
                    "duration_ms": duration_ms,
                    "status": "success",
                }
            )

            # Create ActionResult and relation in single transaction
            with get_soil() as soil:
                soil.create_fact(actionresult_item)

                # Create result_of relation
                relation = SystemRelation(
                    uuid=generate_soil_uuid(),
                    kind="result_of",
                    source=actionresult_uuid,
                    source_type="item",
                    target=action_uuid,
                    target_type="item",
                    created_at=current_day(),
                    evidence={
                        "source": "system_inferred",
                        "method": "audit_logging",
                    }
                )
                soil.create_relation(relation)
                # Commits on __exit__

            return result

        except Exception as e:
            # Create ActionResult fact (error) - separate context
            duration_ms = int((time.time() - start_time) * 1000)
            actionresult_uuid = generate_soil_uuid()

            # Capture structured error information (RFC-005 v7.1)
            error_code = _get_error_code(e)
            error_message = str(e)
            error_details = _extract_error_details(e)
            error_type = f"{e.__class__.__module__}.{e.__class__.__name__}"
            error_traceback = traceback.format_exc()

            # Only create ActionResult if Action was created successfully
            if action_uuid:
                try:
                    actionresult_item = Fact(
                        uuid=actionresult_uuid,
                        _type="ActionResult",
                        realized_at=isodatetime.now(),
                        canonical_at=isodatetime.now(),
                        data={
                            "result": None,
                            "error": {
                                "code": error_code,
                                "message": error_message,
                                "details": error_details,
                            },
                            "result_summary": _generate_result_summary(operation, None, success=False, error=e),
                            "duration_ms": duration_ms,
                            "status": "error",
                            "error_type": error_type,
                            "error_traceback": error_traceback,
                        }
                    )

                    # Create ActionResult and relation in single transaction
                    with get_soil() as soil:
                        soil.create_fact(actionresult_item)

                        # Create result_of relation
                        relation = SystemRelation(
                            uuid=generate_soil_uuid(),
                            kind="result_of",
                            source=actionresult_uuid,
                            source_type="item",
                            target=action_uuid,
                            target_type="item",
                            created_at=current_day(),
                            evidence={
                                "source": "system_inferred",
                                "method": "audit_logging",
                            }
                        )
                        soil.create_relation(relation)
                        # Commits on __exit__
                except Exception as audit_error:
                    # If audit logging fails, don't hide the original error
                    # Log and continue with original exception
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.exception(
                        "Failed to create audit ActionResult for action=%s operation=%s error=%s",
                        action_uuid,
                        operation,
                        str(audit_error)
                    )

            # Re-raise the original exception
            raise

    return wrapper


# ============================================================================
# Helper Functions for Audit Decorator
# ============================================================================

def _serialize_params(request_model) -> dict:
    """Serialize request parameters to dict for audit logging.

    Args:
        request_model: Pydantic request model or dict

    Returns:
        JSON-serializable dict of parameters
    """
    if request_model is None:
        return {}

    if hasattr(request_model, 'model_dump'):
        # Pydantic model - use model_dump() to get dict
        params = request_model.model_dump(exclude={'op', 'bypass_semantic_api'})
    elif isinstance(request_model, dict):
        # Regular dict - exclude certain keys
        params = {k: v for k, v in request_model.items() if k not in {'op', 'bypass_semantic_api'}}
    else:
        # Fallback - convert to dict
        params = dict(vars(request_model))

    # Recursively clean non-serializable values
    return _clean_for_json(params)


def _clean_for_json(obj):
    """Recursively clean object for JSON serialization.

    Replaces non-serializable objects with string representations.
    """
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        # Convert non-serializable objects to string
        return str(obj)


def _is_json_serializable(obj) -> bool:
    """Check if object is JSON-serializable."""
    try:
        json.dumps(obj)
        return True
    except (TypeError, ValueError):
        return False


def _generate_result_summary(operation: str, result, success: bool, error: Exception = None) -> str:
    """Generate human-readable summary of operation result.

    Args:
        operation: Operation name
        result: Operation result (if successful)
        success: Whether operation succeeded
        error: Exception (if failed)

    Returns:
        Human-readable summary string
    """
    if not success:
        return f"{operation} failed: {error.__class__.__name__}"

    if result is None:
        return f"{operation} completed"

    if isinstance(result, dict):
        # Extract meaningful info from result dict
        if 'uuid' in result:
            return f"{operation} completed: {result.get('uuid', '')}"
        elif 'count' in result:
            return f"{operation} returned {result['count']} items"
        elif 'results' in result:
            count = len(result.get('results', []))
            return f"{operation} returned {count} items"
        else:
            return f"{operation} completed"

    return f"{operation} completed"


def _get_error_code(exception: Exception) -> str:
    """Map exception type to error code per RFC-005 v7.1.

    Args:
        exception: Exception to classify

    Returns:
        Error code string (validation_error, not_found, lock_conflict,
        permission_denied, or internal_error)
    """
    # Map specific exception types to error codes
    if isinstance(exception, ValidationError):
        return "validation_error"
    elif isinstance(exception, ResourceNotFound):
        return "not_found"
    elif isinstance(exception, LockConflictError):
        return "lock_conflict"
    elif isinstance(exception, PermissionDenied):
        return "permission_denied"
    else:
        # Default to internal_error for unknown exceptions
        return "internal_error"


def _extract_error_details(exception: Exception) -> dict | None:
    """Extract structured error details from exception.

    Args:
        exception: Exception to extract details from

    Returns:
        Dictionary with error details, or None if no details available
    """
    # Check if exception has a details attribute (MemoGardenError)
    if hasattr(exception, 'details') and exception.details is not None:
        return exception.details

    # For validation errors, try to extract field names
    if isinstance(exception, ValidationError):
        return {"validation_error": str(exception)}

    # For resource not found, try to extract resource type
    if isinstance(exception, ResourceNotFound):
        return {"resource": str(exception)}

    # No structured details available
    return None
