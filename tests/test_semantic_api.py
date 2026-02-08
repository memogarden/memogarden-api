"""Tests for Semantic API Core bundle and Soil bundle verbs.

Per RFC-005 v7, the Semantic API is a message-passing interface with:
- Request: {"op": "verb", ...}
- Response: {"ok": true, "actor": "...", "timestamp": "...", "result": {...}}

Session 1 tests Core bundle verbs:
- create: Create entity (baseline types)
- get: Get entity by UUID
- edit: Edit entity (set/unset semantics)
- forget: Soft delete entity
- query: Query entities with filters

Session 2 tests Soil bundle verbs:
- add: Add fact (bring external data into MemoGarden)
- amend: Amend fact (create superseding fact)
- get: Get fact by UUID (routes based on UUID prefix)
- query: Query facts with filters
"""



class TestSemanticAPIResponseEnvelope:
    """Test response envelope format."""

    def test_response_envelope_success(self, client, auth_headers):
        """Test successful response has required fields."""
        response = client.post(
            "/mg",
            json={"op": "query"},
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()

        # Required response envelope fields (RFC-005 v7)
        assert "ok" in data
        assert "actor" in data
        assert "timestamp" in data
        assert "result" in data

        # Success response
        assert data["ok"] is True
        assert data["actor"] == "testuser"

    def test_response_envelope_error(self, client, auth_headers):
        """Test error response has required fields."""
        response = client.post(
            "/mg",
            json={"op": "invalid"},
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()

        # Required error envelope fields
        assert "ok" in data
        assert "actor" in data
        assert "timestamp" in data
        assert "error" in data

        # Error response
        assert data["ok"] is False
        assert "type" in data["error"]
        assert "message" in data["error"]


class TestSemanticAPIAuthentication:
    """Test Semantic API requires authentication."""

    def test_unauthenticated_request_rejected(self, client):
        """Test unauthenticated requests are rejected."""
        response = client.post(
            "/mg",
            json={"op": "query"}
        )

        assert response.status_code == 401


class TestCreateVerb:
    """Tests for create verb."""

    def test_create_entity(self, client, auth_headers):
        """Test creating an entity via Semantic API."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"name": "Test Entity", "value": 42}
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "result" in data

        # Verify entity structure
        result = data["result"]
        assert "uuid" in result
        assert result["type"] == "Entity"
        assert result["data"]["name"] == "Test Entity"
        assert result["data"]["value"] == 42
        assert "hash" in result
        assert "version" in result
        assert "created_at" in result

    def test_create_transaction_type(self, client, auth_headers):
        """Test creating a Transaction entity (baseline type)."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "amount": -15.50,
                    "account": "Personal",
                    "category": "Food"
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["type"] == "Transaction"

    def test_create_unsupported_type_fails(self, client, auth_headers):
        """Test creating unsupported entity type fails."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "CustomType",
                "data": {}
            },
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
        assert "not supported" in data["error"]["message"]


class TestGetVerb:
    """Tests for get verb."""

    def test_get_entity(self, client, auth_headers):
        """Test getting an entity by UUID."""
        # First create an entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"name": "Test"}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Get the entity
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": entity_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["uuid"] == entity_uuid

    def test_get_entity_with_uuid_prefix(self, client, auth_headers):
        """Test getting entity accepts prefixed UUID."""
        # Create entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Get with prefix
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": entity_uuid  # Already has core_ prefix
            },
            headers=auth_headers
        )

        assert response.status_code == 200

    def test_get_entity_not_found(self, client, auth_headers):
        """Test getting non-existent entity returns 404."""
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": "core_00000000-0000-0000-0000-000000000000"
            },
            headers=auth_headers
        )

        assert response.status_code == 404
        data = response.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"]["message"].lower()


class TestEditVerb:
    """Tests for edit verb with set/unset semantics."""

    def test_edit_entity_set(self, client, auth_headers):
        """Test editing entity with set operation."""
        # Create entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"name": "Original", "value": 1}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Edit with set
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"name": "Updated", "new_field": "added"}
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["data"]["name"] == "Updated"
        assert data["result"]["data"]["new_field"] == "added"
        assert data["result"]["data"]["value"] == 1  # Original value preserved

    def test_edit_entity_unset(self, client, auth_headers):
        """Test editing entity with unset operation."""
        # Create entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"name": "Test", "temp": "remove_me"}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Edit with unset
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "unset": ["temp"]
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "temp" not in data["result"]["data"]
        assert data["result"]["data"]["name"] == "Test"

    def test_edit_entity_set_and_unset(self, client, auth_headers):
        """Test editing entity with both set and unset."""
        # Create entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"old": "value", "keep": "this"}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Edit with set and unset
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": entity_uuid,
                "set": {"new": "value"},
                "unset": ["old"]
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "old" not in data["result"]["data"]
        assert data["result"]["data"]["new"] == "value"
        assert data["result"]["data"]["keep"] == "this"


class TestForgetVerb:
    """Tests for forget verb (soft delete)."""

    def test_forget_entity(self, client, auth_headers):
        """Test forgetting an entity (soft delete)."""
        # Create entity
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"name": "To Forget"}
            },
            headers=auth_headers
        )
        entity_uuid = create_response.get_json()["result"]["uuid"]

        # Forget entity
        response = client.post(
            "/mg",
            json={
                "op": "forget",
                "target": entity_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["superseded_by"] is not None
        assert data["result"]["superseded_at"] is not None

    def test_forget_entity_not_found(self, client, auth_headers):
        """Test forgetting non-existent entity returns 404."""
        response = client.post(
            "/mg",
            json={
                "op": "forget",
                "target": "core_00000000-0000-0000-0000-000000000000"
            },
            headers=auth_headers
        )

        assert response.status_code == 404


class TestQueryVerb:
    """Tests for query verb."""

    def test_query_all_entities(self, client, auth_headers):
        """Test querying all entities."""
        response = client.post(
            "/mg",
            json={
                "op": "query"
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "results" in data["result"]
        assert "total" in data["result"]
        assert "start_index" in data["result"]
        assert "count" in data["result"]

    def test_query_by_type(self, client, auth_headers):
        """Test querying entities filtered by type."""
        # Create entities of different types
        client.post(
            "/mg",
            json={"op": "create", "type": "Entity", "data": {}},
            headers=auth_headers
        )
        client.post(
            "/mg",
            json={"op": "create", "type": "Transaction", "data": {}},
            headers=auth_headers
        )

        # Query for Entity type only
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "type": "Entity"
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        # All results should be Entity type
        for result in data["result"]["results"]:
            assert result["type"] == "Entity"

    def test_query_pagination(self, client, auth_headers):
        """Test query with pagination."""
        # Create multiple entities
        for _ in range(5):
            client.post(
                "/mg",
                json={"op": "create", "type": "Entity", "data": {}},
                headers=auth_headers
            )

        # Query with limit
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "count": 2
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert len(data["result"]["results"]) <= 2
        assert data["result"]["count"] <= 2


class TestSemanticAPIValidation:
    """Test request validation."""

    def test_missing_op_field(self, client, auth_headers):
        """Test request without op field fails."""
        response = client.post(
            "/mg",
            json={},
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
        assert "op" in data["error"]["message"].lower()

    def test_invalid_operation(self, client, auth_headers):
        """Test invalid operation name fails."""
        response = client.post(
            "/mg",
            json={"op": "not_a_real_verb"},
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
        assert "unsupported" in data["error"]["message"].lower()

    def test_missing_required_fields(self, client, auth_headers):
        """Test missing required fields fails."""
        response = client.post(
            "/mg",
            json={"op": "create"},  # Missing required 'type' field
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False

    def test_empty_unset_list_fails(self, client, auth_headers):
        """Test empty unset list fails validation."""
        response = client.post(
            "/mg",
            json={
                "op": "edit",
                "target": "core_abc",
                "unset": []  # Empty list not allowed
            },
            headers=auth_headers
        )

        assert response.status_code == 400


class TestSemanticAPINullSemantics:
    """Test null value semantics (RFC-005 v7)."""

    def test_null_in_data(self, client, auth_headers):
        """Test null values in data field are preserved."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Entity",
                "data": {"field": None}
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["data"]["field"] is None


class TestAddVerb:
    """Tests for add verb (Soil bundle)."""

    def test_add_note_fact(self, client, auth_headers):
        """Test adding a Note fact via Semantic API."""
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {
                    "title": "Test Note",
                    "description": "This is a test note"
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "result" in data

        # Verify fact structure
        result = data["result"]
        assert "uuid" in result
        assert result["type"] == "Note"
        assert result["data"]["title"] == "Test Note"
        assert result["data"]["description"] == "This is a test note"
        assert result["uuid"].startswith("soil_")
        assert "integrity_hash" in result
        assert "realized_at" in result
        assert "canonical_at" in result
        assert result["fidelity"] == "full"

    def test_add_message_fact(self, client, auth_headers):
        """Test adding a Message fact (baseline type)."""
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Message",
                "data": {
                    "from": "alice@example.com",
                    "to": ["bob@example.com"],
                    "content": "Hello Bob"
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["type"] == "Message"

    def test_add_unsupported_type_fails(self, client, auth_headers):
        """Test adding unsupported fact type fails."""
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "CustomFact",
                "data": {}
            },
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
        assert "not supported" in data["error"]["message"]

    def test_add_fact_with_metadata(self, client, auth_headers):
        """Test adding a fact with metadata."""
        response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Test"},
                "metadata": {"source": "test", "priority": 1}
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["metadata"]["source"] == "test"
        assert data["result"]["metadata"]["priority"] == 1


class TestAmendVerb:
    """Tests for amend verb (Soil bundle)."""

    def test_amend_fact(self, client, auth_headers):
        """Test amending a fact creates superseding fact."""
        # Create original fact
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {
                    "title": "Original",
                    "description": "Original content"
                }
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        # Amend the fact
        response = client.post(
            "/mg",
            json={
                "op": "amend",
                "target": fact_uuid,
                "data": {
                    "title": "Corrected",
                    "description": "Corrected content"
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        # Verify amended fact
        result = data["result"]
        assert result["type"] == "Note"
        assert result["data"]["title"] == "Corrected"
        assert result["data"]["description"] == "Corrected content"
        assert result["uuid"].startswith("soil_")
        # New UUID for amended fact
        assert result["uuid"] != fact_uuid

        # Verify original is superseded
        original_response = client.post(
            "/mg",
            json={"op": "get", "target": fact_uuid},
            headers=auth_headers
        )
        original = original_response.get_json()["result"]
        assert original["superseded_by"] is not None
        assert original["superseded_at"] is not None

    def test_amend_fact_preserves_metadata(self, client, auth_headers):
        """Test amending a fact preserves original metadata."""
        # Create fact with metadata
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Test"},
                "metadata": {"original": "value"}
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        # Amend with additional metadata
        response = client.post(
            "/mg",
            json={
                "op": "amend",
                "target": fact_uuid,
                "data": {"description": "Amended"},
                "metadata": {"new": "field"}
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        # Metadata should be merged
        assert data["result"]["metadata"]["original"] == "value"
        assert data["result"]["metadata"]["new"] == "field"

    def test_amend_nonexistent_fact_fails(self, client, auth_headers):
        """Test amending non-existent fact returns 404."""
        response = client.post(
            "/mg",
            json={
                "op": "amend",
                "target": "soil_00000000-0000-0000-0000-000000000000",
                "data": {"description": "Amended"}
            },
            headers=auth_headers
        )

        assert response.status_code == 404

    def test_amend_superseded_fact_fails(self, client, auth_headers):
        """Test amending a fact that is already superseded fails."""
        # Create and amend a fact
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Original"}
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        client.post(
            "/mg",
            json={
                "op": "amend",
                "target": fact_uuid,
                "data": {"description": "First amendment"}
            },
            headers=auth_headers
        )

        # Try to amend again (should fail because original is superseded)
        response = client.post(
            "/mg",
            json={
                "op": "amend",
                "target": fact_uuid,
                "data": {"description": "Second amendment"}
            },
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
        assert "superseded" in data["error"]["message"].lower()


class TestGetFactVerb:
    """Tests for get verb with facts (Soil bundle)."""

    def test_get_fact_by_uuid(self, client, auth_headers):
        """Test getting a fact by UUID."""
        # Create a fact
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Test note"}
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        # Get the fact
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": fact_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["result"]["uuid"] == fact_uuid

    def test_get_fact_routes_on_prefix(self, client, auth_headers):
        """Test get verb routes correctly based on UUID prefix."""
        # Create a fact (soil_ prefix)
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Test"}
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        # Get with soil_ prefix
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": fact_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        # Should return fact, not entity
        assert "integrity_hash" in data["result"]
        assert "realized_at" in data["result"]

    def test_get_fact_not_found(self, client, auth_headers):
        """Test getting non-existent fact returns 404."""
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": "soil_00000000-0000-0000-0000-000000000000"
            },
            headers=auth_headers
        )

        assert response.status_code == 404
        data = response.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"]["message"].lower()


class TestQueryFactsVerb:
    """Tests for query verb with facts (Soil bundle)."""

    def test_query_all_facts(self, client, auth_headers):
        """Test querying all facts via target_type=fact."""
        # Create a few facts
        client.post(
            "/mg",
            json={"op": "add", "type": "Note", "data": {"description": "Note 1"}},
            headers=auth_headers
        )
        client.post(
            "/mg",
            json={"op": "add", "type": "Message", "data": {"content": "Message 1"}},
            headers=auth_headers
        )

        # Query for facts
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact"
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert "results" in data["result"]
        assert "total" in data["result"]

    def test_query_facts_by_type(self, client, auth_headers):
        """Test querying facts filtered by type."""
        # Create facts of different types
        client.post(
            "/mg",
            json={"op": "add", "type": "Note", "data": {"description": "Note 1"}},
            headers=auth_headers
        )
        client.post(
            "/mg",
            json={"op": "add", "type": "Message", "data": {"content": "Message 1"}},
            headers=auth_headers
        )
        client.post(
            "/mg",
            json={"op": "add", "type": "Note", "data": {"description": "Note 2"}},
            headers=auth_headers
        )

        # Query for Note type only
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "type": "Note"
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        # All results should be Note type
        for result in data["result"]["results"]:
            assert result["type"] == "Note"

    def test_query_facts_excludes_superseded(self, client, auth_headers):
        """Test query excludes superseded facts by default."""
        # Create and supersede a fact
        add_response = client.post(
            "/mg",
            json={
                "op": "add",
                "type": "Note",
                "data": {"description": "Original"}
            },
            headers=auth_headers
        )
        fact_uuid = add_response.get_json()["result"]["uuid"]

        client.post(
            "/mg",
            json={
                "op": "amend",
                "target": fact_uuid,
                "data": {"description": "Amended"}
            },
            headers=auth_headers
        )

        # Query facts - should not include superseded
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "type": "Note"
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        # All results should not be superseded
        for result in data["result"]["results"]:
            assert result["superseded_by"] is None

    def test_query_facts_pagination(self, client, auth_headers):
        """Test query facts with pagination."""
        # Create multiple facts
        for i in range(5):
            client.post(
                "/mg",
                json={
                    "op": "add",
                    "type": "Note",
                    "data": {"description": f"Note {i}"}
                },
                headers=auth_headers
            )

        # Query with limit
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "fact",
                "count": 2
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert len(data["result"]["results"]) <= 2
        assert data["result"]["count"] <= 2


class TestLinkVerb:
    """Tests for link verb (Relations bundle - RFC-002)."""

    def test_link_entities(self, client, auth_headers):
        """Test creating a user relation between two entities."""
        # Create two entities
        source_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "Source"}},
            headers=auth_headers
        )
        source_uuid = source_response.get_json()["result"]["uuid"]

        target_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {"name": "Target"}},
            headers=auth_headers
        )
        target_uuid = target_response.get_json()["result"]["uuid"]

        # Create link
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "kind": "explicit_link",
                "source": source_uuid,
                "source_type": "entity",
                "target": target_uuid,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        # Verify relation structure
        result = data["result"]
        assert "uuid" in result
        assert result["uuid"].startswith("core_")
        assert result["kind"] == "explicit_link"
        assert result["source"] == source_uuid
        assert result["target"] == target_uuid
        assert result["source_type"] == "entity"
        assert result["target_type"] == "entity"
        assert "time_horizon" in result
        assert "last_access_at" in result
        assert "created_at" in result

    def test_link_with_custom_horizon(self, client, auth_headers):
        """Test creating a link with custom initial time horizon."""
        # Create entities
        source_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        source_uuid = source_response.get_json()["result"]["uuid"]

        target_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        target_uuid = target_response.get_json()["result"]["uuid"]

        # Create link with 30-day horizon
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": source_uuid,
                "source_type": "entity",
                "target": target_uuid,
                "target_type": "entity",
                "initial_horizon_days": 30,
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        # Verify time_horizon is ~30 days from now
        from system.utils.time import current_day
        expected_horizon = current_day() + 30
        assert data["result"]["time_horizon"] == expected_horizon

    def test_link_accepts_uuids_without_prefix(self, client, auth_headers):
        """Test link works with UUIDs without prefix."""
        # Create entities
        source_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        source_uuid = source_response.get_json()["result"]["uuid"]

        target_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        target_uuid = target_response.get_json()["result"]["uuid"]

        # Strip prefixes
        from system.utils import uid
        source_stripped = uid.strip_prefix(source_uuid)
        target_stripped = uid.strip_prefix(target_uuid)

        # Create link with stripped UUIDs
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": source_stripped,
                "source_type": "entity",
                "target": target_stripped,
                "target_type": "entity",
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        # Response should have prefixes
        assert data["result"]["source"] == source_uuid
        assert data["result"]["target"] == target_uuid

    def test_link_with_metadata(self, client, auth_headers):
        """Test creating a link with metadata."""
        # Create entities
        source_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        source_uuid = source_response.get_json()["result"]["uuid"]

        target_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        target_uuid = target_response.get_json()["result"]["uuid"]

        # Create link with metadata
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": source_uuid,
                "source_type": "entity",
                "target": target_uuid,
                "target_type": "entity",
                "metadata": {"strength": 0.8, "context": "testing"},
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

    def test_link_invalid_initial_horizon_fails(self, client, auth_headers):
        """Test creating a link with invalid initial_horizon_days fails."""
        # Create entities
        source_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        source_uuid = source_response.get_json()["result"]["uuid"]

        target_response = client.post(
            "/mg",
            json={"op": "create", "type": "Artifact", "data": {}},
            headers=auth_headers
        )
        target_uuid = target_response.get_json()["result"]["uuid"]

        # Try with negative horizon (should fail validation)
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source": source_uuid,
                "source_type": "entity",
                "target": target_uuid,
                "target_type": "entity",
                "initial_horizon_days": -1,
            },
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False

    def test_link_requires_source_and_target(self, client, auth_headers):
        """Test creating a link requires both source and target."""
        response = client.post(
            "/mg",
            json={
                "op": "link",
                "source_type": "entity",
                "target_type": "entity",
            },
            headers=auth_headers
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["ok"] is False
