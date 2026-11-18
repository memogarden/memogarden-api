"""Operator Console HTML pages (RFC-015 §4).

Shell routes only: every page is a Tailwind shell that authenticates
client-side from the localStorage JWT and fetches JSON from the governed
APIs — exactly the established pattern of the login/api-keys/agents
pages (ui.py). No server-side session, no data access here; the console's
authority comes from the JWT the operator pastes into the login form,
and every button behind it is a JWT-only endpoint.

Screens (RFC-015 §4.1 — six, no dashboard by operator decision):
- /console/triage      Triage review (default)
- /console/scopes      Scopes & grants
- /console/identity    Keys & agents (overview; actions live on the
                       existing /api-keys and /agents/ pages)
- /console/providers   Providers, accounts, run history
- /console/occurrences Occurrence review
- /console/audit       Action audit
"""

from flask import Blueprint, redirect, render_template

console_bp = Blueprint("console", __name__)


@console_bp.route("/console", methods=["GET"])
def console_index():
    """Redirect to the console's default screen (Triage review)."""
    return redirect("/console/triage")


@console_bp.route("/console/triage", methods=["GET"])
def console_triage():
    """Triage review screen (RFC-015 §4.1)."""
    return render_template("console_triage.html")


@console_bp.route("/console/scopes", methods=["GET"])
def console_scopes():
    """Scopes & grants screen (RFC-015 §4.1)."""
    return render_template("console_scopes.html")


@console_bp.route("/console/identity", methods=["GET"])
def console_identity():
    """Keys & agents overview screen (RFC-015 §4.1)."""
    return render_template("console_identity.html")


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
