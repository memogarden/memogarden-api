"""
Authentication Pydantic schemas for API request/response validation.

These schemas define the API contract for authentication operations:
- User management (registration, login, profile)
- API key management (create, list, revoke)
- JWT token handling

Password requirements:
- Minimum length: 8 characters
- Must contain: at least one letter (a-zA-Z)
- Must contain: at least one digit (0-9)
- Recommended: special characters and mixed case for better security
"""

import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ============================================================================
# User Schemas
# ============================================================================


class UserBase(BaseModel):
    """Shared fields between UserCreate and UserResponse."""

    username: str = Field(..., description="Username (case-insensitive, stored as lowercase)")


class UserCreate(UserBase):
    """
    Request body for creating a user (admin registration).

    Password requirements:
    - Minimum 8 characters
    - At least one letter (a-zA-Z)
    - At least one digit (0-9)

    Example:
    ```json
    {
        "username": "admin",
        "password": "SecurePass123"
    }
    ```
    """

    password: Annotated[
        str,
        Field(min_length=8, description="Password (min 8 characters, must contain letter and digit)"),
    ]

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password meets security requirements."""
        if not re.search(r"[A-Za-z]", v):
            raise ValueError("Password must contain at least one letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate and normalize username."""
        if not re.match(r"^[A-Za-z0-9_-]+$", v):
            raise ValueError(
                "Username can only contain letters, numbers, underscores, and hyphens"
            )
        return v.lower()


class UserLogin(BaseModel):
    """
    Request body for user login.

    Accepts username and password credentials.
    Both JSON and form data formats are supported.

    Example:
    ```json
    {
        "username": "admin",
        "password": "SecurePass123"
    }
    ```
    """

    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class UserResponse(UserBase):
    """
    API response with user data and entity metadata.

    Includes user fields plus entity metadata from the registry.
    Password hash is never exposed in API responses.

    Example:
    ```json
    {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "username": "admin",
        "is_admin": true,
        "created_at": "2025-12-29T10:30:00Z"
    }
    ```
    """

    id: str = Field(..., description="UUID of the user")
    is_admin: bool = Field(..., description="Admin status (true if admin, false if regular user)")
    created_at: datetime = Field(..., description="When the user was created (ISO 8601 UTC)")

    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# API Key Schemas
# ============================================================================


class APIKeyBase(BaseModel):
    """Shared fields between APIKeyCreate and APIKeyResponse."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Descriptive name for the API key (e.g., 'claude-code', 'custom-script')",
    )


class APIKeyCreate(APIKeyBase):
    """
    Request body for creating an API key.

    Optionally specify expiration date. If expires_at is omitted or null,
    the API key does not expire.

    Optionally bind the key to an Agent entity (RFC-010): bound keys
    attribute their operations to the agent (actor = agent entity uuid,
    actor_type = "agent") instead of the bare username. The agent must
    already exist (POST /agents) and be owned by the caller.

    Example:
    ```json
    {
        "name": "claude-code",
        "expires_at": "2026-12-31T23:59:59Z",
        "agent_id": "core_6b0f1e2a-..."
    }
    ```
    """

    expires_at: datetime | None = Field(
        default=None, description="Optional expiration timestamp (ISO 8601 UTC, null for no expiry)"
    )
    agent_id: str | None = Field(
        default=None,
        description="Optional Agent entity uuid (core_...) to bind this key to (RFC-010)",
    )


class APIKeyUpdate(BaseModel):
    """
    Request body for updating an API key (PATCH /api-keys/<id>, RFC-010).

    agent_id semantics follow model_fields_set:
    - field absent: no change
    - field null: unbind (attribution reverts to the owning username)
    - field set: bind / re-bind to the given Agent entity
    """

    agent_id: str | None = Field(
        default=None,
        description="Agent entity uuid to bind, null to unbind; absent = no change",
    )


class APIKeyResponse(APIKeyBase):
    """
    API response for a single API key (full key shown only on creation).

    The full API key is only included in the response when the key is first created.
    For all subsequent operations (list, get), only the prefix is shown.

    Example (on creation, includes full key):
    ```json
    {
        "id": "660e8400-e29b-41d4-a716-446655440000",
        "name": "claude-code",
        "key": "mg_sk_agent_abc123def456...",
        "prefix": "mg_sk_agent_",
        "expires_at": "2026-12-31T23:59:59Z",
        "created_at": "2025-12-29T10:30:00Z",
        "last_seen": null,
        "revoked_at": null
    }
    ```
    """

    id: str = Field(..., description="UUID of the API key")
    key: str | None = Field(
        default=None,
        description="Full API key (only shown on creation, null in list responses)",
    )
    prefix: str = Field(..., description="API key prefix for display (e.g., 'mg_sk_agent_')")
    agent_id: str | None = Field(
        default=None,
        description="Bound Agent entity uuid (core_...), null if unbound (RFC-010)")
    expires_at: datetime | None = Field(
        default=None, description="Expiration timestamp (ISO 8601 UTC, null if no expiry)")
    created_at: datetime = Field(..., description="When the API key was created (ISO 8601 UTC)")
    last_seen: datetime | None = Field(
        default=None, description="Last time this key was used (ISO 8601 UTC, null if never used)")
    revoked_at: datetime | None = Field(
        default=None, description="When the key was revoked (ISO 8601 UTC, null if active)")
    model_config = ConfigDict(from_attributes=True)


class APIKeyListResponse(APIKeyBase):
    """
    API response for listing API keys (full key never shown).

    Used in list operations where the full API key must not be exposed.
    Only shows the prefix for identification purposes.

    Example (in list, no full key):
    ```json
    {
        "id": "660e8400-e29b-41d4-a716-446655440000",
        "name": "claude-code",
        "prefix": "mg_sk_agent_",
        "expires_at": "2026-12-31T23:59:59Z",
        "created_at": "2025-12-29T10:30:00Z",
        "last_seen": "2025-12-29T15:45:00Z",
        "revoked_at": null
    }
    ```
    """

    id: str = Field(..., description="UUID of the API key")
    prefix: str = Field(..., description="API key prefix for display (e.g., 'mg_sk_agent_')")
    agent_id: str | None = Field(
        default=None,
        description="Bound Agent entity uuid (core_...), null if unbound (RFC-010)")
    expires_at: datetime | None = Field(
        default=None, description="Expiration timestamp (ISO 8601 UTC, null if no expiry)")
    created_at: datetime = Field(..., description="When the API key was created (ISO 8601 UTC)")
    last_seen: datetime | None = Field(
        default=None, description="Last time this key was used (ISO 8601 UTC, null if never used)")
    revoked_at: datetime | None = Field(
        default=None, description="When the key was revoked (ISO 8601 UTC, null if active)")
    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# Agent Schemas (RFC-010)
# ============================================================================


class AgentCreate(BaseModel):
    """
    Request body for minting an Agent entity (POST /agents, RFC-010).

    Identity issuance is explicit and operator-gated: this endpoint is
    JWT-only, and no code path in authentication ever creates agents.

    Example:
    ```json
    {
        "label": "claude-code",
        "description": "ZCode sessions on the laptop"
    }
    ```
    """

    label: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Operator-facing name; unique among Agent entities",
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Free text: what this agent is and where it runs",
    )


class AgentResponse(BaseModel):
    """
    API response for a minted Agent entity.

    agent_id is the canonical identity: it is the `actor` recorded on
    Action facts for operations performed with bound keys (RFC-010).

    Example:
    ```json
    {
        "agent_id": "core_6b0f1e2a-...",
        "label": "claude-code",
        "description": "ZCode sessions on the laptop",
        "owner": "550e8400-...",
        "created_at": "2026-11-06T09:00:00Z"
    }
    ```
    """

    agent_id: str = Field(..., description="Agent entity uuid (core_...)")
    label: str = Field(..., description="Operator-facing name")
    description: str | None = Field(default=None, description="Free-text description")
    owner: str = Field(..., description="Owning user id")
    created_at: datetime = Field(..., description="When the agent was minted (ISO 8601 UTC)")


class AgentListResponse(AgentResponse):
    """
    API response for listing agents (management view).

    Adds the prefixes of keys currently bound to the agent. Agents as data
    are also readable through the Semantic API like any entity.

    Example:
    ```json
    {
        "agent_id": "core_6b0f1e2a-...",
        "label": "claude-code",
        "description": "ZCode sessions on the laptop",
        "owner": "550e8400-...",
        "created_at": "2026-11-06T09:00:00Z",
        "key_prefixes": ["mg_sk_agent_9a2b"],
        "bound_key_count": 1
    }
    ```
    """

    key_prefixes: list[str] = Field(
        default_factory=list, description="Prefixes of active keys bound to this agent")
    bound_key_count: int = Field(default=0, description="Number of active keys bound")


# ============================================================================
# JWT Token Schemas
# ============================================================================


class TokenPayload(BaseModel):
    """
    Internal JWT token payload for validation and decoding.

    This schema represents the claims stored in the JWT token:
    - sub: User ID (UUID)
    - username: Username for display
    - is_admin: Admin status for authorization checks
    - exp: Token expiration timestamp (Unix timestamp)

    This is used internally by the auth service, not exposed in API responses.
    """

    sub: str = Field(..., description="User ID (UUID)")
    username: str = Field(..., description="Username")
    is_admin: bool = Field(..., description="Admin status")
    exp: int = Field(..., description="Token expiration timestamp (Unix timestamp)")
    iat: int = Field(..., description="Token issued at timestamp (Unix timestamp)")


class TokenResponse(BaseModel):
    """
    API response for successful authentication (login or token refresh).

    Returns the JWT access token and basic user information.

    Example:
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

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type (always 'bearer')")
    user: UserResponse = Field(..., description="User information")


# ============================================================================
# Admin Registration Schemas
# ============================================================================


class AdminRegistrationResponse(BaseModel):
    """
    API response for successful admin registration.

    Includes confirmation message and user details.

    Example:
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

    message: str = Field(..., description="Success message")
    user: UserResponse = Field(..., description="Created user information")
