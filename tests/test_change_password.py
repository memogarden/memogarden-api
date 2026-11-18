"""
Tests for POST /auth/change-password (RFC-015 §3.1).

Covers the rotation path that never existed before: JWT-gated, current
password required, registration password policy enforced, and the new
password actually authenticates.
"""

import json


def test_change_password_success(client, auth_headers):
    """Correct current password rotates; old password stops working."""
    response = client.post(
        "/auth/change-password",
        data=json.dumps({"current_password": "TestPass123", "new_password": "NewPass456"}),
        content_type="application/json",
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Old password no longer authenticates; new one does.
    old_login = client.post(
        "/auth/login",
        data=json.dumps({"username": "testuser", "password": "TestPass123"}),
        content_type="application/json",
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/auth/login",
        data=json.dumps({"username": "testuser", "password": "NewPass456"}),
        content_type="application/json",
    )
    assert new_login.status_code == 200
    assert new_login.get_json()["access_token"]


def test_change_password_wrong_current(client, auth_headers):
    """A wrong current password is refused; password unchanged."""
    response = client.post(
        "/auth/change-password",
        data=json.dumps({"current_password": "WrongPass1", "new_password": "NewPass456"}),
        content_type="application/json",
        headers=auth_headers,
    )
    assert response.status_code == 401

    # Original still works
    login = client.post(
        "/auth/login",
        data=json.dumps({"username": "testuser", "password": "TestPass123"}),
        content_type="application/json",
    )
    assert login.status_code == 200


def test_change_password_requires_jwt(client, auth_headers_apikey):
    """API keys cannot rotate passwords — credentials cannot manage credentials."""
    response = client.post(
        "/auth/change-password",
        data=json.dumps({"current_password": "TestPass123", "new_password": "NewPass456"}),
        content_type="application/json",
        headers={"X-API-Key": auth_headers_apikey["X-API-Key"]},
    )
    assert response.status_code == 401


def test_change_password_unauthenticated(client):
    response = client.post(
        "/auth/change-password",
        data=json.dumps({"current_password": "TestPass123", "new_password": "NewPass456"}),
        content_type="application/json",
    )
    assert response.status_code == 401


def test_change_password_weak_new_rejected(client, auth_headers):
    """New password follows the registration policy (letter + digit, min 8)."""
    for bad in ("NoDigitsHere", "12345678", "Short1"):
        response = client.post(
            "/auth/change-password",
            data=json.dumps({"current_password": "TestPass123", "new_password": bad}),
            content_type="application/json",
            headers=auth_headers,
        )
        assert response.status_code == 400, bad

    # And the original password still works after the failures.
    login = client.post(
        "/auth/login",
        data=json.dumps({"username": "testuser", "password": "TestPass123"}),
        content_type="application/json",
    )
    assert login.status_code == 200
