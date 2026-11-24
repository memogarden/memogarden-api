"""Client for the triage-model embed daemon (api#95 semantic plane).

Single place owning the unix-socket protocol + the lazy Popen spawn
fallback (architecture decision 2026-11-22: query-time inference never
lives in the serving process — the daemon is a model-layer child with
idle self-exit; systemd socket activation is the Phase 2 successor of
the spawn fallback, not yet installed).

The daemon is scope-masked with capture-time scope sets — that mask is
DEFENSE-IN-DEPTH ONLY. RFC-011 §4.9(3) enforcement for semantic hits
happens at THIS boundary with current-time visibility (see the
semantic/hybrid search paths in handlers/core.py): over-fetch from the
daemon, drop anything not visible under the requester's allowed_scopes,
then truncate.
"""

import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _triage_model_dir() -> Path:
    return Path(os.environ.get(
        "MEMOGARDEN_TRIAGE_MODEL_DIR",
        str(Path.home() / "memogarden/triage-model")))


def _embed_sock() -> Path:
    return Path(os.environ.get(
        "MEMOGARDEN_EMBED_SOCK",
        str(Path.home() / ".local/share/memogarden/triage-model/embed.sock")))


def search(q: str, scopes=None, types=None, k: int = 50):
    """Query the daemon; spawn it lazily if the socket isn't listening.

    Returns [[uuid, score], ...] best-first, or None when the daemon is
    unavailable (caller degrades to the lexical/keyword plane).
    """
    payload = json.dumps({"op": "search", "q": q, "scopes": scopes,
                          "types": types, "k": k}).encode()
    spawned = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect(str(_embed_sock()))
            s.sendall(payload)
            buf = b""
            while True:
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            s.close()
            out = json.loads(buf)
            return out.get("results") if "results" in out else None
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout,
                json.JSONDecodeError):
            if not spawned:
                log = _triage_model_dir() / "embed-daemon.log"
                # Popen dups the fds onto the child — close the parent's
                # copies immediately or the handle leaks per spawn (c0fd2d2)
                try:
                    with open(log, "ab") as f:
                        subprocess.Popen(
                            [str(_triage_model_dir() / ".venv/bin/python"),
                             str(_triage_model_dir() / "embed_daemon.py")],
                            cwd=str(_triage_model_dir()), stdout=f, stderr=f,
                            start_new_session=True)
                except OSError:
                    # No model layer on this host (tests, stripped deploys):
                    # the daemon can never come up — stop retrying now
                    # instead of burning the 15s deadline per query.
                    logger.warning("embed daemon spawn failed; semantic "
                                   "plane unavailable")
                    return None
                spawned = True
            time.sleep(0.5)
    return None
