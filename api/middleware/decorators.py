"""Authentication decorators for protected endpoints.

This module provides decorators for enforcing security constraints on endpoints:
- @auth_required - Requires valid JWT token or API key
- @localhost_only - Restricts access to localhost only
- @first_time_only - Restricts access to first-time setup (no admin exists)

These decorators follow the project's pattern of declarative endpoint security
and can be composed together for multiple constraints.
"""

import logging
from functools import wraps

import jwt
from flask import g, request

from system.core import get_core
from ..exceptions import AuthenticationError
from . import api_keys, service, token

logger = logging.getLogger(__name__)


# ============================================================================
# Shared Authentication Logic
# ============================================================================


def _authenticate_request():
    """
    Shared authentication logic for requests.

    Supports two authentication methods:
    1. JWT Token via Authorization: Bearer <token>
    2. API Key via X-API-Key: <api_key>

    Stores authenticated user information in flask.g:
    - g.user_id: User ID (UUID)
    - g.username: Username
    - g.is_admin: Admin status
    - g.auth_method: "jwt" or "api_key"

    Raises:
        AuthenticationError: If no valid authentication provided

    This function is called by both @auth_required decorator and
    the transactions blueprint's before_request:authenticate handler.
    """
    core = get_core()

    # Try JWT token authentication first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token_str = auth_header[7:]  # Remove "Bearer " prefix
        try:
            payload = token.validate_access_token(token_str)

            # Store user info in flask.g
            g.user_id = payload.sub
            g.username = payload.username
            g.is_admin = payload.is_admin
            g.auth_method = "jwt"

            logger.debug(f"JWT authentication successful for user {g.username}")
            return  # Authentication succeeded

        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            raise AuthenticationError("Token has expired", {"code": "token_expired"})
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise AuthenticationError("Invalid token", {"code": "invalid_token"})

    # Try API key authentication
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        result = api_keys.verify_api_key_and_get_user(core._conn, api_key)
        if result:
            user_id, api_key_id = result

            # Get user details
            user = service.get_user_by_id(core._conn, user_id)
            if user:
                # Store user info in flask.g
                g.user_id = user.id
                g.username = user.username
                g.is_admin = user.is_admin
                g.auth_method = "api_key"

                logger.debug(f"API key authentication successful for user {g.username}")
                return  # Authentication succeeded

        logger.warning("Invalid API key")
        raise AuthenticationError("Invalid API key", {"code": "invalid_api_key"})

    # No authentication provided
    logger.warning("Unauthenticated request to protected endpoint")
    raise AuthenticationError(
        "Authentication required",
        {"code": "missing_auth"}
    )


def _authenticate_jwt():
    """
    JWT-only authentication for sensitive operations.

    Similar to _authenticate_request() but only accepts JWT tokens,
    not API keys. Used for endpoints that manage API keys themselves,
    to prevent API keys from creating or revoking other API keys.

    Stores authenticated user information in flask.g:
    - g.user_id: User ID (UUID)
    - g.username: Username
    - g.is_admin: Admin status
    - g.auth_method: "jwt"

    Returns:
        TokenPayload: The validated JWT token payload

    Raises:
        AuthenticationError: If no valid JWT token provided
    """
    auth_header = request.headers.get("Authorization")

    # Check for missing header
    if auth_header is None:
        raise AuthenticationError(
            "Missing authorization header",
            {"expected": "Authorization: Bearer <token>"}
        )

    # Parse Bearer token
    parts = auth_header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError(
            "Invalid authorization header format",
            {"expected": "Authorization: Bearer <token>"}
        )

    jwt_token = parts[1]

    # Validate token and get user ID
    try:
        payload = token.validate_access_token(jwt_token)
    except Exception as e:
        logger.warning(f"Invalid JWT token: {e}")
        raise AuthenticationError(
            "Invalid or expired token",
            {"token": jwt_token[:20] + "..."}
        )

    # Store user info in flask.g for consistency with _authenticate_request()
    g.user_id = payload.sub
    g.username = payload.username
    g.is_admin = payload.is_admin
    g.auth_method = "jwt"

    return payload


# ============================================================================
# Auth Required Decorator
# ============================================================================


def auth_required(f):
    """
    Decorator to require authentication for endpoint access.

    Supports two authentication methods:
    1. JWT Token via Authorization: Bearer <token>
    2. API Key via X-API-Key: <api_key>

    Stores authenticated user information in flask.g:
    - g.user_id: User ID (UUID)
    - g.username: Username
    - g.is_admin: Admin status
    - g.auth_method: "jwt" or "api_key"

    Raises:
        AuthenticationError: If no valid authentication provided

    Example:
    ```python
    @auth_required
    def protected_endpoint():
        # Access authenticated user via flask.g
        user_id = g.user_id
        username = g.username
        ...
    ```
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Call shared authentication logic
        _authenticate_request()
        return f(*args, **kwargs)

    return wrapper


# ============================================================================
# Localhost-Only Decorator
# ============================================================================


def localhost_only(f):
    """
    Decorator to restrict endpoint access to localhost only.

    Checks that the request originates from localhost (127.0.0.1, ::1).
    When config.bypass_localhost_check is True, treats requests as non-localhost.

    Raises:
        AuthenticationError: If request is not from localhost

    Example:
    ```python
    @localhost_only
    def sensitive_setup():
        # Only accessible from localhost
        ...
    ```
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        from ..config import settings

        # Check remote address
        remote_addr = request.remote_addr or ""

        # When bypass is enabled, treat as non-localhost (for testing)
        if settings.bypass_localhost_check:
            remote_addr = "192.168.1.100"  # Simulate non-localhost

        if remote_addr not in {"127.0.0.1", "::1", "localhost"}:
            logger.warning(f"Protected endpoint accessed from non-localhost: {remote_addr}")
            raise AuthenticationError(
                "This endpoint is only accessible from localhost",
                {"remote_addr": remote_addr}
            )

        return f(*args, **kwargs)

    return wrapper


# ============================================================================
# First-Time-Only Decorator
# ============================================================================


def first_time_only(f):
    """
    Decorator to restrict endpoint access to first-time setup only.

    Checks that no admin user exists in the database.
    Used for one-time setup operations like admin registration.

    Raises:
        AuthenticationError: If an admin user already exists

    Example:
    ```python
    @first_time_only
    def admin_registration():
        # Only accessible when no admin exists
        ...
    ```
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        core = get_core()

        # Check if admin already exists
        if service.has_admin_user(core._conn):
            logger.warning("First-time endpoint accessed after setup completed")
            raise AuthenticationError(
                "Setup has already been completed. This endpoint is disabled."
            )

        return f(*args, **kwargs)

    return wrapper
