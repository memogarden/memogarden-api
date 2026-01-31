"""HTML UI pages for MemoGarden Core.

These endpoints provide HTML user interface pages for:
- Admin setup (localhost only)
- User login
- API key management
- User settings

All pages use TailwindCSS for styling and vanilla JavaScript for interactions.
"""

import logging

from flask import Blueprint, jsonify, render_template, render_template_string

from ..config import settings
from . import service
from system.core import get_core

logger = logging.getLogger(__name__)


# Create blueprint
auth_views_bp = Blueprint("auth_views", __name__, template_folder='../templates')


# ============================================================================
# Admin Registration Page (localhost only, one-time)
# ============================================================================

ADMIN_REGISTER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>MemoGarden - Admin Setup</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center px-4">
    <div class="bg-white shadow-md rounded-lg p-8 max-w-md w-full">
        <h1 class="text-3xl font-bold text-gray-800 mb-2 text-center">MemoGarden Admin Setup</h1>
        <p class="text-gray-600 text-center mb-6">Create your admin account to get started</p>

        {% if message %}
        <div class="bg-blue-100 text-blue-700 border border-blue-400 rounded-lg p-4 mb-4">
            {{ message }}
        </div>
        {% endif %}

        {% if error %}
        <div class="bg-red-100 text-red-700 border border-red-400 rounded-lg p-4 mb-4">
            {{ error }}
        </div>
        {% endif %}

        <form id="adminRegisterForm" class="space-y-4">
            <div>
                <label for="username" class="block text-sm font-medium text-gray-700 mb-1">Username</label>
                <input
                    type="text"
                    id="username"
                    name="username"
                    required
                    pattern="[A-Za-z0-9_-]+"
                    title="Letters, numbers, underscores, and hyphens only"
                    autocomplete="username"
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    placeholder="admin"
                />
                <p class="text-xs text-gray-500 mt-1">Letters, numbers, underscores, and hyphens only</p>
            </div>

            <div>
                <label for="password" class="block text-sm font-medium text-gray-700 mb-1">Password</label>
                <input
                    type="password"
                    id="password"
                    name="password"
                    required
                    minlength="8"
                    autocomplete="new-password"
                    class="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none"
                    placeholder="••••••••"
                />
                <p class="text-xs text-gray-500 mt-1">Minimum 8 characters, at least one letter and one digit</p>
            </div>

            <button
                type="submit"
                class="w-full bg-blue-600 text-white py-2 px-4 rounded-lg hover:bg-blue-700 transition font-medium"
            >
                Create Admin Account
            </button>
        </form>

    </div>
</body>
</html>
<script>
    // Handle admin registration form submission
    document.getElementById('adminRegisterForm').addEventListener('submit', async function(e) {
        e.preventDefault();

        const formData = new FormData(this);
        const data = {
            username: formData.get('username'),
            password: formData.get('password')
        };

        try {
            const response = await fetch('/admin/register', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            });

            const result = await response.json();

            if (response.ok) {
                // Registration successful - redirect to login
                window.location.href = '/login?registered=true';
            } else {
                // Show error(s) - display field-specific validation errors
                const errorDiv = document.createElement('div');
                errorDiv.className = 'bg-red-100 text-red-700 border border-red-400 rounded-lg p-4 mb-4';

                // Check if we have field-specific validation errors
                if (result.error?.details?.errors && Array.isArray(result.error.details.errors)) {
                    // Build error message with field-specific details
                    let errorHtml = '<strong>Registration failed:</strong><ul class="list-disc list-inside mt-2">';
                    result.error.details.errors.forEach(err => {
                        errorHtml += `<li><strong>${err.field}:</strong> ${err.message}</li>`;
                    });
                    errorHtml += '</ul>';
                    errorDiv.innerHTML = errorHtml;
                } else {
                    // Fallback to generic error message
                    errorDiv.textContent = result.error?.message || 'Registration failed';
                }

                this.insertBefore(errorDiv, this.firstChild);

                // Remove error after 8 seconds (longer for field errors)
                setTimeout(() => errorDiv.remove(), 8000);
            }
        } catch (error) {
            console.error('Registration error:', error);
            const errorDiv = document.createElement('div');
            errorDiv.className = 'bg-red-100 text-red-700 border border-red-400 rounded-lg p-4 mb-4';
            errorDiv.textContent = 'Network error. Please try again.';
            this.insertBefore(errorDiv, this.firstChild);
        }
    });
</script>
"""


def _is_localhost_request() -> bool:
    """
    Check if the request is from localhost.

    Returns True if the remote address is localhost (127.0.0.1, ::1, or 'localhost').
    Can be bypassed via config.bypass_localhost_check for testing.
    """
    from flask import request

    if settings.bypass_localhost_check:
        return False

    remote_addr = request.remote_addr or ""
    return remote_addr in {"127.0.0.1", "::1", "localhost"}


@auth_views_bp.route("/admin/register", methods=["GET"])
def admin_register_page():
    """
    Display admin registration page (localhost only).

    This page is only accessible:
    1. From localhost (127.0.0.1, ::1)
    2. When no users exist in the database

    Returns:
        HTML page with admin registration form

    Error Responses:
        403 JSON: If not localhost
        200 HTML: If admin already exists (shows error in HTML)
    """
    from flask import request

    # Check localhost access - return JSON error for API compatibility
    if not _is_localhost_request():
        logger.warning(f"Admin registration attempt from non-localhost: {request.remote_addr}")
        return jsonify({
            "error": {
                "type": "Forbidden",
                "message": "Admin registration is only accessible from localhost"
            }
        }), 403

    # Check if any users exist
    core = get_core()
    try:
        if service.has_admin_user(core._conn):
            logger.info("Admin already exists, redirecting to login")
            from flask import redirect
            return redirect('/login?existing=true')

        return render_template_string(ADMIN_REGISTER_HTML, error=None, message=None)
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return render_template_string(
            "<!DOCTYPE html><html><head><title>Error</title></head><body>"
            "<h1 class='text-red-600 text-center mt-8'>500 - Internal Server Error</h1>"
            "<p class='text-center text-gray-600'>Failed to check admin status</p>"
            "</body></html>"
        ), 500
    # Connection closes automatically via __del__


# ============================================================================
# Login Page
# ============================================================================


@auth_views_bp.route("/login", methods=["GET"])
def login_page():
    """
    Display login page.

    Returns:
        HTML login page with form

    Example response:
        HTML page with login form
    """
    return render_template("login.html")


# ============================================================================
# API Key Management Pages
# ============================================================================


@auth_views_bp.route("/api-keys", methods=["GET"])
def api_keys_page():
    """
    Display API keys management page.

    Returns:
        HTML page for managing API keys

    Example response:
        HTML page with API keys list and management
    """
    return render_template("api_keys.html")


@auth_views_bp.route("/api-keys/new", methods=["GET"])
def api_key_new_page():
    """
    Display create new API key page.

    Returns:
        HTML page for creating new API key

    Example response:
        HTML page with API key creation form
    """
    return render_template("api_key_new.html")


# ============================================================================
# Settings Page
# ============================================================================


@auth_views_bp.route("/settings", methods=["GET"])
def settings_page():
    """
    Display user settings page.

    Returns:
        HTML page with user profile and settings

    Example response:
        HTML page with user settings
    """
    return render_template("settings.html")


# ============================================================================
# Index Page
# ============================================================================


@auth_views_bp.route("/", methods=["GET"])
def index_page():
    """
    Redirect to appropriate page based on auth status.

    If user is authenticated (has valid token in localStorage), redirect to /api-keys.
    Otherwise, redirect to /login.

    Note: This is a simple server-side redirect. The actual auth check happens
    client-side in JavaScript since tokens are stored in localStorage.

    Returns:
        Redirect to /api-keys or /login

    Example response:
        HTTP 302 redirect
    """
    # For now, redirect to login since we can't check localStorage server-side
    # The pages themselves handle the auth check client-side
    return render_template("login.html")
