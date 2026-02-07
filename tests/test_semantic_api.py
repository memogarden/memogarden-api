"""Tests for Semantic API Core bundle verbs.

Per RFC-005 v7, the Semantic API is a message-passing interface with:
- Request: {"op": "verb", ...}
- Response: {"ok": true, "actor": "...", "timestamp": "...", "result": {...}}

Session 1 tests Core bundle verbs:
- create: Create entity (baseline types)
- get: Get entity by UUID
- edit: Edit entity (set/unset semantics)
- forget: Soft delete entity
- query: Query entities with filters
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
