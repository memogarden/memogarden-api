"""Semantic API endpoint - /mg

Per RFC-005 v7, the Semantic API is a message-passing interface that uses
a single /mg endpoint with operation-based dispatch.

Request format:
    {"op": "create", "type": "Contact", "data": {...}}

Response envelope (success):
    {"ok": true, "actor": "usr_xxx", "timestamp": "...", "result": {...}}

Response envelope (error):
    {"ok": false, "actor": "usr_xxx", "timestamp": "...", "error": {...}}

Session 1 implements Core bundle verbs:
- create: Create entity (baseline types)
- get: Get entity by UUID
- edit: Edit entity (set/unset semantics)
- forget: Soft delete entity
- query: Query entities with filters
"""

import json
import logging

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError
from system.utils import isodatetime

from api.handlers import core as core_handlers
from api.schemas.semantic import (
    CreateRequest,
    EditRequest,
    ForgetRequest,
    GetRequest,
    QueryRequest,
    SemanticRequest,
    SemanticResponse,
)
from system.exceptions import (
    AuthenticationError,
    MemoGardenError,
    ResourceNotFound,
)
from system.exceptions import (
    ValidationError as MGValidationError,
)

logger = logging.getLogger(__name__)

# Create Blueprint
semantic_bp = Blueprint("semantic", __name__, url_prefix="/mg")

# Map operation names to handler functions
HANDLERS = {
    "create": core_handlers.handle_create,
    "get": core_handlers.handle_get,
    "edit": core_handlers.handle_edit,
    "forget": core_handlers.handle_forget,
    "query": core_handlers.handle_query,
}


# ============================================================================
# Authentication Middleware
# ============================================================================

@semantic_bp.before_request
def authenticate():
    """
    Require authentication for all Semantic API requests.

    Sets g.username and g.user_id for use in handlers.
    The actor field in the response will be the authenticated user's ID.
    """
    from api.middleware.decorators import _authenticate_request
    _authenticate_request()


# ============================================================================
# Main Dispatcher
# ============================================================================

@semantic_bp.route("", methods=["POST"])
def semantic_api():
    """
    Main Semantic API dispatcher.

    Accepts JSON request with "op" field specifying the verb.
    Dispatches to appropriate handler and wraps response in envelope.

    Request body:
        {
            "op": "create|get|edit|forget|query|...",
            ... (operation-specific fields)
        }

    Response:
        {
            "ok": true,
            "actor": "usr_xxx",
            "timestamp": "2026-02-07T12:34:56Z",
            "result": {...}
        }
    """
    # Get authenticated user
    actor = g.username

    # Parse request JSON
    if request.json is None:
        response = SemanticResponse(
            ok=False,
            actor=actor,
            timestamp=isodatetime.now(),
            error={
                "type": "ValidationError",
                "message": "Request body is required",
            }
        )
        return jsonify(response.model_dump()), 400

    try:
        # Validate operation is present
        if "op" not in request.json:
            response = SemanticResponse(
                ok=False,
                actor=actor,
                timestamp=isodatetime.now(),
                error={
                    "type": "ValidationError",
                    "message": "Missing required field: op",
                }
            )
            return jsonify(response.model_dump()), 400

        op = request.json["op"]

        # Check if operation is supported
        if op not in HANDLERS:
            response = SemanticResponse(
                ok=False,
                actor=actor,
                timestamp=isodatetime.now(),
                error={
                    "type": "ValidationError",
                    "message": f"Unsupported operation: {op}",
                    "details": {
                        "supported_operations": list(HANDLERS.keys()),
                    }
                }
            )
            return jsonify(response.model_dump()), 400

        # Validate request against appropriate schema
        validated_request = _validate_request(request.json, op)

        # Dispatch to handler
        handler = HANDLERS[op]
        result = handler(validated_request, actor)

        # Build success response
        response = SemanticResponse(
            ok=True,
            actor=actor,
            timestamp=isodatetime.now(),
            result=result,
        )
        return jsonify(response.model_dump()), 200

    except ValidationError as e:
        # Pydantic validation error
        logger.warning(
            f"Semantic API validation failed: op={request.json.get('op')}, "
            f"errors={e.errors()}, received={request.json}"
        )
        # Convert errors to JSON-serializable format
        error_list = []
        for error in e.errors():
            error_dict = {
                "type": error["type"],
                "loc": error["loc"],
                "msg": error["msg"],
            }
            # Add input if it's JSON-serializable
            if "input" in error:
                try:
                    json.dumps({"input": error["input"]})  # Test if serializable
                    error_dict["input"] = error["input"]
                except (TypeError, ValueError):
                    pass  # Skip non-serializable input
            error_list.append(error_dict)

        response = SemanticResponse(
            ok=False,
            actor=actor,
            timestamp=isodatetime.now(),
            error={
                "type": "ValidationError",
                "message": "Request validation failed",
                "details": {
                    "model": e.title,
                    "errors": error_list,
                }
            }
        )
        return jsonify(response.model_dump()), 400

    except (MemoGardenError, MGValidationError) as e:
        # MemoGarden exception - determine status code based on exception type
        if isinstance(e, ResourceNotFound):
            status_code = 404
        elif isinstance(e, ValidationError | MGValidationError):
            status_code = 400
        elif isinstance(e, AuthenticationError):
            status_code = 401
        else:
            status_code = 500

        response = SemanticResponse(
            ok=False,
            actor=actor,
            timestamp=isodatetime.now(),
            error={
                "type": e.__class__.__name__,
                "message": e.message,
            }
        )
        if e.details:
            response.error["details"] = e.details  # type: ignore
        return jsonify(response.model_dump()), status_code

    except ValueError as e:
        # Generic ValueError (e.g., unsupported entity type)
        response = SemanticResponse(
            ok=False,
            actor=actor,
            timestamp=isodatetime.now(),
            error={
                "type": "ValueError",
                "message": str(e),
            }
        )
        return jsonify(response.model_dump()), 400

    except Exception:
        # Unexpected error
        logger.exception(f"Unexpected error in Semantic API: op={request.json.get('op')}")
        response = SemanticResponse(
            ok=False,
            actor=actor,
            timestamp=isodatetime.now(),
            error={
                "type": "InternalServerError",
                "message": "An unexpected error occurred",
            }
        )
        return jsonify(response.model_dump()), 500


# ============================================================================
# Request Validation
# ============================================================================

def _validate_request(request_json: dict, op: str) -> SemanticRequest:
    """Validate request against appropriate Pydantic schema.

    Args:
        request_json: Raw request JSON dict
        op: Operation name

    Returns:
        Validated Pydantic model instance

    Raises:
        ValidationError: If validation fails
    """
    # Map operations to request schemas
    request_schemas = {
        "create": CreateRequest,
        "get": GetRequest,
        "edit": EditRequest,
        "forget": ForgetRequest,
        "query": QueryRequest,
    }

    schema = request_schemas.get(op)
    if schema is None:
        # Fallback to base schema (shouldn't happen given HANDLERS check)
        raise ValueError(f"No request schema defined for operation: {op}")

    return schema(**request_json)
