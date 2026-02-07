"""
Tests for /api/v1/recurrences endpoints.

Tests cover:
- POST   /api/v1/recurrences        - Create recurrence
- GET    /api/v1/recurrences        - List with filtering
- GET    /api/v1/recurrences/{id}   - Get single recurrence
- PUT    /api/v1/recurrences/{id}   - Update recurrence
- DELETE /api/v1/recurrences/{id}   - Delete recurrence
"""

import json


def test_create_recurrence(client, auth_headers, sample_recurrence_data):
    """Test creating a new recurrence."""
    response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )

    assert response.status_code == 201
    data = response.get_json()

    # Verify response structure
    assert "id" in data
    assert data["rrule"] == sample_recurrence_data["rrule"]
    assert data["entities"] == sample_recurrence_data["entities"]
    assert data["valid_from"] == sample_recurrence_data["valid_from"]
    assert data["valid_until"] == sample_recurrence_data["valid_until"]
    assert "created_at" in data
    assert "updated_at" in data


def test_create_recurrence_unauthenticated(client, sample_recurrence_data):
    """Test that creating a recurrence without authentication fails."""
    response = client.post(
        "/api/v1/recurrences",
        data=json.dumps(sample_recurrence_data)
    )

    assert response.status_code == 401
    data = response.get_json()
    assert "error" in data
    assert data["error"]["type"] == "AuthenticationError"


def test_create_recurrence_invalid_rrule(client, auth_headers, sample_recurrence_data):
    """Test that invalid RRULE format is rejected."""
    invalid_data = {
        **sample_recurrence_data,
        "rrule": "INVALID_RRULE_FORMAT"
    }

    response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(invalid_data)
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data


def test_create_recurrence_invalid_window(client, auth_headers, sample_recurrence_data):
    """Test that invalid recurrence window is rejected."""
    invalid_data = {
        **sample_recurrence_data,
        "valid_from": "2025-12-31T23:59:59Z",
        "valid_until": "2025-01-01T00:00:00Z"  # Before valid_from
    }

    response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(invalid_data)
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data


def test_get_recurrence(client, auth_headers, sample_recurrence_data):
    """Test getting a single recurrence by ID."""
    # First create a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    # Get the recurrence
    response = client.get(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["id"] == recurrence_id
    assert data["rrule"] == sample_recurrence_data["rrule"]


def test_get_recurrence_not_found(client, auth_headers):
    """Test getting a non-existent recurrence returns 404."""
    response = client.get(
        "/api/v1/recurrences/550e8400-e29b-41d4-a716-446655440000",
        headers=auth_headers
    )

    assert response.status_code == 404


def test_list_recurrences(client, auth_headers, sample_recurrence_data):
    """Test listing all recurrences."""
    # Create two recurrences
    client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps({
            **sample_recurrence_data,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "valid_from": "2025-02-01T00:00:00Z"
        })
    )

    response = client.get(
        "/api/v1/recurrences",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert len(data) >= 2


def test_list_recurrences_with_filters(client, auth_headers, sample_recurrence_data):
    """Test listing recurrences with query filters."""
    # Create recurrences with different valid_from dates
    client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps({
            **sample_recurrence_data,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "valid_from": "2025-06-01T00:00:00Z"
        })
    )

    # Filter by valid_from
    response = client.get(
        "/api/v1/recurrences?valid_from=2025-03-01T00:00:00Z",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) >= 1


def test_list_recurrences_pagination(client, auth_headers, sample_recurrence_data):
    """Test listing recurrences with pagination."""
    # Create multiple recurrences
    for i in range(5):
        client.post(
            "/api/v1/recurrences",
            headers=auth_headers,
            data=json.dumps({
                **sample_recurrence_data,
                "valid_from": f"2025-{i+1:02d}-01T00:00:00Z"
            })
        )

    # Test limit
    response = client.get(
        "/api/v1/recurrences?limit=2",
        headers=auth_headers
    )
    data = response.get_json()
    assert len(data) == 2

    # Test offset
    response = client.get(
        "/api/v1/recurrences?limit=2&offset=2",
        headers=auth_headers
    )
    data = response.get_json()
    assert len(data) == 2


def test_update_recurrence(client, auth_headers, sample_recurrence_data):
    """Test updating a recurrence."""
    # Create a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    # Update the recurrence
    update_data = {
        "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
    }
    response = client.put(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["rrule"] == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_update_recurrence_invalid_rrule(client, auth_headers, sample_recurrence_data):
    """Test that updating with invalid RRULE is rejected."""
    # Create a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    # Try to update with invalid RRULE
    update_data = {
        "rrule": "INVALID_RRULE",
    }
    response = client.put(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 400


def test_update_recurrence_invalid_window(client, auth_headers, sample_recurrence_data):
    """Test that updating with invalid window is rejected."""
    # Create a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    # Try to update with invalid window
    update_data = {
        "valid_until": "2024-01-01T00:00:00Z",  # Before valid_from
    }
    response = client.put(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 400


def test_delete_recurrence(client, auth_headers, sample_recurrence_data):
    """Test deleting a recurrence (soft delete)."""
    # Create a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    # Delete the recurrence
    response = client.delete(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers
    )

    assert response.status_code == 204

    # Verify recurrence is superseded (not included in default list)
    list_response = client.get(
        "/api/v1/recurrences",
        headers=auth_headers
    )
    recurrences = list_response.get_json()
    assert not any(r["id"] == recurrence_id for r in recurrences)


def test_delete_recurrence_not_found(client, auth_headers):
    """Test deleting a non-existent recurrence returns 404."""
    response = client.delete(
        "/api/v1/recurrences/550e8400-e29b-41d4-a716-446655440000",
        headers=auth_headers
    )

    assert response.status_code == 404


def test_list_recurrences_include_superseded(client, auth_headers, sample_recurrence_data):
    """Test listing recurrences with include_superseded flag."""
    # Create and then delete a recurrence
    create_response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers,
        data=json.dumps(sample_recurrence_data)
    )
    recurrence = create_response.get_json()
    recurrence_id = recurrence["id"]

    client.delete(
        f"/api/v1/recurrences/{recurrence_id}",
        headers=auth_headers
    )

    # Without include_superseded - should not show
    response = client.get(
        "/api/v1/recurrences",
        headers=auth_headers
    )
    data = response.get_json()
    assert not any(r["id"] == recurrence_id for r in data)

    # With include_superseded=true - should show
    response = client.get(
        "/api/v1/recurrences?include_superseded=true",
        headers=auth_headers
    )
    data = response.get_json()
    assert any(r["id"] == recurrence_id for r in data)


def test_authentication_with_api_key(client, auth_headers_apikey, sample_recurrence_data):
    """Test that API key authentication works for recurrence endpoints."""
    response = client.post(
        "/api/v1/recurrences",
        headers=auth_headers_apikey,
        data=json.dumps(sample_recurrence_data)
    )

    assert response.status_code == 201
    data = response.get_json()
    assert "id" in data
