"""Pydantic schemas for the provider registry API (RFC-004 §12.2).

Mirrors the client-side model in mg_client.providers so the CLI and the
server agree on the wire schema. `class` is a JSON field name; the Python
attribute is class_name with a populate-by-name alias.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RegisterProviderRequest(BaseModel):
    """Payload for POST /api/v1/providers (RFC-004 §12.2)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    module: str = Field(min_length=1)
    class_name: str = Field(alias="class", min_length=1)
    interval: int = Field(default=3600, ge=60)
    enabled: bool = True
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateProviderRequest(BaseModel):
    """Payload for PATCH /api/v1/providers/{name}.

    Fields omitted stay unchanged. `config` replaces the whole config object
    (it is provider-opaque, so partial merges are the provider's concern).
    module and class are deliberately immutable — registration-time validation
    (import + Protocol conformance) would have to be re-run; re-register
    instead once deletion exists.
    """

    interval: int | None = Field(default=None, ge=60)
    enabled: bool | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    # Capability refresh (tada entity lane): re-derive capabilities from the
    # module's CURRENT on-disk source (bypassing the interpreter's module
    # cache) — provider code evolves while module identity is fixed. A flag,
    # not a column.
    refresh_capabilities: bool = False


class ScopePolicyRule(BaseModel):
    """One rule of a provider scope policy (RFC-011 §4.8, item 4).

    match: exact-equality fields evaluated against a merged view of the
    created fact's data and metadata. scope: the target scope (uuid or
    builtin alias like "triage"); null = deliver unscoped.
    """

    match: dict[str, Any] = Field(default_factory=dict)
    scope: str | None = None


class ScopePolicyRequest(BaseModel):
    """Payload for PUT /api/v1/providers/{name}/scope-policy (RFC-011 §4.8).

    First-match-wins rules; the default catch-all routes unmatched items
    to triage when omitted (ratified 2026-11-12 — fail-closed). Set
    {"default": null} for an unscoped-by-default provider (e.g. task sync).
    """

    rules: list[ScopePolicyRule] = Field(default_factory=list)
    default: str | None = Field(
        default="triage",
        description="Catch-all scope ref (uuid or builtin alias); null = unscoped",
    )


class RegisterAccountRequest(BaseModel):
    """Payload for POST /api/v1/providers/{name}/accounts (RFC-012 §6.1).

    Binds one upstream identity to the provider. Tokens never transit the
    API: the OAuth consent happens CLI-local and the secret lives in HACM;
    `credential_ref` is only a reference (defaults to the {platform}:{subject}
    convention). `subject` must already be verified against the credential
    by the linking flow (RFC-012 INV-3) — the server records, it does not
    attest.
    """

    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    subject: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=120)
    credential_ref: str | None = Field(
        default=None,
        min_length=1,
        description="HACM credential id; defaults to {platform}:{subject}",
    )
    config: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)


class UpdateAccountRequest(BaseModel):
    """Payload for PATCH /api/v1/providers/{name}/accounts/{account_id}.

    Fields omitted stay unchanged. `config` replaces the whole per-account
    config object (same semantics as the provider-level config patch).
    `scope_policy` replaces the whole per-account policy object
    ({rules, default}, RFC-011 §4.8 shape); an explicit null clears the
    override so the account inherits the provider-level policy again.
    PATCHing this field is operator-gated (JWT only), matching
    PUT /providers/{name}/scope-policy — scope labels are authority
    boundaries. platform/subject/credential_ref are immutable — remove and
    re-register instead (the credential they reference is operator-managed
    in HACM).
    """

    label: str | None = Field(default=None, max_length=120)
    status: str | None = Field(
        default=None,
        pattern=r"^(active|needs_reauth|revoked|disabled)$",
    )
    config: dict[str, Any] | None = None
    scope_policy: dict[str, Any] | None = None
    interval: int | None = Field(default=None, ge=60)
