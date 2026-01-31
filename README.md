# MemoGarden API

HTTP API layer for MemoGarden.

## Overview

This package contains the Flask web application that provides HTTP endpoints for MemoGarden. It depends on `memogarden-system` for Core and Soil layer operations.

## Structure

```
api/
├── api/
│   ├── v1/               # API v1 endpoints
│   │   ├── core/         # Core entity endpoints
│   │   ├── soil/         # Soil item endpoints
│   │   └── schemas/      # Pydantic request/response schemas
│   ├── middleware/       # Authentication and authorization
│   ├── main.py           # Flask app factory
│   └── config.py         # Configuration (extends system.config.Settings)
└── tests/                # API tests
```

## Dependencies

- `memogarden-system` - Core and Soil layers
- `flask` - Web framework
- `pydantic` - Request/response validation
- `pyjwt` - JWT tokens
- `bcrypt` - Password hashing

## Development

Install dependencies:

```bash
cd api
poetry install
```

Run development server:

```bash
poetry run python -m api.main
```

Run tests:

```bash
poetry run pytest
```
