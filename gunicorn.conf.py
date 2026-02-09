"""
Gunicorn configuration for MemoGarden API production server.

This configuration is optimized for Raspberry Pi (limited CPU/memory).
For more powerful servers, you may increase worker count.

Environment Variables:
    MEMOGARDEN_WORKERS    - Number of worker processes (default: 2)
    MEMOGARDEN_TIMEOUT    - Worker timeout in seconds (default: 30)
    MEMOGARDEN_BIND       - Bind address (default: 127.0.0.1:5000)
    MEMOGARDEN_LOG_LEVEL  - Log level (default: INFO)

See: https://docs.gunicorn.org/en/stable/settings.html
"""

import os
import multiprocessing

#=============================================================================
# Server Configuration
#=============================================================================

# Bind address (environment override)
# For production behind reverse proxy: 127.0.0.1:5000
# For direct access: 0.0.0.0:5000
bind = os.getenv("MEMOGARDEN_BIND", "127.0.0.1:5000")

# Worker timeout (seconds)
# MemoGarden operations can take time (cross-DB transactions, queries)
timeout = int(os.getenv("MEMOGARDEN_TIMEOUT", "30"))

# Keepalive timeout
keepalive = int(os.getenv("MEMOGARDEN_KEEPALIVE", "5"))

#=============================================================================
# Worker Configuration
#=============================================================================

# Worker type: sync is recommended for SQLite (WAL mode)
# For async operations, consider gevent or eventlet
worker_class = "sync"

# Number of workers (environment override)
# Raspberry Pi: 2-4 workers (limited CPU)
# Production server: (2 x CPU cores) + 1
workers = int(os.getenv("MEMOGARDEN_WORKERS", "2"))

# Maximum number of simultaneous requests per worker
# For sync workers, this is usually 1 (serialized requests)
# For gthread workers, this can be higher
worker_connections = int(os.getenv("MEMOGARDEN_WORKER_CONNECTIONS", "1000"))

# Maximum number of pending connections
backlog = int(os.getenv("MEMOGARDEN_BACKLOG", "2048"))

#=============================================================================
# Process Management
#=============================================================================

# Worker process name
proc_name = "memogarden"

# Daemon mode (false for systemd)
daemon = False

# PID file
pidfile = "/var/run/memogarden/memogarden.pid"

# User and group to run as
# Commented out - systemd service handles this
# user = "memogarden"
# group = "memogarden"

# Umask
umask = 0o007

#=============================================================================
# Logging
#=============================================================================

# Log level
loglevel = os.getenv("MEMOGARDEN_LOG_LEVEL", "INFO")

# Access log
accesslog = "/var/log/memogarden/access.log"

# Error log
errorlog = "/var/log/memogarden/error.log"

# Log format
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Capture stdout/stderr
capture_output = True

#=============================================================================
# SSL/TLS (if terminating SSL at Gunicorn)
#=============================================================================

# Uncomment to enable SSL (provide valid paths)
# keyfile = "/path/to/ssl/key.pem"
# certfile = "/path/to/ssl/cert.pem"
# ssl_version = 3  # TLSv1.2

#=============================================================================
# Server Hooks
#=============================================================================

def on_starting(server):
    """Called just before the master process is initialized."""
    import logging
    logging.info("MemoGarden server starting...")

def on_reload(server):
    """Called when the master process reloads."""
    import logging
    logging.info("MemoGarden server reloading...")

def when_ready(server):
    """Called just after the server is started."""
    import logging
    logging.info(f"MemoGarden server ready. Listening on: {bind}")

def pre_fork(server, worker):
    """Called just before a worker is forked."""
    import logging
    logging.info(f"Worker forking (pid: {worker.pid})")

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    import logging
    logging.info(f"Worker spawned (pid: {worker.pid})")

def pre_exec(server):
    """Called just before a new master process is forked."""
    import logging
    logging.info("Forked child, re-executing.")

def worker_int(worker):
    """Called just after a worker exited on SIGINT or SIGQUIT."""
    import logging
    logging.info(f"Worker received INT or QUIT signal (pid: {worker.pid})")

def worker_abort(worker):
    """Called when a worker receives the SIGABRT signal."""
    import logging
    logging.error(f"Worker received SIGABRT signal (pid: {worker.pid})")

def pre_request(worker, req):
    """Called just before a worker processes the request."""
    # You can add request preprocessing here
    pass

def post_request(worker, req, environ, resp):
    """Called after a worker processes the request."""
    # You can add request postprocessing here
    pass

def child_exit(server, worker):
    """Called just after a worker has been exited."""
    import logging
    logging.info(f"Worker exited (pid: {worker.pid})")

def worker_exit(server, worker):
    """Called just after a worker has been exited."""
    import logging
    logging.info(f"Worker exited (pid: {worker.pid})")

def nworkers_changed(server, new_value, old_value):
    """Called just after num_workers changed."""
    import logging
    logging.info(f"Worker count changed: {old_value} -> {new_value}")

#=============================================================================
# Graceful Shutdown
#=============================================================================

# Graceful timeout (seconds)
# Workers have this much time to finish handling existing requests
# before they are killed.
graceful_timeout = int(os.getenv("MEMOGARDEN_GRACEFUL_TIMEOUT", "10"))

#=============================================================================
# Security
#=============================================================================

# Limit request line size
limit_request_line = int(os.getenv("MEMOGARDEN_LIMIT_REQUEST_LINE", "4094"))

# Limit request fields
limit_request_fields = int(os.getenv("MEMOGARDEN_LIMIT_REQUEST_FIELDS", "100"))

# Limit request field size
limit_request_field_size = int(os.getenv("MEMOGARDEN_LIMIT_REQUEST_FIELD_SIZE", "8190"))

#=============================================================================
# Advanced Configuration
#=============================================================================

# Enable statsD metrics (optional)
# statsd_host = "localhost:8125"

# Enable Prometheus metrics (optional)
# statsd_prefix = "memogarden"

# Reload engine (auto-reload on file changes - development only!)
reload = False

# Sendfile (use OS sendfile for static files)
sendfile = True

# TCP nodelay
tcp_nodelay = True
