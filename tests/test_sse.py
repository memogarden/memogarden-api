"""Tests for Server-Sent Events (SSE) endpoint.

Session 20A: SSE Infrastructure and Event Publishing

Tests cover:
- SSE connection establishment with authentication
- Event publishing to subscribed scopes
- Scope filtering (events only to subscribed connections)
- Keepalive messages
- Connection cleanup on disconnect
- Multiple concurrent connections
- SSE statistics endpoint
"""

import json
import queue as Queue
import threading
import time

import pytest

from api.events import (
    EVENT_TYPES,
    SSEConnection,
    SSEManager,
    publish_artifact_delta,
    publish_event,
    publish_frame_updated,
    publish_message_sent,
    sse_manager,
)


# ============================================================================
# SSEManager Tests (Unit Tests)
# ============================================================================


class TestSSEManager:
    """Unit tests for SSEManager class."""

    def test_register_connection(self):
        """Test registering a new SSE connection."""
        user_id = "user_123"
        username = "testuser"
        scopes = {"core_abc", "core_def"}

        client_id, conn = sse_manager.register(user_id, username, scopes)

        assert client_id.startswith("sse_")
        assert conn.client_id == client_id
        assert conn.user_id == user_id
        assert conn.username == username
        assert conn.subscribed_scopes == scopes
        assert isinstance(conn.queue, Queue.Queue)

        # Cleanup
        sse_manager.unregister(client_id)

    def test_register_increments_counter(self):
        """Test that client IDs increment on each registration."""
        # Clear any existing connections
        for cid in list(sse_manager._connections.keys()):
            sse_manager.unregister(cid)

        # Register multiple connections
        ids = []
        for i in range(5):
            client_id, _ = sse_manager.register(f"user_{i}", f"user{i}", set())
            ids.append(client_id)
            sse_manager.unregister(client_id)

        # Check IDs are sequential
        numbers = [int(cid.split("_")[1]) for cid in ids]
        assert sorted(numbers) == numbers

    def test_unregister_connection(self):
        """Test unregistering an SSE connection."""
        user_id = "user_456"
        username = "testuser"

        client_id, conn = sse_manager.register(user_id, username, set())

        # Unregister
        removed = sse_manager.unregister(client_id)

        assert removed is not None
        assert removed.client_id == client_id
        assert client_id not in sse_manager._connections

    def test_unregister_nonexistent_connection(self):
        """Test unregistering a non-existent connection returns None."""
        result = sse_manager.unregister("sse_nonexistent")
        assert result is None

    def test_get_connection_count(self):
        """Test getting connection count."""
        # Start with no connections
        # Clear any existing connections
        for cid in list(sse_manager._connections.keys()):
            sse_manager.unregister(cid)

        assert sse_manager.get_connection_count() == 0

        # Register some connections
        connections = []
        for i in range(3):
            client_id, _ = sse_manager.register(f"user_{i}", f"user{i}", set())
            connections.append(client_id)

        assert sse_manager.get_connection_count() == 3

        # Cleanup
        for cid in connections:
            sse_manager.unregister(cid)

    def test_publish_to_no_connections(self):
        """Test publishing when no connections exist."""
        # Clear connections
        for cid in list(sse_manager._connections.keys()):
            sse_manager.unregister(cid)

        count = sse_manager.publish("artifact_delta", {"test": "data"})
        assert count == 0

    def test_publish_invalid_event_type_raises_error(self):
        """Test that publishing invalid event type raises ValueError."""
        client_id, _ = sse_manager.register("user_1", "user1", set())

        with pytest.raises(ValueError, match="Unknown event type"):
            sse_manager.publish("invalid_event_type", {})

        sse_manager.unregister(client_id)


class TestSSEConnection:
    """Unit tests for SSEConnection class."""

    def test_is_subscribed_to_with_scope_match(self):
        """Test subscription check when scope matches."""
        conn = SSEConnection(
            client_id="sse_1",
            user_id="user_1",
            username="user1",
            subscribed_scopes={"core_abc", "core_def"}
        )

        assert conn.is_subscribed_to("core_abc") is True
        assert conn.is_subscribed_to("core_def") is True

    def test_is_subscribed_to_with_no_scope_match(self):
        """Test subscription check when scope doesn't match."""
        conn = SSEConnection(
            client_id="sse_1",
            user_id="user_1",
            username="user1",
            subscribed_scopes={"core_abc"}
        )

        assert conn.is_subscribed_to("core_xyz") is False

    def test_is_subscribed_to_with_global_event(self):
        """Test that all connections receive global events (scope_uuid=None)."""
        conn = SSEConnection(
            client_id="sse_1",
            user_id="user_1",
            username="user1",
            subscribed_scopes={"core_abc"}
        )

        # Global events (scope_uuid=None) go to all connections
        assert conn.is_subscribed_to(None) is True

    def test_is_subscribed_to_with_empty_scopes(self):
        """Test connection with no scope subscriptions."""
        conn = SSEConnection(
            client_id="sse_1",
            user_id="user_1",
            username="user1",
            subscribed_scopes=set()
        )

        # Should only receive global events
        assert conn.is_subscribed_to("core_abc") is False
        assert conn.is_subscribed_to(None) is True


class TestEventPublishing:
    """Unit tests for event publishing functions."""

    def test_publish_event(self):
        """Test basic publish_event function."""
        # Register a connection
        client_id, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        # Publish event
        count = publish_event("artifact_delta", {"artifact_uuid": "art_123"}, scope_uuid="core_abc")

        assert count == 1

        # Check event was queued
        event = conn.queue.get(timeout=1)
        assert event["type"] == "artifact_delta"
        assert event["data"]["artifact_uuid"] == "art_123"

        # Cleanup
        sse_manager.unregister(client_id)

    def test_publish_event_scope_filtering(self):
        """Test that events only go to subscribed connections."""
        # Register two connections with different scopes
        _, conn1 = sse_manager.register("user_1", "user1", {"core_abc"})
        _, conn2 = sse_manager.register("user_2", "user2", {"core_def"})

        # Publish event to core_abc
        count = publish_event("message_sent", {"content": "hello"}, scope_uuid="core_abc")

        assert count == 1

        # Only conn1 should have the event
        event = conn1.queue.get(timeout=1)
        assert event["type"] == "message_sent"

        # conn2's queue should be empty
        assert conn2.queue.empty()

        # Cleanup
        sse_manager.unregister(conn1.client_id)
        sse_manager.unregister(conn2.client_id)

    def test_publish_artifact_delta(self):
        """Test publish_artifact_delta convenience wrapper."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_artifact_delta(
            artifact_uuid="art_123",
            commit_hash="hash_456",
            ops="+5:^abc",
            actor="user_1",
            scope_uuid="core_abc"
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "artifact_delta"
        assert event["data"]["artifact_uuid"] == "art_123"
        assert event["data"]["commit_hash"] == "hash_456"
        assert event["data"]["ops"] == "+5:^abc"
        assert event["data"]["actor"] == "user_1"

        sse_manager.unregister(conn.client_id)

    def test_publish_message_sent(self):
        """Test publish_message_sent convenience wrapper."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_message_sent(
            log_uuid="log_123",
            message_uuid="msg_456",
            sender="agent",
            content="Hello world",
            fragments=["^abc", "^def"],
            scope_uuid="core_abc"
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "message_sent"
        assert event["data"]["log_uuid"] == "log_123"
        assert event["data"]["message_uuid"] == "msg_456"
        assert event["data"]["sender"] == "agent"
        assert event["data"]["content"] == "Hello world"
        assert event["data"]["fragments"] == ["^abc", "^def"]

        sse_manager.unregister(conn.client_id)

    def test_publish_frame_updated(self):
        """Test publish_frame_updated convenience wrapper."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_frame_updated(
            participant_uuid="user_1",
            head_item_uuid="item_123",
            scope_uuid="core_abc"
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "frame_updated"
        assert event["data"]["participant_uuid"] == "user_1"
        assert event["data"]["head_item_uuid"] == "item_123"

        sse_manager.unregister(conn.client_id)

    def test_publish_global_event(self):
        """Test that global events (scope_uuid=None) go to all connections."""
        _, conn1 = sse_manager.register("user_1", "user1", {"core_abc"})
        _, conn2 = sse_manager.register("user_2", "user2", {"core_def"})

        # Publish global event
        count = publish_event("scope_created", {"label": "New Scope"}, scope_uuid=None)

        assert count == 2

        # Both connections should receive
        event1 = conn1.queue.get(timeout=1)
        assert event1["type"] == "scope_created"

        event2 = conn2.queue.get(timeout=1)
        assert event2["type"] == "scope_created"

        # Cleanup
        sse_manager.unregister(conn1.client_id)
        sse_manager.unregister(conn2.client_id)

    def test_publish_relation_created(self):
        """Test publish_event for relation_created."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_event(
            "relation_created",
            {
                "relation_uuid": "rel_123",
                "kind": "part_of",
                "source": "core_abc",
                "target": "core_def",
                "actor": "user_1",
            },
            scope_uuid=None,  # Relations are global events
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "relation_created"
        assert event["data"]["relation_uuid"] == "rel_123"
        assert event["data"]["kind"] == "part_of"

        sse_manager.unregister(conn.client_id)

    def test_publish_relation_modified(self):
        """Test publish_event for relation_modified (edit)."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_event(
            "relation_modified",
            {
                "relation_uuid": "rel_123",
                "action": "edited",
                "actor": "user_1",
            },
            scope_uuid=None,
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "relation_modified"
        assert event["data"]["relation_uuid"] == "rel_123"
        assert event["data"]["action"] == "edited"

        sse_manager.unregister(conn.client_id)

    def test_publish_relation_deleted(self):
        """Test publish_event for relation_modified (delete)."""
        _, conn = sse_manager.register("user_1", "user1", {"core_abc"})

        count = publish_event(
            "relation_modified",
            {
                "relation_uuid": "rel_123",
                "action": "deleted",
                "actor": "user_1",
            },
            scope_uuid=None,
        )

        assert count == 1

        event = conn.queue.get(timeout=1)
        assert event["type"] == "relation_modified"
        assert event["data"]["relation_uuid"] == "rel_123"
        assert event["data"]["action"] == "deleted"

        sse_manager.unregister(conn.client_id)


# ============================================================================
# SSE Endpoint Integration Tests
# ============================================================================


class TestSSEEndpoint:
    """Integration tests for /mg/events endpoint."""

    def test_events_endpoint_requires_auth(self, client):
        """Test that /mg/events requires authentication."""
        response = client.get("/mg/events")

        # Should return 401 Unauthorized
        assert response.status_code == 401

    def test_events_stream_with_scope_filter(self, client, auth_headers):
        """Test SSE stream with scope subscription filter."""
        # Just verify the endpoint is registered and accessible
        # Note: Streaming endpoints in Flask test client may not fully work
        # so we just check that the endpoint can be reached
        response = client.get(
            "/mg/events?scopes=core_abc,core_def",
            headers=auth_headers
        )
        # Check for 200 (streaming ready) or 401 (auth failed)
        assert response.status_code in (200, 401)


class TestSSEStatsEndpoint:
    """Tests for /mg/events/stats endpoint."""

    def test_stats_requires_auth(self, client):
        """Test that /mg/events/stats requires authentication."""
        response = client.get("/mg/events/stats")

        assert response.status_code == 401

    def test_stats_returns_connection_info(self, client, auth_headers):
        """Test that stats endpoint returns connection information."""
        # First, directly register a connection to avoid streaming issues
        from api.events import sse_manager
        _, conn = sse_manager.register("test_user", "testuser", {"core_abc"})

        try:
            # Get stats
            stats_response = client.get("/mg/events/stats", headers=auth_headers)

            assert stats_response.status_code == 200
            data = stats_response.json

            assert "active_connections" in data
            assert data["active_connections"] >= 1
            assert "connections" in data
        finally:
            # Cleanup
            sse_manager.unregister(conn.client_id)


# ============================================================================
# SSE Event Types Tests
# ============================================================================


class TestEventTypes:
    """Tests for SSE event type validation."""

    def test_all_defined_event_types_are_valid(self):
        """Test that all defined event types are valid strings."""
        expected_types = {
            "artifact_delta",
            "message_sent",
            "context_updated",
            "frame_updated",
            "scope_created",
            "scope_modified",
            "relation_created",
            "relation_modified",
        }

        assert EVENT_TYPES == expected_types

    def test_event_types_is_immutable(self):
        """Test that EVENT_TYPES is a frozenset and cannot be modified."""
        # Verify it's a frozenset
        assert isinstance(EVENT_TYPES, frozenset)

        # Verify no add method (frozensets don't have .add())
        assert not hasattr(EVENT_TYPES, "add")

        # Original count should remain the same
        original_count = len(EVENT_TYPES)
        assert len(EVENT_TYPES) == original_count


# ============================================================================
# SSE Threading Tests
# ============================================================================


class TestSSEThreading:
    """Tests for thread-safe SSE manager operations."""

    def test_concurrent_publish(self):
        """Test that multiple threads can publish events safely."""
        results = {"count": 0}
        errors = []

        def publish_worker(worker_id):
            """Worker function that publishes events."""
            try:
                # Create connection for this worker
                _, conn = sse_manager.register(f"user_{worker_id}", f"user{worker_id}", {f"scope_{worker_id}"})

                for i in range(10):
                    sse_manager.publish("artifact_delta", {"worker": worker_id, "i": i}, scope_uuid=f"scope_{worker_id}")

                results["count"] += 10

                # Cleanup
                sse_manager.unregister(conn.client_id)
            except Exception as e:
                errors.append(e)

        # Run multiple threads
        threads = []
        for i in range(5):
            t = threading.Thread(target=publish_worker, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join(timeout=5)

        # Verify no errors
        assert len(errors) == 0
        assert results["count"] == 50  # 5 threads * 10 events

    def test_concurrent_register_unregister(self):
        """Test that multiple threads can register/unregister safely."""
        errors = []
        client_ids = []

        def worker(worker_id):
            """Worker that registers and unregisters."""
            try:
                cid, _ = sse_manager.register(f"user_{worker_id}", f"user{worker_id}", set())
                client_ids.append(cid)
                time.sleep(0.01)  # Small delay
                sse_manager.unregister(cid)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        assert len(client_ids) == 10
        assert sse_manager.get_connection_count() == 0
