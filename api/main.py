"""Flask application entry point."""

import logging
import os

from flask import Flask, jsonify
from flask_cors import CORS

from system import (
    SystemStatus,
    TransactionCoordinator,
    init_system,
)
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
    try:
        # Initialize both databases via System public API
        system_info = init_system()

        if not system_info['databases_existed']:
            logger.info("New databases created successfully")
            logger.info(f"Soil database: {system_info['soil_db_path']}")
            logger.info(f"Core database: {system_info['core_db_path']}")
        else:
            logger.info("Existing databases loaded")

        logger.info("Core database initialized")
        logger.info("Soil database initialized")

        # Log consistency check results
        system_status = system_info['status']
        if system_status.value == "normal":
            logger.info("Consistency checks passed")
        else:
            logger.warning(f"System status: {system_status.value}")
            logger.warning("   Consistency issues detected. Check /status endpoint for details.")

        # Check if admin user exists
        if not system_info['has_admin_user']:
            logger.warning(
                "No admin user exists. Visit http://localhost:5000/admin/register to setup"
            )

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
        # Get database paths from system info
        system_info = init_system()
        soil_db = system_info['soil_db_path']
        core_db = system_info['core_db_path']

        # Check if database files exist
        soil_exists = os.path.exists(soil_db)
        core_exists = os.path.exists(core_db)

        result = {
            "status": "ok",
            "databases": {
                "soil": "connected" if soil_exists else "missing",
                "core": "connected" if core_exists else "missing",
                "paths": {
                    "soil": soil_db,
                    "core": core_db,
                }
            }
        }

        # Get system status from consistency check
        system_status = system_info['status']
        result["consistency"] = {
            "status": system_status.value,
        }

        # Update overall status based on consistency check
        if system_status.value != "normal":
            result["status"] = system_status.value
            result["warning"] = "Consistency issues detected"

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
