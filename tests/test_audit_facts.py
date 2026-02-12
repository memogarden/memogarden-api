"""Tests for Semantic API audit facts (Session 6).

Tests the Action and ActionResult fact creation per RFC-005 v7 Section 7:
- Action fact created at operation start
- ActionResult fact created at operation completion
- result_of relation links ActionResult → Action
- bypass_semantic_api flag prevents recursion
"""

import pytest
from system.soil import get_soil
from system.soil.fact import generate_soil_uuid


class TestAuditFacts:
    """Test audit fact creation for Semantic API operations."""

    def test_create_operation_creates_audit_facts(self, client, auth_headers):
        """Test that create operation creates Action and ActionResult facts."""
        # Create a transaction via Semantic API
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 42.50,
                    "currency": "USD",
                    "account": "Test Account",
                    "category": "Test Category",
                }
            },
            headers=auth_headers
        )

        # Debug: print response if not successful
        if response.status_code != 200:
            print(f"ERROR: Status {response.status_code}")
            print(f"Response: {response.get_json()}")

        # Check response is successful
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        # Get Soil instance and query for audit facts
        with get_soil() as soil:
            # Query for Action facts
            all_items = soil.list_items()
            action_items = [i for i in all_items if i._type == "Action"]
            assert len(action_items) == 1
            action = action_items[0]

            # Verify Action fact structure
            assert action._type == "Action"
            assert action.data["operation"] == "create"
            assert "actor" in action.data
            assert "params" in action.data
            assert "request_id" in action.data
            assert action.data["params"]["type"] == "Transaction"
            assert action.data["params"]["data"]["amount"] == 42.50

            # Query for ActionResult facts
            actionresult_items = [i for i in all_items if i._type == "ActionResult"]
            assert len(actionresult_items) == 1
            actionresult = actionresult_items[0]

            # Verify ActionResult fact structure
            assert actionresult._type == "ActionResult"
            assert actionresult.data["status"] == "success"
            assert "duration_ms" in actionresult.data
            assert actionresult.data["duration_ms"] >= 0
            assert actionresult.data["error"] is None
            assert actionresult.data["result_summary"] is not None

            # Verify result_of relation exists
            relations = soil.get_relations(source=actionresult.uuid, kind="result_of")
            assert len(relations) == 1
            assert relations[0].target == action.uuid
            assert relations[0].kind == "result_of"

    def test_get_operation_creates_audit_facts(self, client, auth_headers):
        """Test that get operation creates audit facts."""
        # First create a transaction
        create_response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 100.00,
                    "currency": "USD",
                    "account": "Test",
                    "category": "Test",
                }
            },
            headers=auth_headers
        )
        assert create_response.status_code == 200
        transaction_data = create_response.get_json()["result"]
        transaction_uuid = transaction_data["uuid"]

        # Now get the transaction
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": transaction_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True

        # Verify audit facts were created
        with get_soil() as soil:
            action_items = [a for a in soil.list_items() if a._type == "Action"]
            get_actions = [a for a in action_items if a.data.get("operation") == "get"]
            assert len(get_actions) >= 1
            assert get_actions[-1].data["operation"] == "get"

            actionresult_items = [ar for ar in soil.list_items() if ar._type == "ActionResult"]
            assert len(actionresult_items) >= 1
            get_results = [ar for ar in actionresult_items if ar.data.get("result_summary", "").startswith("get")]
            assert len(get_results) >= 1

    def test_query_operation_creates_audit_facts(self, client, auth_headers):
        """Test that query operation creates audit facts."""
        # First create some test data
        client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 100.00,
                    "currency": "USD",
                    "account": "Test",
                    "category": "Test",
                }
            },
            headers=auth_headers
        )

        # Clear previous audit facts
        with get_soil() as soil:
            previous_actions = len([i for i in soil.list_items() if i._type == "Action"])

        # Query transactions
        response = client.post(
            "/mg",
            json={
                "op": "query",
                "target_type": "entity",
                "type": "Transaction",
            },
            headers=auth_headers
        )

        assert response.status_code == 200

        # Verify audit facts were created
        with get_soil() as soil:
            action_items = [i for i in soil.list_items() if i._type == "Action"]
            assert len(action_items) == previous_actions + 1
            # Find the query action (most recent by realized_at)
            query_actions = [a for a in action_items if a.data.get("operation") == "query"]
            assert len(query_actions) >= 1

    def test_audit_facts_on_error(self, client, auth_headers):
        """Test that audit facts are created even when operation fails."""
        # Try to get a non-existent entity
        fake_uuid = "core_00000000-0000-0000-0000-000000000000"
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": fake_uuid
            },
            headers=auth_headers
        )

        # Should fail (404)
        assert response.status_code == 404

        # Verify audit facts were still created
        with get_soil() as soil:
            action_items = [i for i in soil.list_items() if i._type == "Action"]
            assert len(action_items) >= 1

            # Find the failed get action
            failed_get = None
            for action in action_items:
                if action.data.get("operation") == "get":
                    params = action.data.get("params", {})
                    if isinstance(params, dict) and params.get("target") == fake_uuid:
                        failed_get = action
                        break

            assert failed_get is not None, "Action fact for failed get not found"

            # Verify ActionResult with error status
            actionresult_items = [i for i in soil.list_items() if i._type == "ActionResult"]
            assert len(actionresult_items) >= 1

            # Find ActionResult for this failed action
            failed_result = None
            relations = soil.get_relations(kind="result_of")
            for relation in relations:
                if relation.target == failed_get.uuid:
                    # Find the ActionResult
                    for ar in actionresult_items:
                        if ar.uuid == relation.source:
                            failed_result = ar
                            break
                break

            assert failed_result is not None, "ActionResult for failed get not found"
            assert failed_result.data["status"] == "error"
            assert failed_result.data["error"] is not None
            # Session 6.6: error is now an object with code, message, details
            assert isinstance(failed_result.data["error"], dict)
            assert "code" in failed_result.data["error"]
            assert "message" in failed_result.data["error"]
            assert "duration_ms" in failed_result.data

    def test_bypass_semantic_api_flag(self, client, auth_headers):
        """Test that bypass_semantic_api flag prevents audit logging."""
        # Create entity with audit logging enabled (default)
        response1 = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 50.00,
                    "currency": "USD",
                    "account": "Test",
                    "category": "Test",
                }
            },
            headers=auth_headers
        )
        assert response1.status_code == 200

        with get_soil() as soil:
            action_count_with_audit = len([i for i in soil.list_items() if i._type == "Action"])

        # Create entity with audit logging disabled
        response2 = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 75.00,
                    "currency": "USD",
                    "account": "Test",
                    "category": "Test",
                },
                "bypass_semantic_api": True
            },
            headers=auth_headers
        )
        assert response2.status_code == 200

        # Verify no new Action fact was created
        with get_soil() as soil:
            action_count_bypass = len([i for i in soil.list_items() if i._type == "Action"])
        assert action_count_with_audit == action_count_bypass

    def test_multiple_operations_create_distinct_audit_facts(self, client, auth_headers):
        """Test that multiple operations create distinct audit facts with unique request IDs."""
        # Perform multiple operations
        operations = [
            {
                "op": "create",
                "type": "Transaction",
                "data": {"date": "2026-02-08", "amount": 10.00, "currency": "USD", "account": "A", "category": "C"}
            },
            {
                "op": "create",
                "type": "Transaction",
                "data": {"date": "2026-02-08", "amount": 20.00, "currency": "USD", "account": "B", "category": "D"}
            },
            {
                "op": "query",
                "target_type": "entity",
                "type": "Transaction"
            }
        ]

        for op in operations:
            response = client.post("/mg", json=op, headers=auth_headers)
            assert response.status_code == 200

        # Verify distinct Action facts with unique request IDs
        with get_soil() as soil:
            action_items = [i for i in soil.list_items() if i._type == "Action"]
            assert len(action_items) >= 3

            # Extract request IDs
            request_ids = [action.data.get("request_id") for action in action_items]
            # Filter out None values (in case of any)
            request_ids = [rid for rid in request_ids if rid is not None]

            # Verify all request IDs are unique
            assert len(request_ids) == len(set(request_ids)), "All request IDs should be unique"

    def test_audit_fact_params_serialization(self, client, auth_headers):
        """Test that audit facts properly serialize request parameters."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 99.99,
                    "currency": "USD",
                    "account": "Test Account",
                    "category": "Test Category",
                    "notes": "This is a test transaction with special chars: <>&\"'",
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200

        # Verify params are serialized correctly
        with get_soil() as soil:
            action_items = [i for i in soil.list_items() if i._type == "Action"]
            assert len(action_items) >= 1

            # Find the create action (most recent with amount 99.99)
            create_actions = [a for a in action_items if a.data.get("operation") == "create" and a.data.get("params", {}).get("data", {}).get("amount") == 99.99]
            assert len(create_actions) >= 1
            action = create_actions[-1]
            params = action.data.get("params", {})
            assert isinstance(params, dict)
            assert params["type"] == "Transaction"
            assert params["data"]["amount"] == 99.99
            assert params["data"]["notes"] == "This is a test transaction with special chars: <>&\"'"

    def test_result_relation_structure(self, client, auth_headers):
        """Test that result_of relation has correct structure."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Label",
                "data": {
                    "name": "Test Label",
                    "color": "blue",
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200

        # Get the latest Action and ActionResult
        with get_soil() as soil:
            actions = [i for i in soil.list_items() if i._type == "Action"]
            results = [i for i in soil.list_items() if i._type == "ActionResult"]

            assert len(actions) >= 1
            assert len(results) >= 1

            latest_action = actions[-1]
            latest_result = results[-1]

            # Get the result_of relation
            relations = soil.get_relations(source=latest_result.uuid, kind="result_of")
            assert len(relations) == 1

            relation = relations[0]
            assert relation.kind == "result_of"
            assert relation.source == latest_result.uuid
            assert relation.target == latest_action.uuid
            assert relation.source_type == "item"
            assert relation.target_type == "item"
            assert relation.evidence is not None
            assert relation.evidence.get("source") == "system_inferred"
            assert relation.evidence.get("method") == "audit_logging"


# ============================================================================
# Session 6.6: Structured Error Capture Tests
# ============================================================================

class TestStructuredErrorCapture:
    """Test structured error capture in ActionResult facts (RFC-005 v7.1)."""

    def test_not_found_error_has_structured_error(self, client, auth_headers):
        """Test that not_found error has code, message, and details."""
        # Try to get a non-existent entity
        fake_uuid = "core_00000000-0000-0000-0000-000000000000"
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": fake_uuid
            },
            headers=auth_headers
        )

        # Should fail (404)
        assert response.status_code == 404

        # Verify structured error in ActionResult
        with get_soil() as soil:
            # Find the failed action
            action_items = [i for i in soil.list_items() if i._type == "Action"]
            failed_get = None
            for action in action_items:
                if action.data.get("operation") == "get":
                    params = action.data.get("params", {})
                    if isinstance(params, dict) and params.get("target") == fake_uuid:
                        failed_get = action
                        break

            assert failed_get is not None, "Action fact for failed get not found"

            # Find the ActionResult
            actionresult_items = [i for i in soil.list_items() if i._type == "ActionResult"]
            relations = soil.get_relations(kind="result_of")
            failed_result = None
            for relation in relations:
                if relation.target == failed_get.uuid:
                    for ar in actionresult_items:
                        if ar.uuid == relation.source:
                            failed_result = ar
                            break
                if failed_result:
                    break

            assert failed_result is not None
            assert failed_result.data["status"] == "error"

            # Verify structured error format (RFC-005 v7.1)
            error = failed_result.data["error"]
            assert error is not None
            assert isinstance(error, dict), "Error should be an object"
            assert "code" in error, "Error should have 'code' field"
            assert "message" in error, "Error should have 'message' field"
            assert error["code"] == "not_found"
            assert error["message"] is not None
            assert isinstance(error["message"], str)

            # Verify error_type and error_traceback are present
            assert "error_type" in failed_result.data
            assert "error_traceback" in failed_result.data
            assert failed_result.data["error_type"] is not None
            assert failed_result.data["error_traceback"] is not None

    def test_validation_error_has_validation_code(self, client, auth_headers):
        """Test that validation errors have validation_error code.

        Note: Since the API doesn't yet have field-level validation,
        this test verifies that when a ValidationError is raised,
        it gets properly classified with the validation_error code.
        """
        from api.handlers.decorators import _get_error_code
        from system.exceptions import ValidationError

        # Test error code mapping for ValidationError
        exc = ValidationError("Invalid field value")
        code = _get_error_code(exc)
        assert code == "validation_error"

        # Also verify error details extraction works
        from api.handlers.decorators import _extract_error_details
        exc_with_details = ValidationError(
            "Invalid field",
            details={"field": "amount", "expected": "positive", "got": -100}
        )
        details = _extract_error_details(exc_with_details)
        assert details is not None
        assert details["field"] == "amount"
        assert details["expected"] == "positive"
        assert details["got"] == -100

    def test_permission_denied_error_structure(self, client, auth_headers):
        """Test that permission_denied errors have correct structure.

        Note: This test creates a scenario that would trigger permission denied.
        Since we don't have full permissions implemented yet, we test the
        error code mapping function directly.
        """
        from api.handlers.decorators import _get_error_code
        from system.exceptions import PermissionDenied

        # Test error code mapping
        exc = PermissionDenied("Access denied")
        code = _get_error_code(exc)
        assert code == "permission_denied"

    def test_lock_conflict_error_structure(self):
        """Test that lock_conflict errors have correct structure."""
        from api.handlers.decorators import _get_error_code
        from system.exceptions import LockConflictError

        # Test error code mapping
        exc = LockConflictError("Entity was modified by another transaction")
        code = _get_error_code(exc)
        assert code == "lock_conflict"

    def test_internal_error_fallback(self):
        """Test that unknown exceptions map to internal_error."""
        from api.handlers.decorators import _get_error_code

        # Test with generic exception
        exc = ValueError("Some unknown error")
        code = _get_error_code(exc)
        assert code == "internal_error"

    def test_error_details_extraction(self):
        """Test that error details are extracted correctly."""
        from api.handlers.decorators import _extract_error_details
        from system.exceptions import ValidationError, ResourceNotFound

        # Test MemoGardenError with details
        exc1 = ValidationError("Invalid field", details={"field": "amount", "value": -100})
        details1 = _extract_error_details(exc1)
        assert details1 is not None
        assert details1["field"] == "amount"
        assert details1["value"] == -100

        # Test ResourceNotFound (extracts resource info)
        exc2 = ResourceNotFound("Entity core_abc123 not found")
        details2 = _extract_error_details(exc2)
        assert details2 is not None
        assert "resource" in details2

        # Test generic exception (no details)
        exc3 = ValueError("Generic error")
        details3 = _extract_error_details(exc3)
        assert details3 is None

    def test_error_details_in_actionresult(self, client, auth_headers):
        """Test that error details are stored in ActionResult.data."""
        # Try to get a non-existent entity
        fake_uuid = "core_00000000-0000-0000-0000-000000000000"
        response = client.post(
            "/mg",
            json={
                "op": "get",
                "target": fake_uuid
            },
            headers=auth_headers
        )

        assert response.status_code == 404

        # Verify error details in ActionResult
        with get_soil() as soil:
            actionresult_items = [i for i in soil.list_items() if i._type == "ActionResult"]
            error_results = [ar for ar in actionresult_items if ar.data.get("status") == "error"]
            assert len(error_results) >= 1

            latest_error = error_results[-1]
            error = latest_error.data["error"]

            # error.details may or may not be present depending on exception type
            # but error.code and error.message must be present
            assert "code" in error
            assert "message" in error

    def test_successful_operation_has_no_error(self, client, auth_headers):
        """Test that successful operations have error: null."""
        response = client.post(
            "/mg",
            json={
                "op": "create",
                "type": "Transaction",
                "data": {
                    "date": "2026-02-08",
                    "amount": 100.00,
                    "currency": "USD",
                    "account": "Test",
                    "category": "Test",
                }
            },
            headers=auth_headers
        )

        assert response.status_code == 200

        # Verify error is null for successful operations
        with get_soil() as soil:
            actionresult_items = [i for i in soil.list_items() if i._type == "ActionResult"]
            success_results = [ar for ar in actionresult_items if ar.data.get("status") == "success"]
            assert len(success_results) >= 1

            latest_success = success_results[-1]
            assert latest_success.data["error"] is None
            assert latest_success.data["result"] is not None
