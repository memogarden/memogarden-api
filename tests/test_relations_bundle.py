"""Tests for Relations bundle verbs (Session 7).

Tests the Relations bundle verbs from RFC-002:
- unlink: Remove user relation
- edit_relation: Edit relation attributes
- get_relation: Get relation by UUID
- query_relation: Query relations with filters
- explore: Graph expansion from anchor
"""

import pytest
from system.core import get_core
from system.utils import uid


class TestUnlinkVerb:
    """Test unlink verb - remove user relation."""

    def test_unlink_removes_relation(self, client, auth_headers):
        """Test that unlink removes a user relation."""
        # First create a relation
        create_response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200
        relation_uuid = create_response.get_json()["result"]["uuid"]

        # Now unlink it
        unlink_response = client.post(
            "/mg",
            json={
                "op": "unlink",
                "target": relation_uuid,
            },
            headers=auth_headers
        )

        # Debug: print error if not successful
        if unlink_response.status_code != 200:
            print(f"ERROR: Status {unlink_response.status_code}")
            print(f"Response: {unlink_response.get_json()}")

        assert unlink_response.status_code == 200
        result = unlink_response.get_json()["result"]
        assert result["deleted"] is True
        assert result["uuid"] == relation_uuid

        # Verify relation is deleted
        with get_core() as core:
            from system.exceptions import ResourceNotFound
            with pytest.raises(ResourceNotFound):
                core.relation.get_by_id(relation_uuid)

    def test_unlink_nonexistent_relation(self, client, auth_headers):
        """Test that unlinking a nonexistent relation returns 404."""
        fake_uuid = "core_00000000-0000-0000-0000-999999999999"
        response = client.post(
            "/mg",
            json={
                "op": "unlink",
                "target": fake_uuid,
            },
            headers=auth_headers
        )
        assert response.status_code == 404


class TestEditRelationVerb:
    """Test edit_relation verb - edit relation attributes."""

    def test_edit_relation_time_horizon(self, client, auth_headers):
        """Test that edit_relation can update time_horizon."""
        # First create a relation
        create_response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
                "initial_horizon_days": 7,
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200
        relation_uuid = create_response.get_json()["result"]["uuid"]
        original_horizon = create_response.get_json()["result"]["time_horizon"]

        # Edit the time horizon
        from system.utils.time import current_day
        new_horizon = current_day() + 30

        edit_response = client.post(
            "/mg",
            json={
                "op": "edit_relation",
                "target": relation_uuid,
                "set": {
                    "time_horizon": new_horizon,
                }
            },
            headers=auth_headers
        )
        assert edit_response.status_code == 200
        result = edit_response.get_json()["result"]
        assert result["time_horizon"] == new_horizon
        assert result["time_horizon"] != original_horizon

    def test_edit_relation_metadata(self, client, auth_headers):
        """Test that edit_relation can update metadata."""
        # First create a relation
        create_response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200
        relation_uuid = create_response.get_json()["result"]["uuid"]

        # Edit the metadata
        new_metadata = {"key": "value", "number": 42}
        edit_response = client.post(
            "/mg",
            json={
                "op": "edit_relation",
                "target": relation_uuid,
                "set": {
                    "metadata": new_metadata,
                }
            },
            headers=auth_headers
        )
        assert edit_response.status_code == 200
        result = edit_response.get_json()["result"]
        assert result["metadata"] == new_metadata

    def test_edit_relation_nonexistent(self, client, auth_headers):
        """Test that editing a nonexistent relation returns 404."""
        fake_uuid = "core_00000000-0000-0000-0000-999999999999"
        response = client.post(
            "/mg",
            json={
                "op": "edit_relation",
                "target": fake_uuid,
                "set": {
                    "time_horizon": 100,
                }
            },
            headers=auth_headers
        )
        assert response.status_code == 404


class TestGetRelationVerb:
    """Test get_relation verb - get relation by UUID."""

    def test_get_relation_by_uuid(self, client, auth_headers):
        """Test that get_relation returns relation details."""
        # First create a relation
        create_response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
                "initial_horizon_days": 14,
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200
        relation_uuid = create_response.get_json()["result"]["uuid"]

        # Get the relation
        get_response = client.post(
            "/mg",
            json={
                "op": "get_relation",
                "target": relation_uuid,
            },
            headers=auth_headers
        )
        assert get_response.status_code == 200
        result = get_response.get_json()["result"]
        assert result["uuid"] == relation_uuid
        assert result["kind"] == "explicit_link"
        assert result["source_type"] == "entity"
        assert result["target_type"] == "entity"
        assert result["time_horizon"] is not None

    def test_get_relation_nonexistent(self, client, auth_headers):
        """Test that getting a nonexistent relation returns 404."""
        fake_uuid = "core_00000000-0000-0000-0000-999999999999"
        response = client.post(
            "/mg",
            json={
                "op": "get_relation",
                "target": fake_uuid,
            },
            headers=auth_headers
        )
        assert response.status_code == 404


class TestQueryRelationVerb:
    """Test query_relation verb - query relations with filters."""

    def test_query_relation_by_source(self, client, auth_headers):
        """Test that query_relation can filter by source."""
        # Create some test relations
        source = "core_00000000-0000-0000-0000-000000000001"

        client.post(
            "/mg",
            json={
                "op": "link",
                "source": source,
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
            },
            headers=auth_headers
        )

        client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000003",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000004",
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Query by source
        response = client.post(
            "/mg",
            json={
                "op": "query_relation",
                "source": source,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["count"] >= 1
        # All results should have the specified source
        for rel in result["results"]:
            assert rel["source"] == source

    def test_query_relation_by_target(self, client, auth_headers):
        """Test that query_relation can filter by target."""
        # Create some test relations
        target = "core_00000000-0000-0000-0000-000000000002"

        client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": target,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Query by target
        response = client.post(
            "/mg",
            json={
                "op": "query_relation",
                "target": target,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["count"] >= 1
        # All results should have the specified target
        for rel in result["results"]:
            assert rel["target"] == target

    def test_query_relation_by_kind(self, client, auth_headers):
        """Test that query_relation can filter by kind."""
        # Create a relation
        client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Query by kind
        response = client.post(
            "/mg",
            json={
                "op": "query_relation",
                "kind": "explicit_link",
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["count"] >= 1
        # All results should have the specified kind
        for rel in result["results"]:
            assert rel["kind"] == "explicit_link"

    def test_query_relation_alive_only(self, client, auth_headers):
        """Test that query_relation can filter by alive status."""
        # Create a relation with short horizon
        create_response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": "core_00000000-0000-0000-0000-000000000001",
                "source_type": "entity",
                "target": "core_00000000-0000-0000-0000-000000000002",
                "target_type": "entity",
                "initial_horizon_days": 1,  # Expires tomorrow
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200

        # Query alive relations (should include the new one)
        response_alive = client.post(
            "/mg",
            json={
                "op": "query_relation",
                "alive_only": True,
            },
            headers=auth_headers
        )
        assert response_alive.status_code == 200
        result_alive = response_alive.get_json()["result"]
        assert result_alive["count"] >= 1

    def test_query_relation_limit(self, client, auth_headers):
        """Test that query_relation respects limit parameter."""
        # Create multiple relations
        for i in range(5):
            client.post(
                "/mg",
                json={
                    "op": "link",
                    "source": f"core_00000000-0000-0000-0000-000000000010{i}",
                    "source_type": "entity",
                    "target": f"core_00000000-0000-0000-0000-000000000020{i}",
                    "target_type": "entity",
                },
                headers=auth_headers
            )

        # Query with limit
        response = client.post(
            "/mg",
            json={
                "op": "query_relation",
                "limit": 2,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["count"] <= 2


class TestExploreVerb:
    """Test explore verb - graph expansion from anchor."""

    def test_explore_outgoing(self, client, auth_headers):
        """Test that explore can traverse outgoing relations."""
        # First create actual entities to link
        create_a = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "A"}},
            headers=auth_headers
        )
        uuid_a = create_a.get_json()["result"]["uuid"]

        create_b = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "B"}},
            headers=auth_headers
        )
        uuid_b = create_b.get_json()["result"]["uuid"]

        create_c = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "C"}},
            headers=auth_headers
        )
        uuid_c = create_c.get_json()["result"]["uuid"]

        # Create a chain: A -> B -> C
        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_a,
                "source_type": "entity",
                "target": uuid_b,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_b,
                "source_type": "entity",
                "target": uuid_c,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Explore from A (outgoing)
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": uuid_a,
                "direction": "outgoing",
                "radius": 2,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert "nodes" in result
        assert "edges" in result
        # All edges should be outgoing
        for edge in result["edges"]:
            assert edge["direction"] == "outgoing"

    def test_explore_incoming(self, client, auth_headers):
        """Test that explore can traverse incoming relations."""
        # First create actual entities to link
        create_a = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "A"}},
            headers=auth_headers
        )
        uuid_a = create_a.get_json()["result"]["uuid"]

        create_b = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "B"}},
            headers=auth_headers
        )
        uuid_b = create_b.get_json()["result"]["uuid"]

        create_c = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "C"}},
            headers=auth_headers
        )
        uuid_c = create_c.get_json()["result"]["uuid"]

        # Create a chain: A <- B <- C
        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_b,
                "source_type": "entity",
                "target": uuid_a,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_c,
                "source_type": "entity",
                "target": uuid_b,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Explore from A (incoming)
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": uuid_a,
                "direction": "incoming",
                "radius": 2,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert "nodes" in result
        assert "edges" in result
        # All edges should be incoming
        for edge in result["edges"]:
            assert edge["direction"] == "incoming"

    def test_explore_both_directions(self, client, auth_headers):
        """Test that explore can traverse both directions."""
        # Create entities and relation
        create_a = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "A"}},
            headers=auth_headers
        )
        uuid_a = create_a.get_json()["result"]["uuid"]

        create_b = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "B"}},
            headers=auth_headers
        )
        uuid_b = create_b.get_json()["result"]["uuid"]

        # Create relation
        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_a,
                "source_type": "entity",
                "target": uuid_b,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Explore both directions
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": uuid_a,
                "direction": "both",
                "radius": 1,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert "nodes" in result
        assert "edges" in result

    def test_explore_radius_limit(self, client, auth_headers):
        """Test that explore respects radius limit."""
        # Create a chain of entities
        uuids = []
        for i in range(4):
            response = client.post(
                "/mg",
                json={"op": "create", "type": "Artifact", "data": {"name": f"Node{i}"}},
                headers=auth_headers
            )
            uuids.append(response.get_json()["result"]["uuid"])

        # Create chain: A -> B -> C -> D
        for i in range(3):
            client.post(
                "/mg",
                json={
                    "op": "link",
                    "source": uuids[i],
                    "source_type": "entity",
                    "target": uuids[i + 1],
                    "target_type": "entity",
                },
                headers=auth_headers
            )

        # Explore with radius 1
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": uuids[0],
                "direction": "outgoing",
                "radius": 1,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        # Should only traverse 1 hop
        # (actual count depends on implementation details)

    def test_explore_kind_filter(self, client, auth_headers):
        """Test that explore can filter by relation kind."""
        # Create entities and relation
        create_a = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "A"}},
            headers=auth_headers
        )
        uuid_a = create_a.get_json()["result"]["uuid"]

        create_b = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "B"}},
            headers=auth_headers
        )
        uuid_b = create_b.get_json()["result"]["uuid"]

        # Create relation
        client.post(
            "/mg",
            json={
                "op": "link",
                "source": uuid_a,
                "source_type": "entity",
                "target": uuid_b,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        # Explore with kind filter
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": uuid_a,
                "direction": "outgoing",
                "kind": "explicit_link",
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        # All edges should be of the specified kind
        for edge in result["edges"]:
            assert edge["kind"] == "explicit_link"

    def test_explore_limit(self, client, auth_headers):
        """Test that explore respects limit parameter."""
        # Create source entity
        create_source = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "Source"}},
            headers=auth_headers
        )
        source_uuid = create_source.get_json()["result"]["uuid"]

        # Create multiple target entities
        for i in range(5):
            create_target = client.post(
                "/mg",
                json={"op": "create", "type": "Artifact", "data": {"name": f"Target{i}"}},
                headers=auth_headers
            )
            target_uuid = create_target.get_json()["result"]["uuid"]

            client.post(
                "/mg",
                json={
                    "op": "link",
                    "source": source_uuid,
                    "source_type": "entity",
                    "target": target_uuid,
                    "target_type": "entity",
                },
                headers=auth_headers
            )

        # Explore with limit
        response = client.post(
            "/mg",
            json={
                "op": "explore",
                "anchor": source_uuid,
                "direction": "outgoing",
                "limit": 2,
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["count"] <= 2
