"""Tests for provider accounts (RFC-012): registry CRUD, scheduling, the
per-account runtime, auth-failure parking, verification, and account-scoped
scope policies.

Account-backed provider modules are generated into tmp_path and made
importable via sys.path — same pattern as test_providers.py.
"""

import time
import uuid

import pytest


ACCOUNTED_MODULE_TEMPLATE = '''
class {class_name}:
    PLATFORMS = ["google"]

    def __init__(self, config=None, account=None):
        self.config = config or {{}}
        self.account = account

    def sync(self, since=None):
        subject = self.account["subject"] if self.account else "no-account"
        merged_config = {{**self.config, **(self.account or {{}}).get("config", {{}})}}
        yield {{
            "type": "Note",
            "data": {{"description": "from %s" % subject}},
            "metadata": {{
                "config_marker": merged_config.get("marker", "none"),
                "account": subject,
            }},
        }}

    @staticmethod
    def verify_account(account, config):
        if account["credential_ref"] == "google:bad@gmail.com":
            return {{"ok": False, "error": "token revoked upstream"}}
        return {{"ok": True, "subject": account["subject"]}}
'''


AUTH_FAILURE_MODULE_TEMPLATE = '''
class {class_name}:
    PLATFORMS = ["google"]

    def __init__(self, config=None, account=None):
        self.config = config or {{}}

    def sync(self, since=None):
        error = RuntimeError("401 invalid_grant: token expired")
        error.auth_failure = True
        raise error
        yield  # pragma: no cover — never reached
'''


@pytest.fixture
def accounted_module(tmp_path, monkeypatch):
    """An account-backed provider module (PLATFORMS + account kwarg + verify hook)."""
    module_name = f"acct_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        ACCOUNTED_MODULE_TEMPLATE.format(class_name="AccountedProvider")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


@pytest.fixture
def authfail_module(tmp_path, monkeypatch):
    """An account-backed provider whose sync always raises an auth failure."""
    module_name = f"authfail_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        AUTH_FAILURE_MODULE_TEMPLATE.format(class_name="AuthFailProvider")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


@pytest.fixture
def plain_module(tmp_path, monkeypatch):
    """A valid but NOT account-backed provider (no account constructor param)."""
    module_name = f"plain_provider_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module_name}.py").write_text(
        "class PlainProvider:\n"
        "    def __init__(self, config=None):\n"
        "        self.config = config or {}\n"
        "    def sync(self, since=None):\n"
        "        yield {\"type\": \"Note\", \"data\": {\"description\": \"plain\"}, \"metadata\": {}}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_name


def _provider_payload(module, **overrides):
    payload = {
        "name": "testprov",
        "module": module,
        "class": "AccountedProvider",
        "interval": 3600,
        "config": {"marker": "provider-level"},
    }
    payload.update(overrides)
    return payload


def _register_provider(client, auth_headers, payload):
    return client.post("/api/v1/providers", json=payload, headers=auth_headers)


def _register_account(client, auth_headers, name, subject="a@gmail.com", **overrides):
    payload = {"platform": "google", "subject": subject}
    payload.update(overrides)
    return client.post(
        f"/api/v1/providers/{name}/accounts", json=payload, headers=auth_headers
    )


def _get_account(client, auth_headers, name, account_id):
    return client.get(
        f"/api/v1/providers/{name}/accounts/{account_id}", headers=auth_headers
    ).get_json()


def _wait_for_account(client, auth_headers, name, account_id, timeout=5.0):
    """Poll the account manifest until last_status leaves the unset state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        account = _get_account(client, auth_headers, name, account_id)
        if account.get("last_status") in ("success", "failed"):
            return account
        time.sleep(0.05)
    raise AssertionError(f"account run for {name}/{account_id} did not finish within {timeout}s")


def _notes(client, auth_headers):
    query = client.post(
        "/mg",
        json={"op": "query", "target_type": "fact", "type": "Note"},
        headers=auth_headers,
    )
    assert query.status_code == 200, query.get_json()
    return {r["data"]["description"]: r for r in query.get_json()["result"]["results"]}


class TestAccountCRUD:
    def test_register_account_success(self, client, auth_headers, accounted_module):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        response = _register_account(client, auth_headers, "testprov")
        assert response.status_code == 201
        account = response.get_json()
        assert account["provider_name"] == "testprov"
        assert account["platform"] == "google"
        assert account["subject"] == "a@gmail.com"
        assert account["credential_ref"] == "google:a@gmail.com"  # convention default
        assert account["status"] == "active"
        assert account["config"] == {}
        assert account["next_due_at"] is None  # NULL = due now
        assert account["created_at"]

    def test_register_account_custom_ref_and_config(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        response = _register_account(
            client, auth_headers, "testprov", subject="b@gmail.com",
            credential_ref="google:custom", label="work", config={"batch_limit": 500},
        )
        assert response.status_code == 201
        account = response.get_json()
        assert account["credential_ref"] == "google:custom"
        assert account["label"] == "work"
        assert account["config"] == {"batch_limit": 500}

    def test_register_account_duplicate_subject_conflict(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        assert _register_account(client, auth_headers, "testprov").status_code == 201
        response = _register_account(client, auth_headers, "testprov")
        assert response.status_code == 409

    def test_register_account_unknown_provider_404(self, client, auth_headers, accounted_module):
        response = _register_account(client, auth_headers, "nosuchprov")
        assert response.status_code == 404

    def test_register_account_rejects_platform_mismatch(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        response = _register_account(
            client, auth_headers, "testprov", subject="x@github.example",
            platform="github",
        )
        assert response.status_code == 400
        assert "platform" in response.get_json()["error"]["message"]

    def test_register_account_rejects_non_account_provider(
        self, client, auth_headers, plain_module
    ):
        """A provider class without an `account` constructor parameter is not
        account-backed — registering an account for it would double-run it."""
        plain_payload = {
            "name": "plainprov",
            "module": plain_module,
            "class": "PlainProvider",
            "interval": 3600,
        }
        assert _register_provider(client, auth_headers, plain_payload).status_code == 201
        response = _register_account(client, auth_headers, "plainprov")
        assert response.status_code == 400
        assert "not account-backed" in response.get_json()["error"]["message"]

    def test_list_and_get_accounts(self, client, auth_headers, accounted_module):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        first = _register_account(client, auth_headers, "testprov").get_json()
        second = _register_account(
            client, auth_headers, "testprov", subject="b@gmail.com"
        ).get_json()

        listing = client.get(
            "/api/v1/providers/testprov/accounts", headers=auth_headers
        ).get_json()
        assert listing["count"] == 2
        assert {a["subject"] for a in listing["accounts"]} == {"a@gmail.com", "b@gmail.com"}

        got = _get_account(client, auth_headers, "testprov", second["id"])
        assert got["id"] == second["id"]

        missing = client.get(
            f"/api/v1/providers/testprov/accounts/{uuid.uuid4()}", headers=auth_headers
        )
        assert missing.status_code == 404

    def test_patch_account(self, client, auth_headers, accounted_module):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()

        response = client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"label": "work", "status": "disabled", "interval": 120,
                  "config": {"batch_limit": 200}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        updated = response.get_json()
        assert updated["label"] == "work"
        assert updated["status"] == "disabled"
        assert updated["interval"] == 120
        assert updated["config"] == {"batch_limit": 200}

    def test_patch_account_invalid_status_rejected(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        response = client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"status": "quantum"},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_delete_account(self, client, auth_headers, accounted_module):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        response = client.delete(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 204
        assert client.get(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            headers=auth_headers,
        ).status_code == 404


class TestAccountScheduling:
    def test_account_backed_provider_excluded_from_due_providers(
        self, client, auth_headers, accounted_module
    ):
        from api import provider_registry

        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        _register_account(client, auth_headers, "testprov")

        assert provider_registry.get_due_providers() == []
        due_accounts = provider_registry.get_due_accounts()
        assert [a["subject"] for a in due_accounts] == ["a@gmail.com"]
        assert due_accounts[0]["effective_interval"] == 3600

    def test_account_interval_overrides_provider(
        self, client, auth_headers, accounted_module
    ):
        from api import provider_registry

        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"interval": 60},
            headers=auth_headers,
        )

        due = provider_registry.get_due_accounts()
        assert due[0]["effective_interval"] == 60

    def test_needs_reauth_account_not_due(self, client, auth_headers, accounted_module):
        from api import provider_registry

        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"status": "needs_reauth"},
            headers=auth_headers,
        )
        assert provider_registry.get_due_accounts() == []

    def test_disabled_provider_accounts_not_due(self, client, auth_headers, accounted_module):
        from api import provider_registry

        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module, enabled=False)
        ).status_code == 201
        _register_account(client, auth_headers, "testprov")
        assert provider_registry.get_due_accounts() == []


class TestAccountRuntime:
    def test_account_run_inserts_account_context_fact(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()

        response = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/run",
            headers=auth_headers,
        )
        assert response.status_code == 202
        assert response.get_json()["subject"] == "a@gmail.com"

        finished = _wait_for_account(client, auth_headers, "testprov", account["id"])
        assert finished["last_status"] == "success"
        assert finished["next_due_at"] is not None  # scheduled per account

        notes = _notes(client, auth_headers)
        assert "from a@gmail.com" in notes

    def test_account_config_overrides_provider_config(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(
            client, auth_headers, "testprov", config={"marker": "account-level"}
        ).get_json()
        client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/run",
            headers=auth_headers,
        )
        _wait_for_account(client, auth_headers, "testprov", account["id"])

        notes = _notes(client, auth_headers)
        assert notes["from a@gmail.com"]["metadata"]["config_marker"] == "account-level"

    def test_auth_failure_parks_account(self, client, auth_headers, authfail_module):
        assert _register_provider(
            client, auth_headers,
            _provider_payload(authfail_module, **{"class": "AuthFailProvider"}),
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()

        client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/run",
            headers=auth_headers,
        )
        finished = _wait_for_account(client, auth_headers, "testprov", account["id"])
        assert finished["last_status"] == "failed"
        # RFC-012 §4.2: an auth failure parks the account in needs_reauth
        assert finished["status"] == "needs_reauth"
        assert "invalid_grant" in finished["error"]

    def test_manual_provider_run_starts_all_active_accounts(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        first = _register_account(client, auth_headers, "testprov").get_json()
        second = _register_account(
            client, auth_headers, "testprov", subject="b@gmail.com"
        ).get_json()
        parked = _register_account(
            client, auth_headers, "testprov", subject="c@gmail.com"
        ).get_json()
        client.patch(
            f"/api/v1/providers/testprov/accounts/{parked['id']}",
            json={"status": "needs_reauth"},
            headers=auth_headers,
        )

        response = client.post("/api/v1/providers/testprov/run", headers=auth_headers)
        assert response.status_code == 202
        body = response.get_json()
        assert {t["subject"] for t in body["triggered"]} == {"a@gmail.com", "b@gmail.com"}
        assert body["skipped"] == [{
            "account_id": parked["id"],
            "subject": "c@gmail.com",
            "reason": "status is needs_reauth",
        }]

        _wait_for_account(client, auth_headers, "testprov", first["id"])
        _wait_for_account(client, auth_headers, "testprov", second["id"])
        notes = _notes(client, auth_headers)
        assert "from a@gmail.com" in notes and "from b@gmail.com" in notes

    def test_heartbeat_runs_due_accounts(self, client, auth_headers, accounted_module):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(
            client, auth_headers, "testprov", subject="hb@gmail.com"
        ).get_json()

        response = client.post("/api/v1/providers/run", headers=auth_headers)
        assert response.status_code == 202
        body = response.get_json()
        assert body["skipped"] == []
        assert len(body["triggered"]) == 1
        assert body["triggered"][0]["subject"] == "hb@gmail.com"

        _wait_for_account(client, auth_headers, "testprov", account["id"])


class TestAccountVerification:
    def test_verify_without_hook_rejected(self, client, auth_headers, authfail_module):
        """The auth-failure provider is account-backed but ships no
        verify_account hook — server-side verification is unsupported."""
        assert _register_provider(
            client, auth_headers,
            _provider_payload(authfail_module, **{"class": "AuthFailProvider"}),
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        response = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/verify",
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "verification" in response.get_json()["error"]["message"]

    def test_verify_reactivates_parked_account(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"status": "needs_reauth"},
            headers=auth_headers,
        )

        response = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/verify",
            headers=auth_headers,
        )
        assert response.status_code == 200
        verified = response.get_json()
        assert verified["status"] == "active"
        assert verified["error"] is None
        assert verified["last_verified_at"]

    def test_verify_failure_parks_account(
        self, client, auth_headers, accounted_module
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(
            client, auth_headers, "testprov", subject="bad@gmail.com",
            credential_ref="google:bad@gmail.com",
        ).get_json()
        response = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/verify",
            headers=auth_headers,
        )
        assert response.status_code == 200
        verified = response.get_json()
        assert verified["status"] == "needs_reauth"
        assert verified["error"] == "token revoked upstream"

    def test_verify_does_not_resurrect_disabled(
        self, client, auth_headers, accounted_module
    ):
        """A passing probe records the outcome but respects operator holds —
        disabled/revoked accounts stay parked (RFC-012 §4.2)."""
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        account = _register_account(client, auth_headers, "testprov").get_json()
        client.patch(
            f"/api/v1/providers/testprov/accounts/{account['id']}",
            json={"status": "disabled"},
            headers=auth_headers,
        )
        response = client.post(
            f"/api/v1/providers/testprov/accounts/{account['id']}/verify",
            headers=auth_headers,
        )
        assert response.status_code == 200
        verified = response.get_json()
        assert verified["status"] == "disabled"
        assert verified["error"] is None
        assert verified["last_verified_at"]


class TestAccountScopePolicy:
    """RFC-012 §4.8: metadata.account is immediately matchable by the
    RFC-011 scope policy — per-account labeling without grants changes."""

    @pytest.fixture
    def triage_uuid(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Triage / Untrusted"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        return response.get_json()["result"]["uuid"]

    @pytest.fixture
    def finances_uuid(self, client, auth_headers):
        response = client.post(
            "/mg",
            json={"op": "create", "type": "Scope", "data": {"label": "Finances"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        return response.get_json()["result"]["uuid"]

    def test_policy_labels_per_account(
        self, client, auth_headers, accounted_module, triage_uuid, finances_uuid
    ):
        assert _register_provider(
            client, auth_headers, _provider_payload(accounted_module)
        ).status_code == 201
        put = client.put(
            "/api/v1/providers/testprov/scope-policy",
            json={
                "rules": [{"match": {"account": "a@gmail.com"}, "scope": finances_uuid}],
                "default": "triage",
            },
            headers=auth_headers,
        )
        assert put.status_code == 200

        for subject in ("a@gmail.com", "b@gmail.com"):
            account = _register_account(client, auth_headers, "testprov", subject=subject).get_json()
            client.post(
                f"/api/v1/providers/testprov/accounts/{account['id']}/run",
                headers=auth_headers,
            )
            _wait_for_account(client, auth_headers, "testprov", account["id"])

        def _plain(prefixed):
            return prefixed.split("core_", 1)[-1]

        notes = _notes(client, auth_headers)
        assert notes["from a@gmail.com"]["metadata"]["scopes"] == [_plain(finances_uuid)]
        assert notes["from b@gmail.com"]["metadata"]["scopes"] == [_plain(triage_uuid)]
