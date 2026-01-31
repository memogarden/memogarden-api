"""Custom exception classes for MemoGarden API."""


class MemoGardenError(Exception):
    """Base exception for all MemoGarden errors."""

    def __init__(self, message: str, details: dict | None = None):
        """Initialize exception with message and optional details."""
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ResourceNotFound(MemoGardenError):
    """Raised when a requested resource doesn't exist."""

    pass


class ValidationError(MemoGardenError):
    """Raised when request data fails validation."""

    pass


class DatabaseError(MemoGardenError):
    """Raised when a database operation fails."""

    pass


class AuthenticationError(MemoGardenError):
    """Raised when authentication fails (invalid credentials, token issues)."""

    pass
