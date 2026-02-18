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
# After refactor: utils moved to shared package
export DEPENDENCY_CHECK="from system import get_core"

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

# Get this script's directory before changing directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# IMPORTANT: Clear any stale VIRTUAL_ENV set by VSCode's Python extension
# The extension may cache interpreter paths that point to non-existent directories
# See: docs/history/test-runner-failure-investigation-2026-02-14.md
unset VIRTUAL_ENV

# Then set VIRTUAL_ENV to the correct project-local .venv path
# This ensures Poetry uses the right virtualenv for this project
# Use SCRIPT_DIR so this works inside and outside devcontainer
export VIRTUAL_ENV="$SCRIPT_DIR/.venv"

# Find MemoGarden root: go up two levels from subproject directory
MG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Validate we're in the right place (should end with "memogarden")
if [[ ! "$MG_ROOT" =~ memogarden$ ]]; then
    echo "ERROR: Unexpected project root: $MG_ROOT" >&2
    echo "Path should end with 'memogarden'" >&2
    exit 1
fi

# Change to subproject directory before calling entrypoint
# This ensures poetry finds the correct pyproject.toml
cd "$SCRIPT_DIR"

# Call the centralized test entrypoint with all arguments
exec "$MG_ROOT/scripts/test_entrypoint.sh" "${PYTEST_ARGS[@]}"
