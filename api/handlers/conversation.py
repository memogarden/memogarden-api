"""Conversation verb handlers for Project Studio.

Session 18: Fold Verb
Implements Semantic API verb for conversation fold operation:
- fold: Collapse conversation branch with summary
"""

import logging

from system.core import get_core
from system.exceptions import ResourceNotFound
from .decorators import with_audit

from ..schemas.semantic import FoldRequest

logger = logging.getLogger(__name__)


# ============================================================================
# Conversation Verb Handlers
# ============================================================================

@with_audit
def handle_fold(request: FoldRequest, actor: str) -> dict:
    """Handle fold verb - collapse conversation branch with summary.

    Per Project Studio spec (§6.3):
    - Creates summary object attached to ConversationLog
    - Marks branch as folded (collapsed=true)
    - Branch remains visible and can continue (messages can be appended after fold)

    Per RFC-005: fold is a single-word verb applicable to any entity/fact.

    Args:
        request: Validated FoldRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with log_uuid, summary, and collapsed status

    Raises:
        ResourceNotFound: If ConversationLog doesn't exist
        ValueError: If summary_content is empty or invalid
    """
    with get_core() as core:
        result = core.conversation.fold(
            log_uuid=request.target,
            summary_content=request.summary_content,
            author=request.author,
            fragment_ids=request.fragment_ids if request.fragment_ids else None,
        )

        # log_uuid already has core_ prefix from conversation.py
        return {
            "log_uuid": result.log_uuid,
            "summary": result.summary,
            "collapsed": result.collapsed,
        }


@with_audit
def handle_get_conversation(request, actor: str) -> dict:
    """Handle get operation for ConversationLog entities.

    Args:
        request: Validated GetRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with ConversationLog data

    Raises:
        ResourceNotFound: If ConversationLog doesn't exist
    """
    with get_core() as core:
        result = core.conversation.get(log_uuid=request.target)
        return result


__all__ = [
    "handle_fold",
    "handle_get_conversation",
]
