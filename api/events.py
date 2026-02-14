"""Server-Sent Events (SSE) endpoint for real-time updates.

Session 20A: SSE Infrastructure and Event Publishing

This module provides SSE support for observing real-time changes in MemoGarden:
- Artifact deltas (edits, commits)
- Message sends (conversation updates)
- Context frame changes (enter/leave/focus scope)
- Participant frame updates

Per Project Studio spec, observe mode allows operators to watch agent work
in real-time via SSE push notifications.
"""

import json
import logging
import queue as Queue
import threading
from dataclasses import dataclass, field
from typing import Dict, Set, Optional

from flask import Blueprint, Response, current_app, g, request, stream_with_context

from api.middleware.decorators import auth_required
from system.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# ============================================================================
# Event Types
# ============================================================================

# Valid SSE event types (immutable frozenset for safety)
EVENT_TYPES = frozenset({
    "artifact_delta",     # Artifact modified (commit, amend)
    "message_sent",       # New message in conversation
    "context_updated",    # ContextFrame containers changed
    "frame_updated",      # Participant head_item_uuid changed
    "scope_created",      # New scope created
    "scope_modified",     # Scope metadata changed
    "relation_created",    # New relation created
    "relation_modified",   # Relation edited/amended
})


# ============================================================================
# SSE Connection Management
# ============================================================================

@dataclass
class SSEConnection:
    """Represents an active SSE connection.

    Each connection has:
    - A unique client ID for debugging
    - The authenticated user's ID
    - Set of scope UUIDs to filter events
    - A queue for events targeting this connection
    """
    client_id: str
    user_id: str
    username: str
    subscribed_scopes: Set[str]
    queue: Queue.Queue = field(default_factory=Queue.Queue)

    def is_subscribed_to(self, scope_uuid: Optional[str]) -> bool:
        """Check if connection is subscribed to events for a scope.

        If scope_uuid is None (global event), all connections receive it.
        """
        if scope_uuid is None:
            return True
        return scope_uuid in self.subscribed_scopes


class SSEManager:
    """Manages active SSE connections and event broadcasting.

    Thread-safe implementation supporting:
    - Connection registration/unregistration
    - Event publishing to filtered connections
    - Keepalive comments to prevent timeouts

    Usage:
        sse_manager.publish("artifact_delta", {...}, scope_uuid="core_abc")

    The manager routes events only to connections subscribed to the target scope.
    """

    def __init__(self):
        self._connections: Dict[str, SSEConnection] = {}
        self._lock = threading.Lock()
        self._client_id_counter = 0

    def register(
        self,
        user_id: str,
        username: str,
        scopes: Set[str]
    ) -> tuple[str, SSEConnection]:
        """Register a new SSE connection.

        Args:
            user_id: Authenticated user's UUID
            username: Authenticated user's username
            scopes: Set of scope UUIDs to subscribe to

        Returns:
            Tuple of (client_id, connection)
        """
        with self._lock:
            self._client_id_counter += 1
            client_id = f"sse_{self._client_id_counter}"
            conn = SSEConnection(
                client_id=client_id,
                user_id=user_id,
                username=username,
                subscribed_scopes=scopes,
            )
            self._connections[client_id] = conn
            logger.info(
                f"SSE connection registered: {client_id} for user {username}, "
                f"subscribed to {len(scopes)} scope(s)"
            )
            return client_id, conn

    def unregister(self, client_id: str) -> Optional[SSEConnection]:
        """Remove an SSE connection.

        Args:
            client_id: Connection ID to remove

        Returns:
            The removed connection, or None if not found
        """
        with self._lock:
            conn = self._connections.pop(client_id, None)
            if conn:
                logger.info(
                    f"SSE connection unregistered: {client_id} "
                    f"(user: {conn.username})"
                )
            return conn

    def publish(
        self,
        event_type: str,
        data: dict,
        scope_uuid: Optional[str] = None
    ) -> int:
        """Publish event to relevant connections.

        Args:
            event_type: Type of event (must be in EVENT_TYPES)
            data: Event data payload (JSON-serializable)
            scope_uuid: Target scope UUID, or None for global events

        Returns:
            Number of connections the event was published to

        Raises:
            ValueError: If event_type is not recognized
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}")

        published_count = 0

        with self._lock:
            for conn in self._connections.values():
                if conn.is_subscribed_to(scope_uuid):
                    try:
                        conn.queue.put(
                            {"type": event_type, "data": data},
                            block=False
                        )
                        published_count += 1
                    except queue.Full:
                        logger.warning(
                            f"SSE queue full for {conn.client_id}, "
                            f"dropping event: {event_type}"
                        )

        logger.debug(
            f"SSE event published: {event_type} -> {published_count} connection(s)"
        )
        return published_count

    def get_connection_count(self) -> int:
        """Get current number of active connections."""
        with self._lock:
            return len(self._connections)

    def get_all_connections(self) -> list:
        """Get information about all active connections.

        Returns:
            List of connection info dicts with client_id, username, scope_count
        """
        with self._lock:
            return [
                {
                    "client_id": conn.client_id,
                    "username": conn.username,
                    "scope_count": len(conn.subscribed_scopes),
                }
                for conn in self._connections.values()
            ]


# Global SSE manager instance
sse_manager = SSEManager()


# ============================================================================
# SSE Blueprint
# ============================================================================

events_bp = Blueprint("events", __name__, url_prefix="/mg")


def _parse_scope_subscription(req) -> Set[str]:
    """Parse scope subscription from request parameters.

    Supports two formats:
    1. Query param: ?scopes=core_abc,core_def
    2. Empty (subscribe to all scopes - receives global events only)

    Args:
        req: Flask request object

    Returns:
        Set of scope UUIDs to subscribe to
    """
    scopes_param = req.args.get("scopes", "")
    if not scopes_param:
        return set()

    # Parse comma-separated scope UUIDs
    scopes = {s.strip() for s in scopes_param.split(",") if s.strip()}
    logger.debug(f"Parsed scope subscription: {scopes}")
    return scopes


@events_bp.route("/events", methods=["GET"])
@auth_required
def events_stream():
    """Server-Sent Events stream for real-time updates.

    Endpoint: GET /mg/events?scopes=core_abc,core_def

    Authentication:
        - JWT token via Authorization: Bearer <token>
        - API key via X-API-Key: <api_key>

    Query Parameters:
        scopes: Comma-separated list of scope UUIDs to subscribe to

    Response Format:
        Event-Source format with named events:

        event: artifact_delta
        data: {"artifact_uuid": "...", "commit_hash": "...", ...}

        event: message_sent
        data: {"log_uuid": "...", "message_uuid": "...", ...}

    Keepalive:
        Sends ": keepalive" comment every 30 seconds if no events

    Reconnection:
        Client should auto-reconnect on disconnect.
        Events sent during disconnect are not buffered (MVP limitation).
    """
    # Get authenticated user info from Flask g object
    # @auth_required decorator already authenticated the request
    user_id = g.user_id
    username = g.username

    # Parse scope subscription
    scopes = _parse_scope_subscription(request)

    # Register connection
    client_id, conn = sse_manager.register(user_id, username, scopes)

    logger.info(
        f"SSE stream opened: {client_id} for user {username}, "
        f"scopes={scopes if scopes else '(all)'}"
    )

    def generate():
        """Generator function for SSE stream."""
        # Use shorter timeout in testing to avoid long waits
        # Production uses 30s keepalive, tests use 3s
        timeout = 3 if current_app.config.get("TESTING") else 30

        try:
            while True:
                try:
                    # Block for up to timeout seconds waiting for event
                    event = conn.queue.get(timeout=timeout)

                    # Format SSE response
                    # Format: event: <type>\ndata: <json>\n\n
                    yield f"event: {event['type']}\n"
                    yield f"data: {json.dumps(event['data'])}\n\n"

                except Queue.Empty:
                    # Send keepalive comment to prevent timeout
                    # Format: : comment\n\n (ignored by clients)
                    yield ": keepalive\n\n"

        except GeneratorExit:
            # Client disconnected
            logger.debug(f"SSE stream closed by client: {client_id}")
        finally:
            # Clean up connection
            sse_manager.unregister(client_id)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Connection": "keep-alive",
        }
    )


@events_bp.route("/events/stats", methods=["GET"])
@auth_required
def events_stats():
    """Get SSE connection statistics.

    Endpoint: GET /mg/events/stats

    Returns current connection count and active subscriptions.

    Response:
        {
            "active_connections": 5,
            "connections": [
                {
                    "client_id": "sse_1",
                    "username": "user1",
                    "scope_count": 2
                },
                ...
            ]
        }
    """
    connections = sse_manager.get_all_connections()

    return {
        "active_connections": len(connections),
        "connections": connections,
    }


# ============================================================================
# Event Publishing Helpers
# ============================================================================

def publish_event(
    event_type: str,
    data: dict,
    scope_uuid: Optional[str] = None
) -> int:
    """Publish event to all clients subscribed to scope.

    This is the main entry point for Semantic API handlers to publish events.

    Args:
        event_type: Type of event (must be in EVENT_TYPES)
        data: Event data payload (JSON-serializable dict)
        scope_uuid: Target scope UUID, or None for global events

    Returns:
        Number of connections the event was published to

    Raises:
        ValueError: If event_type is not recognized

    Example:
        from api.events import publish_event

        publish_event(
            "artifact_delta",
            {
                "artifact_uuid": artifact.uuid,
                "commit_hash": delta.commit_hash,
                "ops": delta.ops,
            },
            scope_uuid=artifact.data.get("scope_uuid")
        )
    """
    return sse_manager.publish(event_type, data, scope_uuid)


def publish_artifact_delta(
    artifact_uuid: str,
    commit_hash: str,
    ops: str,
    actor: str,
    scope_uuid: Optional[str] = None,
) -> int:
    """Publish artifact_delta event.

    Convenience wrapper for artifact commit operations.

    Args:
        artifact_uuid: UUID of modified artifact
        commit_hash: New commit hash after delta
        ops: Delta operations applied
        actor: User/agent who made the change
        scope_uuid: Scope UUID for routing (optional)

    Returns:
        Number of connections event was published to
    """
    return publish_event(
        "artifact_delta",
        {
            "artifact_uuid": artifact_uuid,
            "commit_hash": commit_hash,
            "ops": ops,
            "actor": actor,
        },
        scope_uuid=scope_uuid,
    )


def publish_message_sent(
    log_uuid: str,
    message_uuid: str,
    sender: str,
    content: str,
    fragments: list,
    scope_uuid: Optional[str] = None,
) -> int:
    """Publish message_sent event.

    Convenience wrapper for message operations.

    Args:
        log_uuid: ConversationLog UUID
        message_uuid: Message Item UUID
        sender: Message sender (operator/agent/system)
        content: Message content
        fragments: List of fragment IDs in message
        scope_uuid: Scope UUID for routing (optional)

    Returns:
        Number of connections event was published to
    """
    return publish_event(
        "message_sent",
        {
            "log_uuid": log_uuid,
            "message_uuid": message_uuid,
            "sender": sender,
            "content": content,
            "fragments": fragments,
        },
        scope_uuid=scope_uuid,
    )


def publish_context_updated(
    participant_uuid: str,
    containers: list,
    scope_uuid: Optional[str] = None,
) -> int:
    """Publish context_updated event.

    Convenience wrapper for ContextFrame changes.

    Args:
        participant_uuid: UUID of participant whose frame changed
        containers: New container list
        scope_uuid: Scope UUID for routing (optional)

    Returns:
        Number of connections event was published to
    """
    return publish_event(
        "context_updated",
        {
            "participant_uuid": participant_uuid,
            "containers": containers,
        },
        scope_uuid=scope_uuid,
    )


def publish_frame_updated(
    participant_uuid: str,
    head_item_uuid: Optional[str],
    scope_uuid: Optional[str] = None,
) -> int:
    """Publish frame_updated event.

    Convenience wrapper for frame focus changes.

    Args:
        participant_uuid: UUID of participant whose frame changed
        head_item_uuid: New head item UUID (None if empty)
        scope_uuid: Scope UUID for routing (optional)

    Returns:
        Number of connections event was published to
    """
    return publish_event(
        "frame_updated",
        {
            "participant_uuid": participant_uuid,
            "head_item_uuid": head_item_uuid,
        },
        scope_uuid=scope_uuid,
    )
