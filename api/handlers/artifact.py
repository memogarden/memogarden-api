"""Artifact delta verb handlers for Project Studio.

Session 17: Artifact Delta Operations
Implements Semantic API verbs for artifact delta operations:
- commit_artifact: Apply delta operations with optimistic locking
- get_artifact_at_commit: Retrieve artifact state at commit
- diff_commits: Compare two commits with line-by-line diff

Session 20B: Event Integration
Publishes SSE events for artifact delta operations:
- artifact_delta: Published when artifact is modified via commit
"""

import logging

from system.core import get_core
from system.exceptions import ConflictError, PermissionDenied
from utils import uid

from ..events import publish_artifact_delta
from ..middleware.scope_access import (
    active_scope_for,
    enforcement_enabled,
    item_visible,
    requester_has_all_scopes,
    row_labels,
)
from ..schemas.semantic import (
    CommitArtifactRequest,
    DiffCommitsRequest,
    GetArtifactAtCommitRequest,
)
from .decorators import with_audit

logger = logging.getLogger(__name__)


def _add_core_prefix(entity: dict) -> dict:
    """Add core_ prefix to entity UUID fields.

    Args:
        entity: Entity dict from Core API (has parsed JSON data)

    Returns:
        Entity dict with prefixed UUIDs
    """
    result = entity.copy()

    # Add core_ prefix to UUID fields
    result["uuid"] = uid.add_core_prefix(entity["uuid"])

    if entity.get("superseded_by"):
        result["superseded_by"] = uid.add_core_prefix(entity["superseded_by"])

    if entity.get("group_id"):
        result["group_id"] = uid.add_core_prefix(entity["group_id"])

    if entity.get("derived_from"):
        result["derived_from"] = uid.add_core_prefix(entity["derived_from"])

    return result


# ============================================================================
# Artifact Delta Verb Handlers
# ============================================================================

@with_audit
def handle_commit_artifact(request: CommitArtifactRequest, actor: str) -> dict:
    """Handle commit_artifact verb - apply delta operations to artifact.

    Per Project Studio spec (§6.2):
    - Parses and applies delta operations (+, -, ~, >)
    - Creates ArtifactDelta Item in Soil for audit trail
    - Uses optimistic locking via hash-based conflict detection
    - Creates triggers relation from source Message

    Session 20B: Publishes artifact_delta SSE event for real-time updates.

    Args:
        request: Validated CommitArtifactRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with new_hash, new_content, delta_uuid, line_count

    Raises:
        ConflictError: If based_on_hash doesn't match current (optimistic lock failure)
        ResourceNotFound: If artifact doesn't exist
        ValueError: If delta operations are invalid
    """
    with get_core() as core:
        # RFC-011 §4.5 write semantics (api#100): commit_artifact mutates
        # the artifact's content — strict frame gate on its current labels,
        # the edit precedent. Visibility never relaxes a write, so no
        # public-visibility short-circuit here even though these are the
        # same labels the read paths relax on (ratified 2026-11-19).
        # Flag-off and sovereign callers skip the gate (§8 / §4.10). The
        # fetch doubles as the existence check, so a missing artifact
        # still 404s before anything is written.
        if enforcement_enabled() and not requester_has_all_scopes():
            current = core.entity.get_by_id(uid.strip_prefix(request.artifact))
            artifact_scopes = set((current.get("data") or {}).get("scopes") or [])
            if not item_visible(sorted(artifact_scopes), active_scope_for(actor)):
                raise PermissionDenied(
                    "Artifact is in a scope you are not inside",
                    {"artifact": request.artifact,
                     "item_scopes": sorted(artifact_scopes),
                     "code": "scope_write_denied"},
                )

        try:
            result = core.artifact.commit_delta(
                artifact_uuid=request.artifact,
                ops_string=request.ops,
                references=request.references,
                based_on_hash=request.based_on_hash,
                source_message_uuid=request.source_message,
            )

            # Session 20B: Publish SSE event for artifact delta
            # Extract scope UUID from artifact for event routing
            artifact_entity = core.entity.get_by_id(uid.strip_prefix(request.artifact))
            scope_uuid = artifact_entity.get("data", {}).get("scope_uuid") if artifact_entity else None

            publish_artifact_delta(
                artifact_uuid=request.artifact,
                commit_hash=result["new_hash"],
                ops=request.ops,
                actor=actor,
                scope_uuid=scope_uuid,
                # RFC-011 §4.9(5): judged on the artifact's labels as they
                # stand after this commit (the row was re-read above).
                item_labels={
                    request.artifact: row_labels(
                        artifact_entity.get("data") if artifact_entity else None)},
            )

            # delta_uuid already has soil_ prefix from Soil
            # artifact_uuid already has core_ prefix from artifact.py
            return result

        except ConflictError as e:
            # Convert to ValueError for consistent 409 Conflict response
            raise ValueError(str(e)) from e


@with_audit
def handle_get_artifact_at_commit(request: GetArtifactAtCommitRequest, actor: str) -> dict:
    """Handle get_artifact_at_commit verb - retrieve artifact at commit.

    Per Project Studio spec (§6.2):
    - Returns artifact content at specific commit hash
    - For MVP: returns current state if commit matches current
    - Historical reconstruction deferred to future session

    Args:
        request: Validated GetArtifactAtCommitRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with content, hash, line_count, at_commit

    Raises:
        ResourceNotFound: If artifact doesn't exist
        ValueError: If commit_hash is invalid
    """
    with get_core() as core:
        result = core.artifact.get_at_commit(
            artifact_uuid=request.artifact,
            commit_hash=request.commit_hash,
        )

        # artifact_uuid already has core_ prefix from artifact.py
        return result


@with_audit
def handle_diff_commits(request: DiffCommitsRequest, actor: str) -> dict:
    """Handle diff_commits verb - compare two artifact commits.

    Per Project Studio spec (§6.2):
    - Computes line-by-line diff between two commits
    - Returns structured diff for UI rendering (three-way merge support)

    Args:
        request: Validated DiffCommitsRequest
        actor: Authenticated user/agent UUID

    Returns:
        dict with artifact_uuid, commit_a, commit_b, changes array

    Raises:
        ResourceNotFound: If artifact doesn't exist
        ValueError: If either commit hash is invalid
    """
    with get_core() as core:
        result = core.artifact.diff_commits(
            artifact_uuid=request.artifact,
            commit_a=request.commit_a,
            commit_b=request.commit_b,
        )

        # artifact_uuid already has core_ prefix from artifact.py
        return result


__all__ = [
    "handle_commit_artifact",
    "handle_get_artifact_at_commit",
    "handle_diff_commits",
]
