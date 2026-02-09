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
│   ├── handlers/         # Semantic API verb handlers
│   ├── middleware/       # Authentication and authorization
│   ├── main.py           # Flask app factory
│   ├── semantic.py       # Semantic API (/mg endpoint)
│   └── config.py         # Configuration (extends system.config.Settings)
├── tests/                # API tests
├── gunicorn.conf.py      # Production server configuration
└── .env.example          # Environment variable template
```

## Dependencies

- `memogarden-system` - Core and Soil layers
- `flask` - Web framework
- `pydantic` - Request/response validation
- `pyjwt` - JWT tokens
- `bcrypt` - Password hashing
- `gunicorn` - Production WSGI server (optional)

## Development

Install dependencies:

```bash
cd memogarden-api
poetry install
```

Run development server:

```bash
# From repository root
./scripts/run.sh

# Or from memogarden-api directory
poetry run flask --app api.main run --debug
```

Run tests:

```bash
poetry run pytest
```

## Production Deployment

### Supported Distributions

The installation script supports:
- **Debian-based**: Raspberry Pi OS, Ubuntu, Debian
- **Arch-based**: Arch Linux ARM, Manjaro ARM

### Quick Deploy (Raspberry Pi)

Run the deployment script on your Raspberry Pi:

```bash
curl -sSL https://raw.githubusercontent.com/memogarden/memogarden/refs/heads/main/scripts/deploy-memogarden.sh | sudo bash
```

Or manually:

```bash
# Clone repository
git clone https://github.com/memogarden/memogarden.git /opt/memogarden

# Run installer
cd /opt/memogarden
sudo ./install.sh

# Start service
sudo systemctl start memogarden
sudo systemctl status memogarden
```

### Environment Configuration

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
nano .env
```

Required variables:
- `MEMOGARDEN_DATA_DIR` - Data directory (default: `/var/lib/memogarden`)
- `JWT_SECRET_KEY` - Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

Optional variables:
- `MEMOGARDEN_SOIL_DB` - Soil database path (overrides data dir)
- `MEMOGARDEN_CORE_DB` - Core database path (overrides data dir)

See `.env.example` for all configuration options.

### Production Server

The service uses gunicorn with configuration from `gunicorn.conf.py`:
- Workers: 2 (configurable via `MEMOGARDEN_WORKERS`)
- Timeout: 30s (configurable via `MEMOGARDEN_TIMEOUT`)
- Bind: 127.0.0.1:5000 (configurable via `MEMOGARDEN_BIND`)

### Health Checks

Check service status:

```bash
# Simple health check
curl http://localhost:5000/health

# Detailed status (includes consistency checks)
curl http://localhost:5000/status
```

### Logs

View application logs:

```bash
# systemd logs
sudo journalctl -u memogarden -f

# Application logs
sudo tail -f /var/log/memogarden/memogarden.log
```

## API Endpoints

### REST API (`/api/v1/`)

- `POST /api/v1/transactions` - Create transaction
- `GET /api/v1/transactions` - List transactions
- `GET /api/v1/transactions/{uuid}` - Get transaction
- `PATCH /api/v1/transactions/{uuid}` - Edit transaction
- `DELETE /api/v1/transactions/{uuid}` - Delete transaction

- `POST /api/v1/recurrences` - Create recurrence
- `GET /api/v1/recurrences` - List recurrences
- `GET /api/v1/recurrences/{uuid}` - Get recurrence
- `PATCH /api/v1/recurrences/{uuid}` - Edit recurrence
- `DELETE /api/v1/recurrences/{uuid}` - Delete recurrence

### Semantic API (`/mg`)

Verb-based API for flexible operations:
- `create`, `get`, `edit`, `forget`, `query` - Entity operations
- `add`, `amend`, `get`, `query` - Fact operations (Soil)
- `link`, `unlink`, `edit_relation`, `get_relation`, `query_relation` - Relation operations
- `enter_scope`, `leave_scope`, `focus_scope` - Context operations
- `search` - Semantic search
- `track` - Causal chain tracing

See `plan/rfc-005-api-design.md` for API specification.

### Authentication

Admin registration (localhost only):
- `GET /admin/register` - Registration page
- `POST /admin/register` - Create admin user

API key management:
- `GET /api-keys` - List API keys
- `POST /api-keys/new` - Create new API key
- `DELETE /api-keys/{id}` - Revoke API key

## Troubleshooting

### Service won't start

```bash
# Check status
sudo systemctl status memogarden

# Check logs
sudo journalctl -u memogarden -n 50

# Verify configuration
cat /opt/memogarden/memogarden-api/.env
```

### Database issues

```bash
# Check database files
ls -lh /var/lib/memogarden/

# Run consistency check
curl http://localhost:5000/status

# Reinitialize databases (WARNING: deletes data!)
rm /var/lib/memogarden/*.db
sudo systemctl restart memogarden
```

### Permission errors

```bash
# Fix ownership
sudo chown -R memogarden:memogarden /var/lib/memogarden
sudo chown -R memogarden:memogarden /var/log/memogarden
```

## Development Setup

For local development without systemd:

```bash
# Set up environment
cp .env.example .env
# Edit .env as needed

# Initialize databases
./scripts/init-db.sh

# Run development server
./scripts/run.sh
```

## Testing

Run all tests:

```bash
poetry run pytest
```

Run specific test file:

```bash
poetry run pytest tests/test_health_status.py -xvs
```

Deployment integration tests:

```bash
# Auto-detecting test (runs appropriate test for your environment)
# - In container/Docker: Runs local validation
# - On host with Docker: Runs full Docker test
# - On host without Docker: Runs local validation
bash tests/docker/test-deployment.sh

# Force local mode (skip Docker, run validation only)
bash tests/docker/test-deployment.sh --local

# Force Docker mode (requires Docker, runs full test in container)
bash tests/docker/test-deployment.sh --docker

# Show help
bash tests/docker/test-deployment.sh --help
```

**Note:** The deployment test automatically detects your environment and runs appropriate tests. In CI/CD or containers, it runs fast validation tests. On host machines with Docker, it runs full integration tests.
