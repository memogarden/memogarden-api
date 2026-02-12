"""Semantic API verb handlers.

This module provides handler functions for each Semantic API verb.
Each handler takes a validated request and returns a result dict.

Handler signature:
    def handle_verb(request: ValidatedRequest, actor: str) -> dict[str, Any]

Returns result dict that gets wrapped in the response envelope.
"""

from .core import (
    handle_create,
    handle_edit,
    handle_forget,
    handle_get,
    handle_query,
    handle_link,
    handle_unlink,
    handle_edit_relation,
    handle_get_relation,
    handle_query_relation,
    handle_explore,
    handle_track,
    handle_search,
    handle_enter,
    handle_leave,
    handle_focus,
)
from .artifact import (
    handle_commit_artifact,
    handle_get_artifact_at_commit,
    handle_diff_commits,
)
from .conversation import (
    handle_fold,
    handle_get_conversation,
)
from .soil import handle_add, handle_amend, handle_get_fact, handle_query_facts

__all__ = [
    # Core bundle
    "handle_create",
    "handle_edit",
    "handle_forget",
    "handle_get",
    "handle_query",
    # Relations bundle
    "handle_link",
    "handle_unlink",
    "handle_edit_relation",
    "handle_get_relation",
    "handle_query_relation",
    "handle_explore",
    "handle_track",
    # Context bundle
    "handle_enter",
    "handle_leave",
    "handle_focus",
    # Search
    "handle_search",
    # Soil bundle
    "handle_add",
    "handle_amend",
    "handle_get_fact",
    "handle_query_facts",
    # Artifact delta bundle (Session 17)
    "handle_commit_artifact",
    "handle_get_artifact_at_commit",
    "handle_diff_commits",
    # Conversation bundle (Session 18)
    "handle_fold",
    "handle_get_conversation",
]
