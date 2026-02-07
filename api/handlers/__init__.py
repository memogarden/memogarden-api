"""Semantic API verb handlers.

This module provides handler functions for each Semantic API verb.
Each handler takes a validated request and returns a result dict.

Handler signature:
    def handle_verb(request: ValidatedRequest, actor: str) -> dict[str, Any]

Returns result dict that gets wrapped in the response envelope.
"""

from .core import handle_create, handle_edit, handle_forget, handle_get, handle_query

__all__ = [
    "handle_create",
    "handle_edit",
    "handle_forget",
    "handle_get",
    "handle_query",
]
