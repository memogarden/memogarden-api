"""Client for the memogarden-search embed daemon (api#95 semantic plane).

Single place owning the unix-socket protocol + the lazy Popen spawn
fallback (architecture decision 2026-11-22: query-time inference never
lives in the serving process — the daemon is a model-layer child with
idle self-exit; systemd socket activation lives in the memogarden-search
repo's deploy/ units and takes over when installed — this client then
just connects, no spawn).

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


def _model_dir() -> Path:
    """memogarden-search checkout (daemon + venv home since the slice-B
    cutover; MEMOGARDEN_TRIAGE_MODEL_DIR kept as a legacy override)."""
    return Path(os.environ.get(
        "MEMOGARDEN_SEARCH_MODEL_DIR",
        os.environ.get(
            "MEMOGARDEN_TRIAGE_MODEL_DIR",
            str(Path.home() / "memogarden/memogarden-search"))))


def _embed_sock() -> Path:
    return Path(os.environ.get(
        "MEMOGARDEN_EMBED_SOCK",
        str(Path.home() / ".local/share/memogarden/triage-model/embed.sock")))


def _cache_dir() -> Path:
    """Where memogarden-search keeps the index artifacts: MEMOGARDEN_SEARCH_CACHE,
    else <search repo>/cache — the same resolution the daemon and the index
    builder use, so the API reads the state the daemon writes as long as the
    service and the daemon share these variables (production sets neither)."""
    return Path(os.environ.get(
        "MEMOGARDEN_SEARCH_CACHE", str(_model_dir() / "cache")))


# Fact types the semantic index embeds — mirrors TYPES in
# memogarden-search/build_search_index.py. Only changes to these are ever
# reflected by the index's catch-up, so only they count toward its lag.
INDEXED_TYPES = ("Email", "Attachment", "Note", "Entry", "Event")
_LAG_CAP = 1000   # reported lag stops counting here ("1000+ behind")


def index_watermark() -> int | None:
    """The change_log seq the semantic index reflects, or None when no
    index exists.

    Read from the index state file, never by asking the daemon (a status
    call would wake a multi-GB model). The file is rewritten only after a
    catch-up pass that had work to do, so its age says nothing about
    freshness; the watermark does.
    """
    state = _cache_dir() / "index_state.json"
    try:
        return int(json.loads(state.read_text()).get("watermark", 0))
    except (OSError, ValueError, TypeError):
        return None


def index_lag(soil, watermark: int) -> int:
    """How far the semantic index is behind: indexed-type items changed
    after its watermark and not yet reflected (api#91 — anything derived
    from the index must advertise its age).

    Only INDEXED_TYPES count. The change feed also carries audit facts,
    which the index never covers, so a raw count would call a current
    index stale after every audited request. Capped at _LAG_CAP so a long
    daemon outage stays cheap to report.

    Trustworthy only while the catch-up applies what it advances past:
    it is the counterpart of memogarden-search's `plan_from_change_log`.
    """
    marks = ",".join("?" * len(INDEXED_TYPES))
    row = soil._get_connection().execute(
        f"""SELECT COUNT(*) FROM (
                SELECT DISTINCT c.uuid FROM change_log c
                JOIN item i ON i.uuid = c.uuid
                WHERE c.tbl = 'item' AND c.seq > ?
                  AND i._type IN ({marks})
                LIMIT ?)""",
        (watermark, *INDEXED_TYPES, _LAG_CAP + 1),
    ).fetchone()
    return row[0]


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
                log = _model_dir() / "embed-daemon.log"
                # Popen dups the fds onto the child — close the parent's
                # copies immediately or the handle leaks per spawn (c0fd2d2)
                try:
                    with open(log, "ab") as f:
                        subprocess.Popen(
                            [str(_model_dir() / ".venv/bin/python"),
                             str(_model_dir() / "embed_daemon.py")],
                            cwd=str(_model_dir()), stdout=f, stderr=f,
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


def rerank_scores(q: str, uuids: list, timeout: int = 5):
    """Cross-encoder scores for already-recalled candidates (api#95 final).

    Scoring-only op by contract: `uuids` arrive visibility-filtered from
    the api boundary — the daemon scores whatever it is given. Returns
    {uuid: logit} or None when the daemon can't answer (no spawn
    fallback: if the daemon were up it just served the search call, so a
    rerank miss means degradation — callers fall back to their unreranked
    order, never to a cold start).
    """
    if not q or not uuids:
        return None
    payload = json.dumps({"op": "rerank", "q": q,
                          "uuids": list(uuids)[:64]}).encode()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
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
        if "results" not in out:
            return None
        return {u: sc for u, sc in out["results"]}
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout,
            json.JSONDecodeError, OSError):
        return None
