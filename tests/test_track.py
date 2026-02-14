"""Tests for track verb - Session 8.

Tests the causal chain tracing from entities back to originating facts.
Per RFC-005 v7.1, the track verb returns a tree structure showing entity lineage.
"""

import pytest
from flask import Flask

from system.core import get_core
from utils import uid


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def app(flask_app: Flask):
    """Flask app with test database setup."""
    return flask_app


@pytest.fixture
def client(app: Flask):
    """Test client for the Flask app."""
    return app.test_client()


# ============================================================================
# Track Verb Tests
# ============================================================================

class TestTrackVerb:
    """Tests for track verb - causal chain tracing."""

    def test_track_simple_entity_no_sources(self, client, auth_headers):
        """Test tracking an entity with no derived_from sources."""
        with get_core() as core:
            # Create a simple entity with no derived_from
            entity_uuid = core.entity.create(
                entity_type="Artifact",
                data='{"name": "Test Artifact"}'
            )

        # Track the entity
        response = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(entity_uuid)},
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "result" in data

        result = data["result"]
        assert result["target"] == uid.add_core_prefix(entity_uuid)
        assert "chain" in result
        assert len(result["chain"]) == 1

        chain = result["chain"][0]
        assert chain["kind"] == "entity"
        assert chain["id"] == uid.add_core_prefix(entity_uuid)
        assert chain["sources"] == []  # No sources

    def test_track_entity_with_derived_from(self, client, auth_headers):
        """Test tracking an entity with derived_from link."""
        with get_core() as core:
            # Create parent entity
            parent_uuid = core.entity.create(
                entity_type="Artifact",
                data='{"name": "Parent Artifact"}'
            )

            # Create child entity derived from parent
            child_uuid = core.entity.create(
                entity_type="Artifact",
                group_id=None,
                derived_from=parent_uuid,
                data='{"name": "Child Artifact"}'
            )

        # Track the child entity
        response = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(child_uuid)},
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        result = data["result"]
        assert result["target"] == uid.add_core_prefix(child_uuid)

        chain = result["chain"][0]
        assert chain["kind"] == "entity"
        assert chain["id"] == uid.add_core_prefix(child_uuid)
        assert len(chain["sources"]) == 1

        # Check parent source
        parent_source = chain["sources"][0]
        assert parent_source["kind"] == "entity"
        assert parent_source["id"] == uid.add_core_prefix(parent_uuid)
        assert parent_source["sources"] == []  # Parent has no sources

    def test_track_chain_of_entities(self, client, auth_headers):
        """Test tracking a chain of derived entities (grandparent -> parent -> child)."""
        with get_core() as core:
            # Create grandparent
            grandparent_uuid = core.entity.create(
                entity_type="Artifact",
                data='{"name": "Grandparent"}'
            )

            # Create parent derived from grandparent
            parent_uuid = core.entity.create(
                entity_type="Artifact",
                derived_from=grandparent_uuid,
                data='{"name": "Parent"}'
            )

            # Create child derived from parent
            child_uuid = core.entity.create(
                entity_type="Artifact",
                derived_from=parent_uuid,
                data='{"name": "Child"}'
            )

        # Track the child entity
        response = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(child_uuid)},
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        chain = data["result"]["chain"][0]

        # Child -> Parent -> Grandparent
        assert chain["id"] == uid.add_core_prefix(child_uuid)
        assert len(chain["sources"]) == 1

        parent_source = chain["sources"][0]
        assert parent_source["id"] == uid.add_core_prefix(parent_uuid)
        assert len(parent_source["sources"]) == 1

        grandparent_source = parent_source["sources"][0]
        assert grandparent_source["id"] == uid.add_core_prefix(grandparent_uuid)
        assert grandparent_source["sources"] == []

    def test_track_with_depth_limit(self, client, auth_headers):
        """Test tracking with depth limit parameter."""
        with get_core() as core:
            # Create a chain: A -> B -> C -> D
            uuid_a = core.entity.create(entity_type="Artifact", data='{"name": "A"}')
            uuid_b = core.entity.create(entity_type="Artifact", derived_from=uuid_a, data='{"name": "B"}')
            uuid_c = core.entity.create(entity_type="Artifact", derived_from=uuid_b, data='{"name": "C"}')
            uuid_d = core.entity.create(entity_type="Artifact", derived_from=uuid_c, data='{"name": "D"}')

        # Track with depth=2
        response = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(uuid_d), "depth": 2},
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        chain = data["result"]["chain"][0]

        # D -> C -> B (depth=2 means D(0) -> C(1) -> B(2), stop before A)
        assert chain["id"] == uid.add_core_prefix(uuid_d)
        assert len(chain["sources"]) == 1

        c_source = chain["sources"][0]
        assert c_source["id"] == uid.add_core_prefix(uuid_c)
        assert len(c_source["sources"]) == 1

        b_source = c_source["sources"][0]
        assert b_source["id"] == uid.add_core_prefix(uuid_b)
        assert b_source["sources"] == []  # Depth limit reached, no A

    def test_track_handles_diamond_ancestry(self, client, auth_headers):
        """Test tracking handles diamond ancestry (shared parent)."""
        with get_core() as core:
            # Create shared parent
            parent_uuid = core.entity.create(
                entity_type="Artifact",
                data='{"name": "Shared Parent"}'
            )

            # Create two children from same parent
            child1_uuid = core.entity.create(
                entity_type="Artifact",
                derived_from=parent_uuid,
                data='{"name": "Child 1"}'
            )

            child2_uuid = core.entity.create(
                entity_type="Artifact",
                derived_from=parent_uuid,
                data='{"name": "Child 2"}'
            )

        # Track both children - parent should appear in both chains
        response1 = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(child1_uuid)},
            headers=auth_headers
        )

        response2 = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(child2_uuid)},
            headers=auth_headers
        )

        assert response1.status_code == 200
        assert response2.status_code == 200

        data1 = response1.get_json()
        data2 = response2.get_json()

        # Both should trace back to the same parent
        chain1 = data1["result"]["chain"][0]
        chain2 = data2["result"]["chain"][0]

        assert chain1["sources"][0]["id"] == uid.add_core_prefix(parent_uuid)
        assert chain2["sources"][0]["id"] == uid.add_core_prefix(parent_uuid)

    def test_track_with_uuid_prefix(self, client, auth_headers):
        """Test track accepts both prefixed and non-prefixed UUIDs."""
        with get_core() as core:
            entity_uuid = core.entity.create(
                entity_type="Artifact",
                data='{"name": "Test"}'
            )

        # Test with prefix
        response1 = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(entity_uuid)},
            headers=auth_headers
        )

        # Test without prefix
        response2 = client.post(
            "/mg",
            json={"op": "track", "target": entity_uuid},
            headers=auth_headers
        )

        assert response1.status_code == 200
        assert response2.status_code == 200

        data1 = response1.get_json()
        data2 = response2.get_json()

        # Both should return the same result
        assert data1["result"]["target"] == data2["result"]["target"]

    def test_track_nonexistent_entity(self, client, auth_headers):
        """Test tracking a nonexistent entity returns 404."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"

        response = client.post(
            "/mg",
            json={"op": "track", "target": uid.add_core_prefix(fake_uuid)},
            headers=auth_headers
        )

        assert response.status_code == 404
        data = response.get_json()
        assert data["ok"] is False
        assert "error" in data
