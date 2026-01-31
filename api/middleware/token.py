"""JWT token generation and validation service.

This module provides JWT token operations for authentication:
- Token generation with user identity claims
- Token validation and decoding
- Token expiry handling

Uses PyJWT for token encoding/decoding with HS256 algorithm.
"""

from datetime import timedelta

import jwt

from .schemas import TokenPayload, UserResponse
from ..config import settings
from system.utils import isodatetime

# ============================================================================
# Token Generation
# ============================================================================


def generate_access_token(user: UserResponse) -> str:
    """
    Generate a JWT access token for a user.

    The token includes the following claims:
    - sub: User ID (UUID)
    - username: Username for display
    - is_admin: Admin status for authorization checks
    - iat: Issued at timestamp (Unix timestamp)
    - exp: Expiration timestamp (Unix timestamp, default 30 days from now)

    Args:
        user: User response with id, username, is_admin, created_at

    Returns:
        Encoded JWT access token string

    Example:
    ```python
    user = UserResponse(
        id="550e8400-e29b-41d4-a716-446655440000",
        username="admin",
        is_admin=True,
        created_at=datetime(2025, 12, 29, 10, 30, 0)
    )
    token = generate_access_token(user)
    # Returns: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    ```
    """
    now_ts = isodatetime.now_unix()
    expiry_ts = now_ts + (settings.jwt_expiry_days * 24 * 60 * 60)

    payload = {
        "sub": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "iat": now_ts,
        "exp": expiry_ts,
    }

    token = jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")
    return token


# ============================================================================
# Token Validation
# ============================================================================


def validate_access_token(token: str) -> TokenPayload:
    """
    Validate and decode a JWT access token.

    Verifies the token signature using HS256 algorithm and the secret key.
    Raises jwt.InvalidTokenError if the token is invalid, expired, or malformed.

    Args:
        token: JWT access token string

    Returns:
        TokenPayload with decoded claims (sub, username, is_admin, exp, iat)

    Raises:
        jwt.InvalidTokenError: Token is invalid, expired, or malformed
        jwt.ExpiredSignatureError: Token has expired (subclass of InvalidTokenError)
        jwt.DecodeError: Token cannot be decoded (subclass of InvalidTokenError)

    Example:
    ```python
    try:
        payload = validate_access_token(token)
        print(f"User ID: {payload.sub}")
        print(f"Username: {payload.username}")
        print(f"Admin: {payload.is_admin}")
    except jwt.ExpiredSignatureError:
        print("Token expired")
    except jwt.InvalidTokenError:
        print("Invalid token")
    ```
    """
    decoded = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=["HS256"],
        options={"require": ["sub", "username", "is_admin", "exp", "iat"]},
    )

    return TokenPayload(**decoded)


def decode_token_no_validation(token: str) -> dict:
    """
    Decode a JWT token without verifying the signature.

    WARNING: This is for debugging/inspection only. The token contents
    are NOT verified and may be forged. Use validate_access_token() for
    actual authentication.

    Args:
        token: JWT access token string

    Returns:
        Raw decoded token payload as dict

    Example:
    ```python
    # For debugging only
    payload = decode_token_no_validation(token)
    print(f"Token claims: {payload}")
    ```
    """
    return jwt.decode(token, options={"verify_signature": False})


# ============================================================================
# Token Introspection
# ============================================================================


def get_token_expiry_remaining(token: str) -> timedelta | None:
    """
    Get remaining time until token expires.

    Returns None if token is invalid or already expired.

    Args:
        token: JWT access token string

    Returns:
        Timedelta until expiry, or None if token is invalid/expired

    Example:
    ```python
    remaining = get_token_expiry_remaining(token)
    if remaining:
        print(f"Token expires in {remaining.days} days")
    else:
        print("Token is invalid or expired")
    ```
    """
    try:
        payload = validate_access_token(token)
        now_ts = isodatetime.now_unix()
        remaining_seconds = payload.exp - now_ts
        return timedelta(seconds=remaining_seconds)
    except jwt.InvalidTokenError:
        return None


def is_token_expired(token: str) -> bool:
    """
    Check if a token has expired.

    Args:
        token: JWT access token string

    Returns:
        True if token is expired or invalid, False if token is valid

    Example:
    ```python
    if is_token_expired(token):
        print("Token has expired, please login again")
    ```
    """
    remaining = get_token_expiry_remaining(token)
    return remaining is None or remaining.total_seconds() <= 0
