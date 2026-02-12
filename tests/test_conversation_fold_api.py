"""Tests for conversation fold API endpoint (Session 18).

Tests fold operation through the Semantic API:
- Request validation
- Fold operation execution
- Response format
"""

import pytest


class TestFoldAPI:
    """Test fold verb through Semantic API."""

    def test_fold_request_validation(self, client, auth_headers):
        """Fold request validates correctly."""
        # First create a ConversationLog
        create_response = client.post("/mg", json={
            "op": "create",
            "type": "ConversationLog",
            "data": {
                "parent_uuid": None,
                "items": [],
            }
        }, headers=auth_headers)
        assert create_response.status_code == 200
        log_uuid = create_response.json["result"]["uuid"]

        # Now fold it
        response = client.post("/mg", json={
            "op": "fold",
            "target": log_uuid,
            "summary_content": "Summary of conversation",
            "author": "operator",
        }, headers=auth_headers)

        assert response.status_code == 200
        result = response.json["result"]
        assert result["collapsed"] is True
        assert result["summary"]["content"] == "Summary of conversation"
        assert result["summary"]["author"] == "operator"
        assert "timestamp" in result["summary"]

    def test_fold_with_fragment_ids(self, client, auth_headers):
        """Fold request includes fragment IDs."""
        # Create a ConversationLog
        create_response = client.post("/mg", json={
            "op": "create",
            "type": "ConversationLog",
            "data": {"parent_uuid": None, "items": []}
        }, headers=auth_headers)
        log_uuid = create_response.json["result"]["uuid"]

        # Fold with fragments
        response = client.post("/mg", json={
            "op": "fold",
            "target": log_uuid,
            "summary_content": "Used fragments ^abc and ^def",
            "author": "agent",
            "fragment_ids": ["^abc", "^def"],
        }, headers=auth_headers)

        assert response.status_code == 200
        result = response.json["result"]
        assert result["summary"]["fragment_ids"] == ["^abc", "^def"]

    def test_fold_empty_summary_returns_error(self, client, auth_headers):
        """Fold request with empty summary returns validation error."""
        # Create a ConversationLog
        create_response = client.post("/mg", json={
            "op": "create",
            "type": "ConversationLog",
            "data": {"parent_uuid": None, "items": []}
        }, headers=auth_headers)
        log_uuid = create_response.json["result"]["uuid"]

        # Try to fold with empty summary
        response = client.post("/mg", json={
            "op": "fold",
            "target": log_uuid,
            "summary_content": "",
            "author": "operator",
        }, headers=auth_headers)

        assert response.status_code == 400
        # Check for validation error
        assert response.json["ok"] is False

    def test_fold_invalid_author_returns_error(self, client, auth_headers):
        """Fold request with invalid author returns validation error."""
        # Create a ConversationLog
        create_response = client.post("/mg", json={
            "op": "create",
            "type": "ConversationLog",
            "data": {"parent_uuid": None, "items": []}
        }, headers=auth_headers)
        log_uuid = create_response.json["result"]["uuid"]

        # Try to fold with invalid author
        response = client.post("/mg", json={
            "op": "fold",
            "target": log_uuid,
            "summary_content": "Summary",
            "author": "invalid_author",
        }, headers=auth_headers)

        assert response.status_code == 400

    def test_fold_nonexistent_log_returns_error(self, client, auth_headers):
        """Fold request on non-existent log returns 404."""
        response = client.post("/mg", json={
            "op": "fold",
            "target": "core_nonexistent123",
            "summary_content": "Summary",
            "author": "operator",
        }, headers=auth_headers)

        assert response.status_code == 404

    def test_fold_by_system_author(self, client, auth_headers):
        """Fold request with system author."""
        # Create a ConversationLog
        create_response = client.post("/mg", json={
            "op": "create",
            "type": "ConversationLog",
            "data": {"parent_uuid": None, "items": []}
        }, headers=auth_headers)
        log_uuid = create_response.json["result"]["uuid"]

        # Fold by system
        response = client.post("/mg", json={
            "op": "fold",
            "target": log_uuid,
            "summary_content": "System-generated summary",
            "author": "system",
        }, headers=auth_headers)

        assert response.status_code == 200
        assert response.json["result"]["summary"]["author"] == "system"
