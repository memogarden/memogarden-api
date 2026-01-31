"""Core API endpoints for MemoGarden.

This module provides blueprints for Core domain entities:
- Transactions (mutable financial records)
- Recurrences (recurring transaction rules)

Core entities are mutable and stored in the Core database.
"""

from . import recurrences, transactions

__all__ = ["transactions", "recurrences"]
