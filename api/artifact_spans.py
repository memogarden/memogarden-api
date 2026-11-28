"""Artifact span bridge — Project Studio §15.1 thin plumbing.

The main service stores span state but NEVER chunks: decomposition is the
Artifact Handler's job (memogarden-artifact-handler, a content-agnostic socket
worker with no Core access). This module is the bridge:

  - call the handler over its unix socket (§15.3 contract)
  - validate the returned set against the §15.6 invariants — with an
    independent copy of the id algorithm, because the bridge must be able to
    reject sets it did not produce
  - persist into artifact_fragment / artifact_span_set / artifact_span_status
    (schema 20261130; §15.4 diff-encoded: fragment rows are content-addressed,
    deltas' span_ops contribution is exactly the version's membership rows)
  - on ANY failure, leave the version `spans_pending` and swallow — the
    artifact's belief always outranks reference infrastructure (§15.6 step 4)

Atomicity note: the pending→ok marking is not yet inside the commit
transaction (§15.7 step 3's single-boundary write is a follow-up). The
*visible* contract holds regardless: a version reads pending until its set
lands, never silently wrong.
"""

import hashlib
import json
import logging
import os
import socket

from system.core import get_core
from utils import isodatetime

logger = logging.getLogger(__name__)

_ID_SPACE = 36 ** 3  # §6.3: ids are exactly 3 chars (§6.2 mod-36³ errata)
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_HANDLER_TIMEOUT = 15  # seconds; chunking is ms-fast, cold start ~300ms


def _handler_socket() -> str:
    """The handler's socket path (the systemd RuntimeDirectory form)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "memogarden-artifact-handler", "handler.sock")


def content_version_of(content: str) -> str:
    """Content-version id — same convention as system compute_content_hash."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


def _base36_id(value: int) -> str:
    out = ""
    while value > 0:
        out = _BASE36[value % 36] + out
        value //= 36
    return "^" + out.zfill(3)


def _salt_candidates(text: str, max_salt: int = 64) -> set[str]:
    """Every id the §6.2 salt chain could assign to `text`."""
    out = {_base36_id(int.from_bytes(hashlib.sha256(text.encode()).digest()[:2], "big") % _ID_SPACE)}
    for salt in range(1, max_salt + 1):
        digest = hashlib.sha256(f"{text}#{salt}".encode()).digest()[:2]
        out.add(_base36_id(int.from_bytes(digest, "big") % _ID_SPACE))
    return out


def validate(content: str, content_version: str, spans: list[dict]) -> list[str]:
    """§15.6 invariants, independent implementation (rejects foreign sets)."""
    errors: list[str] = []
    by_block: dict[str, list[tuple[int, int, int]]] = {}
    seen_ords: set[int] = set()
    for i, span in enumerate(spans):
        fid = span.get("fragment_id", "")
        text = span.get("content", "")
        start, end = span.get("start_char"), span.get("end_char")
        abs_start, abs_end = span.get("abs_start"), span.get("abs_end")

        ord_ = span.get("ord")
        if ord_ is None or ord_ in seen_ords:
            errors.append(f"span[{i}] ({fid}): duplicate or missing ord")
        else:
            seen_ords.add(ord_)

        if fid not in _salt_candidates(text):
            errors.append(f"span[{i}] ({fid}): id is not the content hash of its text")
        if abs_start is None or abs_end is None or content[abs_start:abs_end] != text:
            errors.append(f"span[{i}] ({fid}): fidelity — text != content at abs offsets")
        if start is None or end is None or end <= start or start < 0:
            errors.append(f"span[{i}] ({fid}): bad char range")
            continue
        by_block.setdefault(span.get("block_hash"), []).append((start, end, i))

    for bh, ranges in by_block.items():
        ordered = sorted(ranges)
        for (s1, e1, i1), (s2, e2, i2) in zip(ordered, ordered[1:]):
            if s2 < e1:
                errors.append(
                    f"spans {i1}/{i2} in block {str(bh)[:8]}: overlapping ranges"
                )
    if content_version != content_version_of(content):
        errors.append("content_version does not hash the given content")
    return errors


def _call_handler(artifact_uuid: str, content: str) -> dict:
    """§15.3 socket contract. Raises on unreachability or ok:false."""
    path = _handler_socket()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(_HANDLER_TIMEOUT)
    try:
        conn.connect(path)
        conn.sendall(json.dumps(
            {"artifact_uuid": artifact_uuid, "content": content}
        ).encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            data = conn.recv(65536)
            if not data:
                break
            response += data
    finally:
        conn.close()
    parsed = json.loads(response.decode("utf-8"))
    if not parsed.get("ok"):
        raise RuntimeError(f"handler rejected chunk: {parsed.get('error')}")
    return parsed


def _store(conn, artifact_uuid: str, content_version: str, spans: list[dict]) -> None:
    """Persist fragment rows (content-addressed, insert-only) + this version's
    membership (the delta's span_ops contribution) + ok status."""
    now = isodatetime.now()
    for span in spans:
        conn.execute(
            "INSERT OR REPLACE INTO artifact_fragment "
            "(artifact_uuid, fragment_id, content, block_hash, block_type, start_char, end_char) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (artifact_uuid, span["fragment_id"], span["content"], span["block_hash"],
             span.get("block_type", "paragraph"), span["start_char"], span["end_char"]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO artifact_span_set "
            "(artifact_uuid, content_version, fragment_id, ord, abs_start, abs_end) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (artifact_uuid, content_version, span["fragment_id"], span["ord"],
             span["abs_start"], span["abs_end"]),
        )
    conn.execute(
        "INSERT OR REPLACE INTO artifact_span_status "
        "(artifact_uuid, content_version, status, updated_at) VALUES (?, ?, 'ok', ?)",
        (artifact_uuid, content_version, now),
    )


def sync_spans(artifact_uuid: str, content: str) -> str:
    """§15.6 edit flow for one artifact content version.

    Marks the version pending, chunks via the handler, validates, stores.
    Never raises into the caller: any failure leaves `pending` for the sweep.
    Returns the final status ('ok' | 'pending').
    """
    plain = artifact_uuid.removeprefix("core_")
    version = content_version_of(content)
    try:
        with get_core() as core:
            core._conn.execute(
                "INSERT OR REPLACE INTO artifact_span_status "
                "(artifact_uuid, content_version, status, updated_at) VALUES (?, ?, 'pending', ?)",
                (plain, version, isodatetime.now()),
            )
            core._conn.commit()
    except Exception:  # noqa: BLE001 — pending-marking is best-effort
        logger.exception("span pending-mark failed for %s@%s", plain, version)

    try:
        result = _call_handler(plain, content)
    except Exception as e:  # noqa: BLE001 — §15.6 step 4: handler down = pending
        logger.warning("artifact handler unreachable for %s@%s: %s", plain, version, e)
        return "pending"

    errors = validate(content, result["content_version"], result.get("spans", []))
    if errors:
        logger.warning("handler span set rejected for %s@%s: %s", plain, version, errors)
        return "pending"

    try:
        with get_core() as core:
            _store(core._conn, plain, result["content_version"], result["spans"])
            core._conn.commit()
        return "ok"
    except Exception:  # noqa: BLE001
        logger.exception("span persistence failed for %s@%s (version stays pending)", plain, version)
        return "pending"


def maybe_sync(op: str, request_json: dict, result: dict | None) -> None:
    """Post-dispatch hook (semantic.py): sync spans after successful
    create(Arifact) / commit_artifact. Never raises."""
    try:
        if op == "create" and (result or {}).get("type") == "Artifact":
            content = (result.get("data") or {}).get("content")
            if result.get("uuid") and content:
                sync_spans(result["uuid"], content)
        elif op == "commit_artifact":
            artifact = request_json.get("artifact")
            content = (result or {}).get("new_content")
            if artifact and content:
                sync_spans(artifact, content)
    except Exception:  # noqa: BLE001 — the envelope must never carry span failures
        logger.exception("artifact span sync failed (spans stay pending)")


def status_for(conn, artifact_uuid: str, content_version: str) -> str:
    row = conn.execute(
        "SELECT status FROM artifact_span_status "
        "WHERE artifact_uuid = ? AND content_version = ?",
        (artifact_uuid.removeprefix("core_"), content_version),
    ).fetchone()
    return row["status"] if row else "none"


def spans_for(conn, artifact_uuid: str, content_version: str) -> list[dict]:
    rows = conn.execute(
        "SELECT m.fragment_id, m.ord, m.abs_start, m.abs_end, f.content, "
        "f.block_hash, f.block_type, f.start_char, f.end_char "
        "FROM artifact_span_set m JOIN artifact_fragment f "
        " ON f.artifact_uuid = m.artifact_uuid AND f.fragment_id = m.fragment_id "
        "WHERE m.artifact_uuid = ? AND m.content_version = ? ORDER BY m.ord",
        (artifact_uuid.removeprefix("core_"), content_version),
    ).fetchall()
    return [
        {
            "fragment_id": r["fragment_id"],
            "ord": r["ord"],
            "content": r["content"],
            "block_hash": r["block_hash"],
            "block_type": r["block_type"],
            "start_char": r["start_char"],
            "end_char": r["end_char"],
            "abs_start": r["abs_start"],
            "abs_end": r["abs_end"],
        }
        for r in rows
    ]
