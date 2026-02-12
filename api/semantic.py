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

Session 2 implements Soil bundle verbs:
- add: Add fact (bring external data into MemoGarden)
- amend: Amend fact (create superseding fact)
- get: Get fact by UUID (routes based on UUID prefix)
- query: Query facts with filters (routes based on target_type)
"""

import json
import logging

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError

from api.handlers import core as core_handlers
from api.handlers import soil as soil_handlers
from api.handlers import artifact as artifact_handlers
from api.handlers import conversation as conversation_handlers
from api.schemas.semantic import (
    AddRequest,
    AmendRequest,
    CreateRequest,
    DiffCommitsRequest,
    EditRequest,
    EnterRequest,
    ExploreRequest,
    FoldRequest,
    FocusRequest,
    ForgetRequest,
    GetArtifactAtCommitRequest,
    GetRequest,
    LeaveRequest,
    LinkRequest,
    QueryRelationRequest,
    QueryRequest,
    SearchRequest,
    SemanticRequest,
    SemanticResponse,
    TrackRequest,
    UnlinkRequest,
    CommitArtifactRequest,
)
from system.exceptions import (
    AuthenticationError,
    MemoGardenError,
    ResourceNotFound,
)
from system.exceptions import (
    ValidationError as MGValidationError,
)
from system.utils import isodatetime

logger = logging.getLogger(__name__)

# Create Blueprint
semantic_bp = Blueprint("semantic", __name__, url_prefix="/mg")

# Map operation names to handler functions
HANDLERS = {
    # Core bundle
    "create": core_handlers.handle_create,
    "edit": core_handlers.handle_edit,
    "forget": core_handlers.handle_forget,
    "track": core_handlers.handle_track,
    "search": core_handlers.handle_search,
    # Relations bundle
    "link": core_handlers.handle_link,
    "unlink": core_handlers.handle_unlink,
    "edit_relation": core_handlers.handle_edit_relation,
    "get_relation": core_handlers.handle_get_relation,
    "query_relation": core_handlers.handle_query_relation,
    "explore": core_handlers.handle_explore,
    # Soil bundle
    "add": soil_handlers.handle_add,
    "amend": soil_handlers.handle_amend,
    # Context bundle (RFC-003 v4)
    "enter": core_handlers.handle_enter,
    "leave": core_handlers.handle_leave,
    "focus": core_handlers.handle_focus,
    # Artifact delta bundle (Session 17)
    "commit_artifact": artifact_handlers.handle_commit_artifact,
    "get_artifact_at_commit": artifact_handlers.handle_get_artifact_at_commit,
    "diff_commits": artifact_handlers.handle_diff_commits,
    # Conversation bundle (Session 18)
    "fold": conversation_handlers.handle_fold,
    "get_conversation": conversation_handlers.handle_get_conversation,
}


def _get_handler(op: str, request_json: dict):
    """Get handler function for operation.

    Some operations route to different handlers based on request parameters:
    - get: Routes based on target UUID prefix (soil_ → fact, core_ → entity)
    - get_relation: Routes to handle_get_relation
    - query: Routes based on target_type field (fact → soil, entity/relation → core)
    - edit_relation: Routes to handle_edit_relation
    """
    if op == "get":
        # Route based on target UUID prefix
        target = request_json.get("target", "")
        if target.startswith("soil_"):
            return soil_handlers.handle_get_fact
        else:
            return core_handlers.handle_get

    if op == "query":
        # Route based on target_type field
        target_type = request_json.get("target_type", "entity")
        if target_type == "fact":
            return soil_handlers.handle_query_facts
        else:
            return core_handlers.handle_query

    # Default handler lookup
    return HANDLERS.get(op)


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
        handler = _get_handler(op, request.json)
        if handler is None:
            response = SemanticResponse(
                ok=False,
                actor=actor,
                timestamp=isodatetime.now(),
                error={
                    "type": "ValidationError",
                    "message": f"Unsupported operation: {op}",
                    "details": {
                        "supported_operations": sorted(set(HANDLERS.keys()) | {"get", "query"}),
                    }
                }
            )
            return jsonify(response.model_dump()), 400

        # Validate request against appropriate schema
        validated_request = _validate_request(request.json, op)

        # Dispatch to handler
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
        "add": AddRequest,
        "amend": AmendRequest,
        "link": LinkRequest,
        "unlink": UnlinkRequest,
        "edit_relation": EditRequest,
        "get_relation": GetRequest,
        "query_relation": QueryRelationRequest,
        "explore": ExploreRequest,
        "track": TrackRequest,
        "search": SearchRequest,
        "enter": EnterRequest,
        "leave": LeaveRequest,
        "focus": FocusRequest,
        "commit_artifact": CommitArtifactRequest,
        "get_artifact_at_commit": GetArtifactAtCommitRequest,
        "diff_commits": DiffCommitsRequest,
        "fold": FoldRequest,
        "get_conversation": GetRequest,
    }

    schema = request_schemas.get(op)
    if schema is None:
        # Fallback to base schema (shouldn't happen given HANDLERS check)
        raise ValueError(f"No request schema defined for operation: {op}")

    return schema(**request_json)
