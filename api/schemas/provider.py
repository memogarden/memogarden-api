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
