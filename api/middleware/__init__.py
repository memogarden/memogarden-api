"""Authentication module for MemoGarden Core.

This module provides authentication and authorization functionality:
- Schema validation for auth operations
- JWT token generation and validation
- Password hashing and verification
- Authentication middleware for protected endpoints
- User and API key management
- JSON API endpoints and HTML UI pages

Auth API endpoints (top-level routes, not under /api/v1/):
- POST /admin/register - Create admin account (localhost only)
- POST /auth/login - Authenticate and return JWT token
- POST /auth/logout - Revoke current token
- GET /auth/me - Get current user info
- GET /api-keys/ - List API keys for current user
- POST /api-keys/ - Create new API key
- DELETE /api-keys/:id - Revoke API key

Auth UI pages:
- GET /admin/register - Admin setup page (localhost only)
- GET /login - Login page
- GET /api-keys - API key management page
- GET /api-keys/new - Create new API key page
- GET /settings - User settings page
- GET / - Index page (redirects to /api-keys or /login)
"""

from . import api, api_keys, decorators, schemas, service, token, ui
from .decorators import _authenticate_jwt, auth_required

__all__ = ["schemas", "service", "token", "api_keys", "decorators", "api", "ui", "auth_required", "_authenticate_jwt"]
