"""Restricted API-key capabilities (RFC-013 ingestion register E3).

A key minted with a non-empty capabilities list is a RESTRICTED key: the
auth middleware allows it only on endpoints covered by those
capabilities and 403s everything else. An empty list is unrestricted —
the legacy all-or-nothing behavior every existing key keeps. Issuance
stays operator-gated JWT (RFC-010 invariant); enforcement is read-only
over the key row (auth hot-path invariant).
"""

KNOWN_CAPABILITIES = frozenset({"provider_run"})

# Endpoint names (flask endpoint = "<blueprint>.<function>") each
# capability may call. A restricted key may call the union of its
# capabilities' route sets; everything else is denied.
CAPABILITY_ROUTES = {
    "provider_run": frozenset({
        "api_v1.providers.run_due_providers",   # POST /providers/run (heartbeat poke)
        "api_v1.providers.run_provider",        # POST /providers/<name>/run
        "api_v1.providers.run_account",         # POST /providers/<name>/accounts/<id>/run
    }),
}

CAPABILITY_DESCRIPTIONS = {
    "provider_run": (
        "May request provider runs (POST /providers/.../run). Cannot read "
        "or write facts, entities, keys, or provider config."
    ),
}


def validate_capabilities(caps: list[str] | None) -> list[str]:
    """Validate a capabilities list at mint/update time."""
    caps = list(caps or [])
    unknown = set(caps) - KNOWN_CAPABILITIES
    if unknown:
        raise ValueError(
            "unknown capabilities: %s (known: %s)"
            % (", ".join(sorted(unknown)), ", ".join(sorted(KNOWN_CAPABILITIES)))
        )
    return caps
