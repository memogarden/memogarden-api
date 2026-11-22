"""Operator Console HTML pages (RFC-015 §4).

Shell routes only: every page is a Tailwind shell that authenticates
client-side from the localStorage JWT and fetches JSON from the governed
APIs. No server-side session, no data access here; the console's
authority comes from the JWT the operator pastes into the login form,
and every button behind it is a JWT-only endpoint.

Screens (RFC-015 §4.1 + consolidation addendum):
- /console/triage      Triage review (default)
- /console/scopes      Scopes & grants
- /console/identity    Keys & agents — full management since the
                       2026-11-19 consolidation absorbed the pre-RFC-010
                       /api-keys, /api-keys/new and /agents/ pages
                       (mint agents/keys, bind/unbind, revoke)
- /console/providers   Providers, accounts, run history
- /console/occurrences Occurrence review
- /console/audit       Action audit
- /console/settings    Operator profile + change-password (absorbed
                       the read-only /settings page; the change-password
                       endpoint finally has a UI)

Retired legacy page URLs redirect here (§4.1 addendum): /api-keys,
/api-keys/new and /agents/ → /console/identity; /settings →
/console/settings; / → /console.
"""

from flask import Blueprint, redirect, render_template

console_bp = Blueprint("console", __name__)


@console_bp.route("/console", methods=["GET"])
def console_index():
    """Redirect to the console's default screen (Triage review)."""
    return redirect("/console/triage")


# ---------------------------------------------------------------------------
# Consolidation redirects (2026-11-19): the legacy auth_views pages are
# retired; old links and bookmarks get a soft landing instead of a 404.
# ---------------------------------------------------------------------------


@console_bp.route("/api-keys", methods=["GET"])
@console_bp.route("/api-keys/new", methods=["GET"])
@console_bp.route("/agents/", methods=["GET"])
def redirect_legacy_identity_pages():
    """Legacy identity pages → the console screen that absorbed them."""
    return redirect("/console/identity")


@console_bp.route("/settings", methods=["GET"])
def redirect_legacy_settings_page():
    """Legacy settings page → the console settings screen."""
    return redirect("/console/settings")



@console_bp.route("/console/triage", methods=["GET"])
def console_triage():
    """Triage review screen (RFC-015 §4.1)."""
    return render_template("console_triage.html")


@console_bp.route("/console/scopes", methods=["GET"])
def console_scopes():
    """Scopes & grants screen (RFC-015 §4.1)."""
    return render_template("console_scopes.html")


@console_bp.route("/console/item", methods=["GET"])
def console_item():
    """Single-item inspector — unique per-item URL (2026-11-22).

    Shell only: ?uuid=soil_… is resolved client-side via the JWT-gated
    GET /api/v1/items/<uuid>. Facts; entities pending.
    """
    return render_template("console_item.html")


@console_bp.route("/console/identity", methods=["GET"])
def console_identity():
    """Keys & agents management screen (RFC-015 §4.1 + consolidation addendum)."""
    return render_template("console_identity.html")


@console_bp.route("/console/settings", methods=["GET"])
def console_settings():
    """Operator settings screen: profile + change-password (RFC-015 §3.1)."""
    return render_template("console_settings.html")


@console_bp.route("/console/providers", methods=["GET"])
def console_providers():
    """Providers / accounts / run-history screen (RFC-015 §4.1)."""
    return render_template("console_providers.html")


@console_bp.route("/console/occurrences", methods=["GET"])
def console_occurrences():
    """Occurrence review screen (RFC-015 §4.1)."""
    return render_template("console_occurrences.html")


@console_bp.route("/console/audit", methods=["GET"])
def console_audit():
    """Action audit screen (RFC-015 §4.1)."""
    return render_template("console_audit.html")
