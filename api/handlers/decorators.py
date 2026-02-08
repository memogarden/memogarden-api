"""Shared decorators for Semantic API verb handlers.

Session 6.5 Update: Context Manager Enforcement
------------------------------------------------
As of Session 6.5, Core and Soil enforce context manager usage at runtime.
Handlers MUST use `with get_core() as core:` and `with get_soil() as soil:`.
Connection lifecycle (commit/rollback/close) is managed by the context managers.

The old cleanup decorators (with_core_cleanup, with_soil_cleanup) have been removed.

Session 6: Audit Facts
----------------------
The with_audit decorator adds audit logging for all Semantic API operations via
Action and ActionResult facts per RFC-005 v7 Section 7:
- Action fact: Created immediately when operation starts
- ActionResult fact: Created when operation completes (success/failure)
- result_of relation: Links ActionResult to Action
- bypass_semantic_api flag: Prevents recursion in audit logging
"""

import json
import time
from functools import wraps

from system.core import get_core
from system.soil import get_soil
from system.soil.item import Item, current_day, generate_soil_uuid
from system.soil.relation import SystemRelation
from system.utils import isodatetime, uid


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
        action_item = Item(
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
            soil.create_item(action_item)
            # Commits on __exit__

        try:
            # Call the actual handler
            result = handler_func(request, actor)

            # Create ActionResult fact (success) - separate context
            duration_ms = int((time.time() - start_time) * 1000)
            actionresult_uuid = generate_soil_uuid()

            actionresult_item = Item(
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
                soil.create_item(actionresult_item)

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

            # Only create ActionResult if Action was created successfully
            if action_uuid:
                try:
                    actionresult_item = Item(
                        uuid=actionresult_uuid,
                        _type="ActionResult",
                        realized_at=isodatetime.now(),
                        canonical_at=isodatetime.now(),
                        data={
                            "result": None,
                            "error": str(e),
                            "result_summary": _generate_result_summary(operation, None, success=False, error=e),
                            "duration_ms": duration_ms,
                            "status": "error",
                        }
                    )

                    # Create ActionResult and relation in single transaction
                    with get_soil() as soil:
                        soil.create_item(actionresult_item)

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
