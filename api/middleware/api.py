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

from flask import Blueprint, g, jsonify, request

from system.core import get_core
from system.exceptions import AuthenticationError, ResourceNotFound, ValidationError
from system.soil import Fact, generate_soil_uuid, get_soil
from utils import isodatetime, uid

from ..validation import validate_request
from . import api_keys, decorators, scope_access, service, token
from .decorators import _authenticate_jwt, _authenticate_request
from .schemas import (
    AdminRegistrationResponse,
    AgentCreate,
    APIKeyCreate,
    APIKeyUpdate,
    ChangePasswordRequest,
    GrantCreate,
    TokenResponse,
    UserCreate,
    UserLogin,
)

logger = logging.getLogger(__name__)


# Create blueprint
auth_bp = Blueprint("auth", __name__)


def _record_identity_action(operation: str, params: dict) -> None:
    """Write an Action fact for an identity management operation (RFC-010 INV-2).

    Identity issuance and binding are JWT-gated operator actions; this
    makes each one traceable in the same audit trail as Semantic API
    operations (actor_type "operator"). Committed immediately so the
    mint is visible before the endpoint returns.
    """
    now = isodatetime.now()
    item = Fact(
        uuid=generate_soil_uuid(),
        _type="Action",
        realized_at=now,
        canonical_at=now,
        data={
            "actor": getattr(g, "username", None),
            "actor_type": "operator",
            "operation": operation,
            "params": params,
            "context": None,
            "request_id": generate_soil_uuid(),
            "parent_action": None,
        },
    )
    with get_soil() as soil:
        soil.create_fact(item)


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
        raise AuthenticationError(  # noqa: B904
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
# Operator Account Security (RFC-015 §3.1)
# ============================================================================


@auth_bp.route("/auth/change-password", methods=["POST"])
@validate_request
def change_password(data: ChangePasswordRequest):
    """
    Rotate the authenticated operator's password (RFC-015 §3.1). JWT-gated.

    Until now the password could be set once at bootstrap and never changed
    (no endpoint existed) — a standing rotation-hygiene gap since the
    2026-11-05 bootstrap. The current password must be presented; the new
    one follows the registration policy (min 8 chars, letter + digit).

    An active JWT stays valid after rotation (stateless tokens, 30-day
    expiry) — log out other sessions by revoking their keys is not
    applicable to JWTs; the operator simply stops using them.

    Returns:
        200: Password updated (an identity Action fact is written, RFC-010 INV-2)

    Raises:
        AuthenticationError: Not a JWT session, or current password wrong
    """
    payload = _authenticate_jwt()

    core = get_core()
    row = core._conn.execute(
        "SELECT id, username, password_hash FROM users WHERE id = ?",
        (payload.sub,),
    ).fetchone()
    if row is None:
        raise AuthenticationError(
            "Unknown user for this session",
            {"username": getattr(payload, "username", None)},
        )

    if not service.verify_password(data.current_password, row["password_hash"]):
        logger.warning(f"Failed password change (wrong current password): {row['username']}")
        raise AuthenticationError(
            "Current password is incorrect",
            {"field": "current_password"},
        )

    core._conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (service.hash_password(data.new_password), row["id"]),
    )
    core._conn.commit()

    _record_identity_action(
        "change_password", {"username": row["username"]}
    )
    logger.info(f"Password changed for {row['username']}")
    return jsonify({"message": "Password updated"}), 200
    # Connection closes automatically via __del__


# ============================================================================
# User Profile Endpoints
# ============================================================================


@auth_bp.route("/auth/me", methods=["GET"])
def get_current_user():
    """
    Get the authenticated principal's identity (RFC-010 introspection).

    Accepts both authentication methods and returns a shape per method:

    - JWT (operator session): the user record plus actor_type "operator"
    - API key, unbound: actor_type "api_key", actor = owning username
    - API key, bound (RFC-010): actor_type "agent", actor = Agent entity
      uuid, plus the agent's label — the canonical identity an agent uses
      for provenance stamping

    Raises:
        AuthenticationError: If no valid authentication provided

    Example (JWT):
    ```json
    {
        "id": "550e8400-...",
        "username": "admin",
        "is_admin": true,
        "created_at": "2025-12-29T10:30:00Z",
        "actor_type": "operator",
        "actor": "admin"
    }
    ```

    Example (bound API key):
    ```json
    {
        "actor_type": "agent",
        "actor": "core_6b0f1e2a-...",
        "label": "claude-code",
        "key_prefix": "mg_sk_agent_9a2b",
        "username": "admin",
        "is_admin": true
    }
    ```
    """
    # RFC-010: identity introspection is available to both operators
    # (JWT) and agents (API key) — an agent discovers its canonical
    # identity here. Management endpoints below stay JWT-only.
    _authenticate_request()
    core = get_core()

    if g.auth_method == "api_key":
        key = api_keys.get_api_key_by_id(core._conn, g.api_key_id)
        result = {
            "actor_type": g.actor_type,
            "actor": g.actor,
            "key_prefix": key.prefix if key else None,
            "username": g.username,
            "is_admin": g.is_admin,
            # RFC-011 §6: the presenting key's scope grants ([] =
            # unscoped-only; the principal discovers its access here)
            "grants": g.grants or [],
        }
        if g.actor_type == "agent" and key is not None and key.agent_id:
            agent = api_keys.get_agent(core._conn, key.agent_id, g.user_id)
            result["label"] = agent.label
        # RFC-011 §4.5: the principal's active scope (access state)
        result["active_scope"] = _prefixed_or_none(scope_access.active_scope_for(g.actor))
        return jsonify(result), 200

    # JWT path: user record as before, plus RFC-010 attribution fields
    user = service.get_user_by_id(core._conn, g.user_id)
    if user is None:
        raise AuthenticationError(
            "User not found",
            {"user_id": g.user_id}
        )

    result = user.model_dump()
    result["actor_type"] = g.actor_type
    result["actor"] = g.actor
    # RFC-011 SG-11: operator sessions hold implicit all-grants
    result["grants"] = None
    result["active_scope"] = _prefixed_or_none(scope_access.active_scope_for(g.actor))
    return jsonify(result), 200
    # Connection closes automatically via __del__


def _prefixed_or_none(scope_uuid: str | None) -> str | None:
    """Add the core_ prefix to a plain uuid, passing None through."""
    return uid.add_core_prefix(scope_uuid) if scope_uuid else None


# ============================================================================
# Scope Grant Management (RFC-011 §6)
# ============================================================================


@auth_bp.route("/grants", methods=["POST"])
@validate_request
def create_grant(data: GrantCreate):
    """
    Issue a scope grant — operator action, JWT-gated (RFC-011 §6).

    Exactly one of key_id / agent_id. agent_id grants apply to all keys
    currently bound to that agent; future bindings inherit at bind time.
    Grant issuance is never implicit (RFC-010 issuance invariant): API
    keys cannot mint, modify, or revoke grants, including their own.

    Returns:
        201 with the created grant entries

    Raises:
        AuthenticationError: Not a JWT session
        ResourceNotFound: Unknown key/agent/scope
        ValidationError: Neither or both of key_id/agent_id
    """
    payload = _authenticate_jwt()

    if bool(data.key_id) == bool(data.agent_id):
        raise ValidationError(
            "Exactly one of key_id or agent_id is required",
            {"key_id": data.key_id, "agent_id": data.agent_id}
        )

    scope_uuid = uid.strip_prefix(data.scope_uuid)

    core = get_core()

    # The scope must exist as a Scope entity
    scope_row = core._conn.execute(
        "SELECT uuid FROM entity WHERE uuid = ? AND type = 'Scope'",
        (scope_uuid,)
    ).fetchone()
    if scope_row is None:
        raise ResourceNotFound("Scope not found", {"scope_uuid": data.scope_uuid})

    if data.key_id:
        key_id = uid.strip_prefix(data.key_id)
        grant = api_keys.grant_scope_to_key(
            core._conn, payload.sub, key_id, scope_uuid, data.expires_at
        )
        # key_id in the response carries the caller's prefixing
        grant = {**grant, "key_id": data.key_id}
        created = [grant]
    else:
        # agent_id passes through raw — grant_scope_to_agent canonicalizes
        created = api_keys.grant_scope_to_agent(
            core._conn, payload.sub, data.agent_id, scope_uuid, data.expires_at
        )
        created = [{**g, "agent_id": data.agent_id} for g in created]

    core._conn.commit()
    return jsonify({"grants": created, "count": len(created)}), 201
    # Connection closes automatically via __del__


@auth_bp.route("/grants", methods=["GET"])
def list_grants():
    """
    List scope grants for a key or an agent (RFC-011 §6). JWT-gated.

    Query params: exactly one of key_id / agent_id.

    Returns:
        List of grant entries with key attribution
    """
    payload = _authenticate_jwt()

    key_id = request.args.get("key_id")
    agent_id = request.args.get("agent_id")
    if bool(key_id) == bool(agent_id):
        raise ValidationError(
            "Exactly one of key_id or agent_id is required",
            {"key_id": key_id, "agent_id": agent_id}
        )

    core = get_core()
    grants = api_keys.list_grants(
        core._conn, payload.sub,
        key_id=uid.strip_prefix(key_id) if key_id else None,
        agent_id=agent_id,
    )
    return jsonify(grants), 200
    # Connection closes automatically via __del__


@auth_bp.route("/grants/<grant_id>", methods=["DELETE"])
def revoke_grant(grant_id: str):
    """
    Revoke a scope grant by id — operator action, JWT-gated (RFC-011 §6).

    Immediate for future operations (grants re-read per request); an
    in-flight session is not retroactively ejected (RFC-011 §7) — revoke
    the key itself for that.

    Returns:
        200 with the revoked grant entry
    """
    payload = _authenticate_jwt()

    core = get_core()
    result = api_keys.revoke_grant(core._conn, payload.sub, grant_id)
    core._conn.commit()
    return jsonify(result), 200
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


@auth_bp.route("/api-keys/<api_key_id>", methods=["PATCH"])
@validate_request
def update_api_key(data: APIKeyUpdate, api_key_id: str):
    """
    Bind, re-bind, or unbind an Agent entity on an API key (RFC-010).

    Requires authentication via JWT token (identity issuance and binding
    are explicit operator actions; key holders cannot modify bindings).

    agent_id semantics (model_fields_set):
    - absent: no change (400 if the body sets no fields)
    - null: unbind — attribution reverts to the owning username with
      actor_type "api_key"
    - set: bind / re-bind to that Agent entity

    Args:
        data: APIKeyUpdate payload
        api_key_id: API key to update

    Returns:
        Updated API key record (no secret material)

    Raises:
        AuthenticationError: If token is missing or invalid
        ResourceNotFound: Key or agent not found (404)
        ValidationError: Non-Agent target or empty body (400)
        PermissionDenied: Agent owned by another user (403)
        LockConflictError: Key is revoked (binding refused, 409)
    """
    payload = _authenticate_jwt()

    sets_agent = "agent_id" in data.model_fields_set
    sets_capabilities = "capabilities" in data.model_fields_set
    sets_description = "description" in data.model_fields_set
    if not (sets_agent or sets_capabilities or sets_description):
        raise ValidationError(
            "PATCH body must set agent_id (bind/unbind), capabilities "
            "(restricted key classes, E3), and/or description (api#105)",
            {"expected_fields": ["agent_id", "capabilities", "description"]}
        )

    core = get_core()
    key = None
    if sets_agent:
        key = api_keys.bind_agent_to_key(core._conn, api_key_id, payload.sub, data.agent_id)
    if sets_capabilities:
        key = api_keys.set_key_capabilities(
            core._conn, api_key_id, payload.sub, data.capabilities)
    if sets_description:
        key = api_keys.set_key_description(
            core._conn, api_key_id, payload.sub, data.description)
    core._conn.commit()

    # RFC-010 INV-2 / §5.3: (re-)attribution is explicit and auditable
    if sets_agent:
        _record_identity_action(
            "bind",
            {"api_key_id": api_key_id, "agent_id": data.agent_id},
        )
    if sets_capabilities:
        _record_identity_action(
            "set_capabilities",
            {"api_key_id": api_key_id, "capabilities": data.capabilities},
        )

    logger.info(
        f"API key updated: {api_key_id} "
        f"(agent_id={data.agent_id if sets_agent else 'unchanged'}, "
        f"capabilities={data.capabilities if sets_capabilities else 'unchanged'}) "
        f"by user {payload.sub}"
    )

    return jsonify(key.model_dump()), 200
    # Connection closes automatically via __del__


# ============================================================================
# Agent Identity Endpoints (RFC-010)
# ============================================================================


@auth_bp.route("/agents", methods=["POST"])
@validate_request
def create_agent(data: AgentCreate):
    """
    Mint an Agent entity (RFC-010 identity issuance).

    Requires authentication via JWT token — the ONLY creation path for
    agent identity (explicit, operator-gated; authentication never
    creates identity state, RFC-010 INV-1). Labels are unique among
    agents.

    Args:
        data: AgentCreate payload (label, optional description)

    Returns:
        AgentResponse with the canonical agent_id (core_...) — the uuid
        recorded as `actor` on Action facts for bound-key operations

    Raises:
        AuthenticationError: If token is missing or invalid
        LockConflictError: Label already in use (409)
        ValidationError: Schema violation (422 via validate_request)
    """
    payload = _authenticate_jwt()

    core = get_core()
    agent = api_keys.create_agent(core._conn, payload.sub, data)
    core._conn.commit()

    # RFC-010 INV-2: the mint is traceable to a JWT-authenticated operator
    _record_identity_action(
        "register",
        {"resource": "agent", "agent_id": agent.agent_id, "label": agent.label},
    )

    logger.info(f"Agent minted: {agent.label} ({agent.agent_id}) by user {payload.sub}")

    return jsonify(agent.model_dump()), 201
    # Connection closes automatically via __del__


@auth_bp.route("/agents", methods=["GET"])
def list_agents():
    """
    List Agent entities owned by the authenticated user (RFC-010).

    Requires authentication via JWT token. Management listing only —
    agents as semantic-layer data are readable through the Semantic API
    like any entity. Includes the prefixes and count of active keys
    bound to each agent.

    Returns:
        List of AgentListResponse

    Raises:
        AuthenticationError: If token is missing or invalid
    """
    payload = _authenticate_jwt()

    core = get_core()
    agents = api_keys.list_agents(core._conn, payload.sub)

    return jsonify([agent.model_dump() for agent in agents]), 200
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
