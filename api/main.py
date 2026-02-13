"""Flask application entry point."""

import logging

from flask import Flask, jsonify
from flask_cors import CORS

from system.core import _create_connection, init_db
from system.exceptions import (
    AuthenticationError,
    MemoGardenError,
    ResourceNotFound,
    ValidationError,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Database initialization (runs once on app startup)
def initialize_database():
    """Initialize database on app startup.

    This function performs greenfield database initialization:
    1. Creates databases if they don't exist (RFC-004 path resolution)
    2. Initializes schemas (applies migrations)
    3. Runs consistency checks (RFC-008)
    4. Checks for admin user existence

    The system is always-available - startup succeeds even if databases
    are missing (they will be created automatically).

    Raises:
        Exception: If database initialization fails (prevents app startup)
    """
    from system.host.environment import get_db_path
    from system.soil.database import Soil
    from system.transaction_coordinator import TransactionCoordinator
    import os

    try:
        # Get database paths (RFC-004)
        soil_db_path = get_db_path('soil')
        core_db_path = get_db_path('core')

        # Check if databases exist
        soil_exists = os.path.exists(soil_db_path)
        core_exists = os.path.exists(core_db_path)

        if not soil_exists or not core_exists:
            logger.info("Databases not found. Creating new databases...")
            logger.info(f"Soil database: {soil_db_path}")
            logger.info(f"Core database: {core_db_path}")

            # Create parent directories if needed
            soil_dir = os.path.dirname(soil_db_path)
            core_dir = os.path.dirname(core_db_path)
            if soil_dir:
                os.makedirs(soil_dir, exist_ok=True)
            if core_dir:
                os.makedirs(core_dir, exist_ok=True)

        # Initialize Core database (creates if missing, applies migrations)
        init_db()
        logger.info("Core database initialized")

        # Initialize Soil database (creates if missing, applies migrations)
        from system.soil.database import get_soil
        with get_soil(str(soil_db_path)) as soil:
            soil.init_schema()
        logger.info("Soil database initialized")

        if not soil_exists or not core_exists:
            logger.info("New databases created successfully")
        else:
            logger.info("Existing databases loaded")

        # Run consistency checks (RFC-008)
        logger.info("Running consistency checks...")
        coordinator = TransactionCoordinator(
            soil_db_path=soil_db_path,
            core_db_path=core_db_path
        )
        system_status = coordinator.check_consistency()

        if system_status.value == "normal":
            logger.info("Consistency checks passed")
        else:
            logger.warning(f"System status: {system_status.value}")
            logger.warning("   Consistency issues detected. Check /status endpoint for details.")

        # Check if admin user exists
        # NOTE: Using _create_connection() temporarily until Core has public API for this
        # TODO: Add core.user.has_admin() public method to Core
        from .middleware import service

        conn = _create_connection()
        try:
            if not service.has_admin_user(conn):
                logger.warning(
                    "No admin user exists. Visit http://localhost:5000/admin/register to setup"
                )
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def create_app(test_config=None):
    """Create and configure Flask app.

    Args:
        test_config: Optional test configuration dict

    Returns:
        Configured Flask application
    """
    from .config import settings

    app = Flask(__name__)

    # Load config
    if test_config:
        app.config.update(test_config)
    else:
        app.config["JWT_SECRET_KEY"] = settings.jwt_secret_key
        app.config["DATABASE_PATH"] = settings.database_path

    # CORS configuration
    CORS(app, origins=settings.cors_origins, supports_credentials=True)

    # Initialize database with app context
    # Skip database initialization in test mode (tests handle their own schema setup)
    if not app.config.get("TESTING"):
        with app.app_context():
            initialize_database()

    # Register error handlers
    _register_error_handlers(app)

    # Register routes
    _register_routes(app)

    # Register blueprints
    _register_blueprints(app)

    return app


def _register_error_handlers(app):
    """Register error handlers for the app."""

    @app.errorhandler(ResourceNotFound)
    def handle_not_found(error):
        """Handle ResourceNotFound exceptions."""
        response = {
            "error": {
                "type": "ResourceNotFound",
                "message": error.message
            }
        }
        if error.details:
            response["error"]["details"] = error.details
        return jsonify(response), 404

    @app.errorhandler(ValidationError)
    def handle_validation_error(error):
        """Handle ValidationError exceptions."""
        response = {
            "error": {
                "type": "ValidationError",
                "message": error.message
            }
        }
        if error.details:
            response["error"]["details"] = error.details
        return jsonify(response), 400

    @app.errorhandler(AuthenticationError)
    def handle_authentication_error(error):
        """Handle AuthenticationError exceptions."""
        response = {
            "error": {
                "type": "AuthenticationError",
                "message": error.message
            }
        }
        if error.details:
            response["error"]["details"] = error.details
        return jsonify(response), 401

    @app.errorhandler(MemoGardenError)
    def handle_memo_garden_error(error):
        """Handle generic MemoGardenError exceptions."""
        response = {
            "error": {
                "type": error.__class__.__name__,
                "message": error.message
            }
        }
        if error.details:
            response["error"]["details"] = error.details
        return jsonify(response), 500

    @app.errorhandler(500)
    def handle_internal_error(error):
        """Handle internal server errors."""
        logger.error(f"Internal error: {error}")
        return jsonify({
            "error": {
                "type": "InternalServerError",
                "message": "An internal error occurred"
            }
        }), 500


def _register_routes(app):
    """Register routes for the app."""

    @app.route("/health")
    def health():
        """Simple health check endpoint.

        Returns 200 if the server is running. For detailed status, use /status.
        """
        return jsonify({"status": "ok"})

    @app.route("/status")
    def status():
        """System status endpoint with database consistency checks.

        Returns:
            - status: System status (normal, inconsistent, read_only, safe_mode)
            - databases: Database connection status
            - consistency: Consistency check results (if issues found)

        See: RFC-008 v1.2 Transaction Semantics
        """
        from system.transaction_coordinator import TransactionCoordinator
        from system.host.environment import get_db_path
        import os

        # Get database paths (RFC-004)
        soil_db = str(get_db_path('soil'))
        core_db = str(get_db_path('core'))

        # Check if database files exist
        soil_exists = os.path.exists(soil_db)
        core_exists = os.path.exists(core_db)

        result = {
            "status": "ok",
            "databases": {
                "soil": "connected" if soil_exists else "missing",
                "core": "connected" if core_exists else "missing",
                "paths": {
                    "soil": str(soil_db),
                    "core": str(core_db),
                }
            }
        }

        # Run consistency checks if databases exist
        if soil_exists and core_exists:
            try:
                coordinator = TransactionCoordinator(
                    soil_db_path=soil_db,
                    core_db_path=core_db
                )
                system_status = coordinator.check_consistency()
                result["consistency"] = {
                    "status": system_status.value,
                }

                # Update overall status based on consistency check
                if system_status.value != "normal":
                    result["status"] = system_status.value
                    result["warning"] = "Consistency issues detected"

            except Exception as e:
                logger.error(f"Consistency check failed: {e}")
                result["consistency"] = {
                    "status": "error",
                    "error": str(e)
                }
                result["status"] = "error"

        return jsonify(result)


def _register_blueprints(app):
    """Register blueprints for the app."""
    from . import semantic
    from . import events
    from .middleware import api as auth_api
    from .middleware import ui as auth_ui
    from .v1 import api_v1_bp

    # Register API blueprints
    app.register_blueprint(api_v1_bp)

    # Auth API endpoints (JSON responses, top-level routes)
    app.register_blueprint(auth_api.auth_bp)

    # Auth UI pages (HTML responses, top-level routes)
    app.register_blueprint(auth_ui.auth_views_bp)

    # Semantic API (/mg endpoint)
    app.register_blueprint(semantic.semantic_bp)

    # Server-Sent Events (/mg/events endpoint)
    # Session 20A: SSE Infrastructure
    app.register_blueprint(events.events_bp)


# Legacy global app for backward compatibility (will be removed)
# This allows old `from api.main import app` pattern to work during transition
# DEPRECATED: Use create_app() instead
_app = None


def __getattr__(name):
    """Provide legacy global app for backward compatibility."""
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
