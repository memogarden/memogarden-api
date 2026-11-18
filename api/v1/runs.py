"""Provider run reads (RFC-015 §3.2).

Run history was previously internal — `provider_runs` had no read
endpoint, so neither the operator console nor the CLI could show what a
run actually did (created counts, errors, duration). These reads are the
verification side of delegated run triggers: triggering a run is
delegable (RFC-015 §2.2), reading what happened must be trivial.

Read-only and non-governed: matches provider GET's principal class (any
authenticated principal). The operator-governed boundary lives on
configuration (scope-policy, registration), not on operational reads.
"""

from flask import Blueprint, jsonify

from .. import provider_registry

runs_bp = Blueprint("runs", __name__, url_prefix="/runs")


@runs_bp.get("/<run_id>")
def get_run(run_id: str):
    """Get a single provider run by id (RFC-015 §3.2).

    Returns:
        200: Run record (status, started/finished, triggered_by, detail)
        404: Unknown run id
    """
    return jsonify(provider_registry.get_run(run_id))
