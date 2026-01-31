"""API v1 endpoints for MemoGarden.

This module provides the ApiV1 blueprint that aggregates all v1 resources:
- Core: Transactions, Recurrences (mutable entities)
- Soil: Documents, Artifacts (immutable items, future)

The ApiV1 blueprint is registered in main.py and provides a central point
for applying security decorators and middleware to all v1 endpoints.

All API v1 endpoints require authentication via JWT token or API key.
"""

from flask import Blueprint

from ..middleware.decorators import _authenticate_request
from ..exceptions import AuthenticationError
from .core import recurrences, transactions

# Create the ApiV1 blueprint
api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


# ============================================================================
# Authentication Middleware (ApiV1-level)
# ============================================================================


@api_v1_bp.before_request
def authenticate():
    """
    Require authentication for all API v1 endpoints.

    This before_request handler runs before every endpoint in the ApiV1 blueprint,
    ensuring all API requests are authenticated via JWT token or API key.

    Delegates to the shared _authenticate_request() function to avoid
    code duplication with the @auth_required decorator.

    Raises:
        AuthenticationError: If no valid authentication provided
    """
    # Call shared authentication logic
    _authenticate_request()


# Register Core blueprints under ApiV1
# Note: each blueprint has url_prefix, so full paths will be /api/v1/transactions, /api/v1/recurrences
api_v1_bp.register_blueprint(transactions.transactions_bp)
api_v1_bp.register_blueprint(recurrences.recurrences_bp)

# Soil blueprints will be registered here when document storage is implemented

__all__ = ["api_v1_bp"]
