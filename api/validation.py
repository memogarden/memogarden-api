"""Request validation decorators.

VALIDATION MESSAGE PRINCIPLE:
Assume API is used by human/agent stakeholders. Provide clear, detailed
validation error messages. No need to obscure information for "security".
"""

import functools
import inspect
import logging

from flask import request
from pydantic import BaseModel, ValidationError

from .exceptions import ValidationError as MGValidationError

logger = logging.getLogger(__name__)


def _find_body_parameter(params: list[inspect.Parameter], view_args: dict) -> tuple[inspect.Parameter | None, type | None]:
    """Find the first parameter that is NOT a path parameter.

    Path parameters are in view_args. The first parameter not in view_args
    is the body parameter to validate.

    Returns:
        Tuple of (body_param, body_model_class). Returns (None, None) if all params are path params.
    """
    for param in params:
        if param.name not in view_args:
            return param, param.annotation
    return None, None


def _ensure_basemodel_annotation(param: inspect.Parameter, model_class: type, func_name: str) -> None:
    """Ensure parameter has a Pydantic BaseModel annotation.

    Raises:
        TypeError: If annotation is missing or not a BaseModel subclass.
    """
    if model_class is inspect.Parameter.empty:
        raise TypeError(
            f"Function {func_name}'s body parameter '{param.name}' "
            "lacks a type annotation. Expected a Pydantic BaseModel subclass."
        )

    if not (isinstance(model_class, type) and issubclass(model_class, BaseModel)):
        raise TypeError(
            f"Type annotation for '{param.name}' must be a Pydantic BaseModel subclass, "
            f"got {model_class!r}"
        )


def _ensure_request_body_exists(model_class: type) -> None:
    """Ensure request.json is present.

    Raises:
        MGValidationError: If request.json is None.
    """
    if request.json is None:
        raise MGValidationError(
            "Request body is required but was not provided.",
            {
                "expected": model_class.__name__,
                "received": None,
                "schema": model_class.model_json_schema()
            }
        )


def _format_validation_errors(pydantic_errors: list[dict]) -> list[dict]:
    """Format Pydantic validation errors into our error structure.

    Each error includes field path, message, and expected type.
    """
    error_details = []
    for error in pydantic_errors:
        loc = " -> ".join(str(item) for item in error["loc"])
        error_details.append({
            "field": loc,
            "message": error["msg"],
            "expected_type": error["type"],
        })
    return error_details


def _validate_request_body(model_class: type) -> BaseModel:
    """Parse and validate request JSON against Pydantic model.

    Returns:
        Validated Pydantic model instance.

    Raises:
        MGValidationError: If validation fails.
    """
    try:
        return model_class(**request.json)
    except ValidationError as e:
        errors = _format_validation_errors(e.errors())

        # Log validation errors to stderr for debugging
        logger.warning(
            f"Validation failed for {model_class.__name__}: "
            f"path={request.path}, errors={errors}, received={request.json}"
        )

        raise MGValidationError(
            f"Request validation failed for {model_class.__name__}. "
            f"See details for specific fields.",
            {
                "model": model_class.__name__,
                "errors": errors,
                "received": request.json,
            }
        )


def validate_request(f):
    """
    Decorator that validates request JSON against a body parameter's type annotation.

    Skips path parameters (in view_args) and validates the first non-path parameter.

    Example:
        @app.put("/items/<uuid>")
        @validate_request
        def update_item(uuid: str, data: ItemUpdate):
            # uuid is from path (string), data is validated Pydantic model
            pass
    """
    # Get all parameters (at decoration time)
    sig = inspect.signature(f)
    params = list(sig.parameters.values())

    if not params:
        raise TypeError(
            f"Function {f.__name__} has no parameters to validate. "
            "At least one parameter should have a Pydantic model type annotation."
        )

    # Validate first parameter has type annotation (early error for misconfiguration)
    first_param = params[0]
    if first_param.annotation is inspect.Parameter.empty:
        raise TypeError(
            f"Function {f.__name__}'s first parameter '{first_param.name}' "
            "lacks a type annotation. Expected a Pydantic BaseModel subclass."
        )

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # At request time, find and validate body parameter
        view_args = request.view_args or {}
        body_param, body_model_class = _find_body_parameter(params, view_args)

        # If all params are path params, no validation needed
        if body_param is None:
            return f(*args, **kwargs)

        # Ensure body parameter has BaseModel annotation
        _ensure_basemodel_annotation(body_param, body_model_class, f.__name__)

        # Validate request body exists and parse it
        _ensure_request_body_exists(body_model_class)
        validated_data = _validate_request_body(body_model_class)

        # Inject validated data and call original function
        kwargs[body_param.name] = validated_data
        return f(*args, **kwargs)

    return wrapper
