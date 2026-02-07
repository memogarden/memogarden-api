"""
Tests for authentication endpoints.

Tests cover:
- POST /auth/register       - Admin registration
- POST /auth/login          - User login
- GET  /auth/me             - Get current user profile
"""

import json


def test_health_check(client):
    """Test the health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"


def test_admin_registration(client):
    """Test admin user registration."""
    # Set bypass_localhost_check to true for testing
    import os
    os.environ["BYPASS_LOCALHOST_CHECK"] = "true"

    admin_data = {
        "username": "admin",
        "password": "SecurePass123"
    }

    response = client.post(
        "/admin/register",
        data=json.dumps(admin_data),
        content_type="application/json"
    )

    assert response.status_code in (200, 201)
    data = response.get_json()
    assert "user" in data
    assert data["user"]["username"] == "admin"
    assert data["user"]["is_admin"] is True


def test_admin_registration_duplicate(client):
    """Test that duplicate admin registration fails."""
    import os
    os.environ["BYPASS_LOCALHOST_CHECK"] = "true"

    admin_data = {
        "username": "admin",
        "password": "SecurePass123"
    }

    # First registration should succeed
    response = client.post(
        "/admin/register",
        data=json.dumps(admin_data),
        content_type="application/json"
    )
    assert response.status_code in (200, 201)

    # Second registration should fail
    response = client.post(
        "/admin/register",
        data=json.dumps(admin_data),
        content_type="application/json"
    )
    assert response.status_code == 401


def test_admin_registration_weak_password(client):
    """Test that weak passwords are rejected."""
    import os
    os.environ["BYPASS_LOCALHOST_CHECK"] = "true"

    # Test no digit
    response = client.post(
        "/admin/register",
        data=json.dumps({"username": "admin2", "password": "NoDigits"}),
        content_type="application/json"
    )
    assert response.status_code == 400

    # Test no letter
    response = client.post(
        "/admin/register",
        data=json.dumps({"username": "admin3", "password": "12345678"}),
        content_type="application/json"
    )
    assert response.status_code == 400

    # Test too short
    response = client.post(
        "/admin/register",
        data=json.dumps({"username": "admin4", "password": "Short1"}),
        content_type="application/json"
    )
    assert response.status_code == 400


def test_login(client, test_user_app):
    """Test user login."""
    login_data = {
        "username": test_user_app["username"],
        "password": test_user_app["password"]
    }

    response = client.post(
        "/auth/login",
        data=json.dumps(login_data),
        content_type="application/json"
    )

    assert response.status_code == 200
    data = response.get_json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert "user" in data
    assert data["user"]["username"] == test_user_app["username"]


def test_login_wrong_password(client, test_user_app):
    """Test login with wrong password fails."""
    login_data = {
        "username": test_user_app["username"],
        "password": "WrongPassword123"
    }

    response = client.post(
        "/auth/login",
        data=json.dumps(login_data),
        content_type="application/json"
    )

    assert response.status_code == 401


def test_login_nonexistent_user(client):
    """Test login with non-existent user fails."""
    login_data = {
        "username": "nonexistent",
        "password": "SomePass123"
    }

    response = client.post(
        "/auth/login",
        data=json.dumps(login_data),
        content_type="application/json"
    )

    assert response.status_code == 401


def test_get_current_user(client, auth_headers):
    """Test getting current user profile."""
    response = client.get(
        "/auth/me",
        headers=auth_headers
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["username"] == "testuser"
    assert "id" in data
    assert "is_admin" in data


def test_get_current_user_unauthenticated(client):
    """Test that getting profile without authentication fails."""
    response = client.get("/auth/me")

    assert response.status_code == 401
