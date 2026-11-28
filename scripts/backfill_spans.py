"""Backfill span sets for live artifacts (Project Studio §15).

Run with the API venv while the artifact handler socket is up:

    cd memogarden-api
    .venv/bin/python scripts/backfill_spans.py [--limit N]

Safe to re-run: content versions already 'ok' are skipped; 'pending'/'none'
are re-chunked via the handler socket. Metadata-only concern does not apply
here — one set per content version, and content versions never change.
"""

import argparse
import json
import sys

from api import artifact_spans
from system.core import get_core


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="stop after N artifacts (0 = all)")
    args = parser.parse_args()

    with get_core() as core:
        rows = core._conn.execute(
            "SELECT uuid, data FROM entity WHERE type = 'Artifact' AND superseded_by IS NULL"
        ).fetchall()

    print(f"live artifacts: {len(rows)}")
    ok = skipped = pending = no_content = 0
    processed = 0
    for row in rows:
        if args.limit and processed >= args.limit:
            break
        uuid = row["uuid"]
        data = json.loads(row["data"]) if row["data"] else {}
        content = data.get("content") or ""
        if not content:
            no_content += 1
            continue
        version = artifact_spans.content_version_of(content)
        with get_core() as core:
            status = artifact_spans.status_for(core._conn, uuid, version)
        if status == "ok":
            skipped += 1
            continue

        result = artifact_spans.sync_spans(uuid, content)
        processed += 1
        if result == "ok":
            ok += 1
        else:
            pending += 1
        if processed % 25 == 0:
            print(f"  … processed {processed} (ok={ok} pending={pending})")

    print(
        f"done: ok={ok} pending={pending} skipped-already-ok={skipped} "
        f"no-content={no_content} processed={processed}"
    )
    return 0 if pending == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
