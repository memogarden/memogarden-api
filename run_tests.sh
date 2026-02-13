#!/bin/bash
# MemoGarden API Test Runner
#
# Standardized test entrypoint for memogarden-api.
# Usage: ./run_tests.sh [options] [pytest_args...]
#
# Options:
#   -h, --help              Show this help message
#   --format=FORMAT         Output format: textbox (default), plaintext, markdown
#
# Examples:
#   ./run_tests.sh                        # Run all tests with default textbox format
#   ./run_tests.sh --format=markdown      # Markdown output (for agents/logs)
#   ./run_tests.sh --format=plaintext     # Plain text output
#   ./run_tests.sh -xvs                   # Verbose, stop on first failure
#   ./run_tests.sh tests/test_sse.py::test_sse_connection
#   ./run_tests.sh --cov=api --cov-report=term-missing

# ============================================================================
# PROJECT CONFIGURATION
# ============================================================================
# These variables configure the centralized test entrypoint.
# Only change these values - all other logic is in scripts/test_entrypoint.sh
#
# WARNING TO AGENTS: If you need functionality not supported by the test
# entrypoint, DO NOT work around it with ad-hoc bash commands.
# Instead, alert a human that scripts/test_entrypoint.sh needs improvement.
# ============================================================================

# Project name (for display)
export PROJECT_NAME="memogarden-api"

# Python module name for coverage (e.g., "api", "system", "mg_client")
export MODULE_NAME="api"

# Dependency check: Python import to verify (empty = no check)
# Example: "from system.utils import isodatetime"
export DEPENDENCY_CHECK="from system.utils import isodatetime"

# Optional: Additional environment variables for tests
# export MY_VAR="value"

# ============================================================================
# PARSE SCRIPT OPTIONS
# ============================================================================

# Parse --format before sourcing entrypoint (so entrypoint can use it)
PYTEST_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --format=*)
            export TEST_FORMAT="${1#*=}"
            shift
            ;;
        *)
            PYTEST_ARGS+=("$1")
            shift
            ;;
    esac
done

# ============================================================================
# INVOKE CENTRALIZED ENTRYPOINT
# ============================================================================

# Change to this script's directory before calling entrypoint
# This ensures poetry finds the correct pyproject.toml
cd "$(dirname "${BASH_SOURCE[0]}")"

# Call the centralized test entrypoint with all arguments
exec /workspaces/memogarden/scripts/test_entrypoint.sh "${PYTEST_ARGS[@]}"
