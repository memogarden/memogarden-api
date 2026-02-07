"""
Tests for /api/v1/transactions endpoints.

Tests cover:
- POST   /api/v1/transactions        - Create transaction
- GET    /api/v1/transactions        - List with filtering
- GET    /api/v1/transactions/{id}   - Get single transaction
- PUT    /api/v1/transactions/{id}   - Update transaction
- DELETE /api/v1/transactions/{id}   - Delete transaction
- GET    /api/v1/transactions/accounts    - List distinct accounts
- GET    /api/v1/transactions/categories  - List distinct categories
"""

import json


def test_create_transaction(client, auth_headers, sample_transaction_data):
    """Test creating a new transaction."""
    response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )

    assert response.status_code == 201
    data = response.get_json()

    # Verify response structure
    assert "uuid" in data
    assert "id" in data  # Legacy field for compatibility
    assert data["amount"] == sample_transaction_data["amount"]
    assert data["currency"] == sample_transaction_data["currency"]
    assert data["transaction_date"] == sample_transaction_data["transaction_date"]
    assert data["description"] == sample_transaction_data["description"]
    assert data["account"] == sample_transaction_data["account"]
    assert data["category"] == sample_transaction_data["category"]
    assert data["notes"] == sample_transaction_data["notes"]
    assert data["author"] == "testuser"
    assert data["hash"] is not None
    assert data["version"] == 1
    assert "created_at" in data
    assert "updated_at" in data


def test_create_transaction_minimal(client, auth_headers):
    """Test creating a transaction with minimal required fields."""
    minimal_data = {
        "amount": 100.00,
        "transaction_date": "2025-12-23",
        "account": "Savings",
    }

    response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(minimal_data)
    )

    assert response.status_code == 201
    data = response.get_json()

    assert data["amount"] == 100.00
    assert data["currency"] == "SGD"  # Default
    assert data["description"] == ""  # Default
    assert data["account"] == "Savings"
    assert data["category"] is None  # Not provided


def test_create_transaction_unauthenticated(client, sample_transaction_data):
    """Test that creating a transaction without authentication fails."""
    response = client.post(
        "/api/v1/transactions",
        data=json.dumps(sample_transaction_data)
    )

    assert response.status_code == 401
    data = response.get_json()
    assert "error" in data
    assert data["error"]["type"] == "AuthenticationError"


def test_create_transaction_validation_error(client, auth_headers):
    """Test that validation errors are returned for invalid data."""
    invalid_data = {
        "amount": "not a number",
        "transaction_date": "invalid-date",
        "account": "",
    }

    response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(invalid_data)
    )

    assert response.status_code == 400


def test_get_transaction(client, auth_headers, sample_transaction_data):
    """Test getting a single transaction by ID."""
    # First create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()
    transaction_id = transaction["uuid"]

    # Get the transaction
    response = client.get(
        f"/api/v1/transactions/{transaction_id}",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["uuid"] == transaction_id
    assert data["amount"] == sample_transaction_data["amount"]


def test_get_transaction_not_found(client, auth_headers):
    """Test getting a non-existent transaction returns 404."""
    response = client.get(
        "/api/v1/transactions/core_550e8400-e29b-41d4-a716-446655440000",
        headers=auth_headers
    )

    assert response.status_code == 404


def test_list_transactions(client, auth_headers, sample_transaction_data):
    """Test listing all transactions."""
    # Create two transactions
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({
            **sample_transaction_data,
            "amount": 200.00,
            "description": "Second transaction"
        })
    )

    response = client.get(
        "/api/v1/transactions",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert len(data) >= 2


def test_list_transactions_with_filters(client, auth_headers, sample_transaction_data):
    """Test listing transactions with query filters."""
    # Create transactions with different accounts
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({
            **sample_transaction_data,
            "account": "Business",
            "amount": 500.00
        })
    )

    # Filter by account
    response = client.get(
        "/api/v1/transactions?account=Personal",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert all(t["account"] == "Personal" for t in data)


def test_list_transactions_with_date_range(client, auth_headers, sample_transaction_data):
    """Test listing transactions with date range filter."""
    # Create transactions with different dates
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({
            **sample_transaction_data,
            "transaction_date": "2025-01-01",
            "description": "January transaction"
        })
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({
            **sample_transaction_data,
            "transaction_date": "2025-02-01",
            "description": "February transaction"
        })
    )

    # Filter by date range
    response = client.get(
        "/api/v1/transactions?start_date=2025-01-15&end_date=2025-02-15",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert all(t["description"] == "February transaction" for t in data)


def test_list_transactions_pagination(client, auth_headers, sample_transaction_data):
    """Test listing transactions with pagination."""
    # Create multiple transactions
    for i in range(5):
        client.post(
            "/api/v1/transactions",
            headers=auth_headers,
            data=json.dumps({
                **sample_transaction_data,
                "description": f"Transaction {i}"
            })
        )

    # Test limit
    response = client.get(
        "/api/v1/transactions?limit=2",
        headers=auth_headers
    )
    data = response.get_json()
    assert len(data) == 2

    # Test offset
    response = client.get(
        "/api/v1/transactions?limit=2&offset=2",
        headers=auth_headers
    )
    data = response.get_json()
    assert len(data) == 2


def test_update_transaction(client, auth_headers, sample_transaction_data):
    """Test updating a transaction."""
    # Create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()
    transaction_id = transaction["uuid"]

    # Update the transaction
    update_data = {
        "amount": -20.00,
        "description": "Updated coffee",
    }
    response = client.put(
        f"/api/v1/transactions/{transaction_id}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["amount"] == -20.00
    assert data["description"] == "Updated coffee"
    assert data["version"] == 2  # Version incremented


def test_update_transaction_with_conflict_detection(client, auth_headers, sample_transaction_data):
    """Test optimistic locking with conflict detection."""
    # Create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()

    # Try to update with wrong hash (simulating concurrent modification)
    update_data = {
        "amount": -20.00,
        "based_on_hash": "wrong_hash_value",
    }
    response = client.put(
        f"/api/v1/transactions/{transaction['uuid']}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 409
    data = response.get_json()
    assert "message" in data
    assert "current_hash" in data


def test_update_transaction_with_version_conflict(client, auth_headers, sample_transaction_data):
    """Test optimistic locking with version conflict detection."""
    # Create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()

    # Try to update with wrong version
    update_data = {
        "amount": -20.00,
        "based_on_version": 999,  # Wrong version
    }
    response = client.put(
        f"/api/v1/transactions/{transaction['uuid']}",
        headers=auth_headers,
        data=json.dumps(update_data)
    )

    assert response.status_code == 409


def test_delete_transaction(client, auth_headers, sample_transaction_data):
    """Test deleting a transaction (soft delete)."""
    # Create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()
    transaction_id = transaction["uuid"]

    # Delete the transaction
    response = client.delete(
        f"/api/v1/transactions/{transaction_id}",
        headers=auth_headers
    )

    assert response.status_code == 204

    # Verify transaction is superseded (not included in default list)
    list_response = client.get(
        "/api/v1/transactions",
        headers=auth_headers
    )
    transactions = list_response.get_json()
    assert not any(t["uuid"] == transaction_id for t in transactions)


def test_delete_transaction_not_found(client, auth_headers):
    """Test deleting a non-existent transaction returns 404."""
    response = client.delete(
        "/api/v1/transactions/core_550e8400-e29b-41d4-a716-446655440000",
        headers=auth_headers
    )

    assert response.status_code == 404


def test_list_accounts(client, auth_headers, sample_transaction_data):
    """Test listing distinct account labels."""
    # Create transactions with different accounts
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "account": "Personal"})
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "account": "Business"})
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "account": "Personal"})
    )

    response = client.get(
        "/api/v1/transactions/accounts",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert "Personal" in data
    assert "Business" in data


def test_list_categories(client, auth_headers, sample_transaction_data):
    """Test listing distinct category labels."""
    # Create transactions with different categories
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "category": "Food"})
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "category": "Transport"})
    )
    client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps({**sample_transaction_data, "category": "Food"})
    )

    response = client.get(
        "/api/v1/transactions/categories",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    assert "Food" in data
    assert "Transport" in data


def test_transaction_id_with_core_prefix(client, auth_headers, sample_transaction_data):
    """Test that transaction IDs work with core_ prefix."""
    # Create a transaction
    create_response = client.post(
        "/api/v1/transactions",
        headers=auth_headers,
        data=json.dumps(sample_transaction_data)
    )
    transaction = create_response.get_json()
    transaction_id = transaction["uuid"]  # Already has core_ prefix

    # Get with prefix
    response = client.get(
        f"/api/v1/transactions/{transaction_id}",
        headers=auth_headers
    )
    assert response.status_code == 200

    # Get without prefix (strip it)
    transaction_id_no_prefix = transaction_id.replace("core_", "")
    response = client.get(
        f"/api/v1/transactions/{transaction_id_no_prefix}",
        headers=auth_headers
    )
    assert response.status_code == 200


def test_authentication_with_api_key(client, auth_headers_apikey, sample_transaction_data):
    """Test that API key authentication works for transaction endpoints."""
    response = client.post(
        "/api/v1/transactions",
        headers=auth_headers_apikey,
        data=json.dumps(sample_transaction_data)
    )

    assert response.status_code == 201
    data = response.get_json()
    assert "uuid" in data
