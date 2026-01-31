"""Authentication API endpoints for MemoGarden Core.

These endpoints handle user authentication and return JSON responses:
- Admin registration (localhost only, one-time)
- User login and logout
- User profile retrieval
- API key management

All endpoints return JSON responses. Admin registration is only accessible
from localhost and only when no users exist in the database.
"""

import logging
import sqlite3

from flask import Blueprint, jsonify, request

from ..validation import validate_request
from ..exceptions import AuthenticationError
from . import api_keys, decorators, service, token
from .decorators import _authenticate_jwt
from .schemas import AdminRegistrationResponse, APIKeyCreate, TokenResponse, UserCreate, UserLogin
from system.core import get_core

logger = logging.getLogger(__name__)


# Create blueprint
auth_bp = Blueprint("auth", __name__)


# ============================================================================
# Admin Registration (localhost only, one-time)
# ============================================================================


@auth_bp.route("/admin/register", methods=["POST"])
@validate_request
@decorators.localhost_only
@decorators.first_time_only
def admin_register(data: UserCreate):
    """
    Create admin account (localhost only, one-time).

    Security constraints enforced by decorators:
    - @localhost_only: Only accessible from localhost (127.0.0.1, ::1)
    - @first_time_only: Only accessible when no admin user exists

    Args:
        data: User creation data with username and password

    Returns:
        Admin registration response with created user

    Raises:
        AuthenticationError: If not localhost or admin already exists
        ValidationError: If request data is invalid

    Example request:
    ```json
    {
        "username": "admin",
        "password": "SecurePass123"
    }
    ```

    Example response:
    ```json
    {
        "message": "Admin account created successfully",
        "user": {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "username": "admin",
            "is_admin": true,
            "created_at": "2025-12-29T10:30:00Z"
        }
    }
    ```
    """
    core = get_core()
    try:
        # Create admin user (authorization handled by decorators)
        user = service.create_user(core._conn, data, is_admin=True)
        core._conn.commit()

        logger.info(f"Admin account created: {user.username}")

        return jsonify(
            AdminRegistrationResponse(
                message="Admin account created successfully",
                user=user
            ).model_dump()
        ), 201

    except sqlite3.IntegrityError:
        logger.warning(f"Admin registration failed (username exists): {data.username}")
        raise AuthenticationError(
            "Username already exists",
            {"username": data.username}
        )
    # Connection closes automatically via __del__


# ============================================================================
# Authentication Endpoints
# ============================================================================


@auth_bp.route("/auth/login", methods=["POST"])
@validate_request
def login(data: UserLogin):
    """
    Authenticate user and return JWT token.

    Accepts both JSON and form data (for HTML forms).

    Args:
        data: Login credentials with username and password

    Returns:
        Token response with JWT access token and user info

    Raises:
        AuthenticationError: If credentials are invalid

    Example request (JSON):
    ```json
    {
        "username": "admin",
        "password": "SecurePass123"
    }
    ```

    Example request (form data):
    ```
    username=admin&password=SecurePass123
    ```

    Example response:
    ```json
    {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "token_type": "bearer",
        "user": {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "username": "admin",
            "is_admin": true,
            "created_at": "2025-12-29T10:30:00Z"
        }
    }
    ```
    """
    core = get_core()
    # Verify credentials
    user = service.verify_credentials(core._conn, data.username, data.password)
    if user is None:
        logger.warning(f"Failed login attempt for username: {data.username}")
        raise AuthenticationError(
            "Invalid username or password",
            {"username": data.username}
        )

    # Generate JWT token
    access_token = token.generate_access_token(user)

    logger.info(f"Successful login: {user.username}")

    return jsonify(
        TokenResponse(
            access_token=access_token,
            token_type="bearer",
            user=user
        ).model_dump()
    ), 200
    # Connection closes automatically via __del__


@auth_bp.route("/auth/logout", methods=["POST"])
def logout():
    """
    Logout user (no-op for MVP).

    In the current MVP, JWT tokens are self-validating and stateless.
    The client should simply discard the token. This endpoint exists
    for API compatibility and future token blacklist support.

    Returns:
        Success message

    Example response:
    ```json
    {
        "message": "Logged out successfully"
    }
    ```
    """
    # For MVP: tokens are stateless, just return success
    # Future: implement token blacklist if needed
    return jsonify({"message": "Logged out successfully"}), 200


# ============================================================================
# User Profile Endpoints
# ============================================================================


@auth_bp.route("/auth/me", methods=["GET"])
def get_current_user():
    """
    Get current user info from JWT token.

    This endpoint requires authentication via the Authorization header:
    Authorization: Bearer <token>

    Returns:
        User info for authenticated user

    Raises:
        AuthenticationError: If token is missing or invalid

    Example request:
    ```
    GET /auth/me
    Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
    ```

    Example response:
    ```json
    {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "username": "admin",
        "is_admin": true,
        "created_at": "2025-12-29T10:30:00Z"
    }
    ```
    """
    # Authenticate via JWT (API keys not allowed for this endpoint)
    payload = _authenticate_jwt()

    # Get user from database
    core = get_core()
    user = service.get_user_by_id(core._conn, payload.sub)
    if user is None:
        raise AuthenticationError(
            "User not found",
            {"user_id": payload.sub}
        )

    return jsonify(user.model_dump()), 200
    # Connection closes automatically via __del__


# ============================================================================
# API Key Management Endpoints
# ============================================================================


@auth_bp.route("/api-keys/", methods=["GET"])
def list_api_keys():
    """
    List all API keys for the authenticated user.

    Requires authentication via JWT token.
    Full API keys are never shown in the list (only prefix).

    Returns:
        List of API key responses (without full keys)

    Raises:
        AuthenticationError: If token is missing or invalid

    Example request:
    ```
    GET /api-keys/
    Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
    ```

    Example response:
    ```json
    [
        {
            "id": "660e8400-e29b-41d4-a716-446655440000",
            "name": "claude-code",
            "prefix": "mg_sk_agent_",
            "expires_at": null,
            "created_at": "2025-12-29T10:30:00Z",
            "last_seen": "2025-12-29T15:45:00Z",
            "revoked_at": null
        }
    ]
    ```
    """
    # Authenticate via JWT (API keys not allowed for listing API keys)
    payload = _authenticate_jwt()

    # List API keys for user
    core = get_core()
    api_keys_list = api_keys.list_api_keys(core._conn, payload.sub)

    return jsonify([key.model_dump() for key in api_keys_list]), 200
    # Connection closes automatically via __del__


@auth_bp.route("/api-keys/", methods=["POST"])
@validate_request
def create_api_key(data: APIKeyCreate):
    """
    Create a new API key for the authenticated user.

    Requires authentication via JWT token.
    The full API key is only shown once in the response.

    Args:
        data: API key creation data with name and optional expires_at

    Returns:
        Created API key response with full key (only shown once)

    Raises:
        AuthenticationError: If token is missing or invalid

    Example request:
    ```json
    {
        "name": "claude-code",
        "expires_at": "2026-12-31T23:59:59Z"
    }
    ```

    Example response:
    ```json
    {
        "id": "660e8400-e29b-41d4-a716-446655440000",
        "name": "claude-code",
        "key": "mg_sk_agent_9a2b8c7d...",
        "prefix": "mg_sk_agent_",
        "expires_at": "2026-12-31T23:59:59Z",
        "created_at": "2025-12-29T10:30:00Z",
        "last_seen": null,
        "revoked_at": null
    }
    ```
    """
    # Authenticate via JWT (API keys not allowed for creating API keys)
    payload = _authenticate_jwt()

    # Create API key
    core = get_core()
    api_key = api_keys.create_api_key(core._conn, payload.sub, data)
    core._conn.commit()

    logger.info(f"API key created: {api_key.name} for user {payload.sub}")

    return jsonify(api_key.model_dump()), 201


@auth_bp.route("/api-keys/<api_key_id>", methods=["DELETE"])
def revoke_api_key(api_key_id: str):
    """
    Revoke an API key for the authenticated user (soft delete).

    Requires authentication via JWT token.
    Sets the revoked_at timestamp to deactivate the key.

    Args:
        api_key_id: API key UUID to revoke

    Returns:
        Success message

    Raises:
        AuthenticationError: If token is missing or invalid
        ResourceNotFound: If API key doesn't exist or doesn't belong to user

    Example request:
    ```
    DELETE /api-keys/660e8400-e29b-41d4-a716-446655440000
    Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
    ```

    Example response:
    ```json
    {
        "message": "API key revoked successfully"
    }
    ```
    """
    from ...exceptions import ResourceNotFound

    # Authenticate via JWT (API keys not allowed for revoking API keys)
    payload = _authenticate_jwt()

    # Revoke API key
    core = get_core()
    success = api_keys.revoke_api_key(core._conn, api_key_id, payload.sub)
    core._conn.commit()

    if not success:
        raise ResourceNotFound(
            "API key not found",
            {"api_key_id": api_key_id}
        )

    logger.info(f"API key revoked: {api_key_id} by user {payload.sub}")

    return jsonify({"message": "API key revoked successfully"}), 200
    # Connection closes automatically via __del__


# ============================================================================
# Test Endpoint for @auth_required Decorator
# ============================================================================


@auth_bp.route("/auth/test-require-auth", methods=["GET"])
@decorators.auth_required
def test_require_auth():
    """
    Test endpoint for @auth_required decorator.

    Returns authenticated user info from flask.g.
    Only used for testing authentication middleware.
    """
    from flask import g

    return jsonify({
        "user_id": g.user_id,
        "username": g.username,
        "is_admin": g.is_admin,
        "auth_method": g.auth_method
    }), 200
