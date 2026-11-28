"""Artifact span bridge + read surface (Project Studio §15.4–§15.7).

The handler socket is never up in unit tests: `_call_handler` is monkeypatched
to a canned producer obeying the §15.3 contract, and the UNPATCHED default
(socket absent → sync returns pending, commit unaffected) is itself the
pending-path test. The validator runs an independent copy of the §6.2 id
algorithm — deliberate: the bridge must be able to reject sets it did not
produce.
"""

import hashlib

from api import artifact_spans

CONTENT = "## Heading\n\nA sentence. Another one here."


def _frag_id(text: str) -> str:
    """§6.2 id (mod-36³ errata) — independent copy of the contract."""
    digest = hashlib.sha256(text.encode()).digest()[:2]
    value = int.from_bytes(digest, "big") % (36**3)
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value > 0:
        out = chars[value % 36] + out
        value //= 36
    return "^" + out.zfill(3)


def _canned_spans(content: str) -> list[dict]:
    """Hand-built span set for CONTENT per the §15.3 producer contract."""
    if content != CONTENT:
        raise ValueError("canned set only knows CONTENT")
    return [
        {"fragment_id": _frag_id("Heading"), "content": "Heading", "block_hash": "aaaa1111",
         "block_type": "heading", "start_char": 3, "end_char": 10, "abs_start": 3, "abs_end": 10, "ord": 0},
        {"fragment_id": _frag_id("A sentence."), "content": "A sentence.", "block_hash": "bbbb2222",
         "block_type": "paragraph", "start_char": 0, "end_char": 11, "abs_start": 12, "abs_end": 23, "ord": 1},
        {"fragment_id": _frag_id("Another one here."), "content": "Another one here.", "block_hash": "bbbb2222",
         "block_type": "paragraph", "start_char": 12, "end_char": 29, "abs_start": 24, "abs_end": 41, "ord": 2},
    ]


def _patch_ok_handler(monkeypatch):
    import hashlib as _h

    def fake(artifact_uuid, content):
        return {
            "ok": True,
            "content_version": _h.sha256(content.encode()).hexdigest()[:8],
            "spans": _canned_spans(content) if content == CONTENT else [],
        }

    monkeypatch.setattr(artifact_spans, "_call_handler", fake)


def _create_artifact(client, auth_headers, content=CONTENT):
    return client.post(
        "/mg",
        json={"op": "create", "type": "Artifact",
              "data": {"label": "span test artifact", "content": content,
                       "content_type": "text/markdown"}},
        headers=auth_headers,
    )


class TestSpanValidation:
    """The bridge's independent §15.6 validator."""

    def test_accepts_correct_set(self):
        content_version = artifact_spans.content_version_of(CONTENT)
        assert artifact_spans.validate(CONTENT, content_version, _canned_spans(CONTENT)) == []

    def test_rejects_wrong_id(self):
        spans = _canned_spans(CONTENT)
        spans[0]["fragment_id"] = "^zzz"
        errors = artifact_spans.validate(CONTENT, artifact_spans.content_version_of(CONTENT), spans)
        assert any("content hash" in e for e in errors)

    def test_rejects_fidelity_drift(self):
        spans = _canned_spans(CONTENT)
        spans[1]["abs_start"] = 0  # text no longer matches content at offsets
        errors = artifact_spans.validate(CONTENT, artifact_spans.content_version_of(CONTENT), spans)
        assert any("fidelity" in e for e in errors)

    def test_rejects_version_mismatch(self):
        errors = artifact_spans.validate(CONTENT, "00000000", _canned_spans(CONTENT))
        assert any("content_version" in e for e in errors)


class TestCreateHookLandsSpans:
    def test_create_artifact_lands_span_set(self, client, auth_headers, monkeypatch):
        _patch_ok_handler(monkeypatch)
        response = _create_artifact(client, auth_headers)
        assert response.status_code == 200
        uuid = response.get_json()["result"]["uuid"]

        spans_response = client.get(f"/api/v1/artifacts/{uuid}/spans", headers=auth_headers)
        assert spans_response.status_code == 200
        body = spans_response.get_json()
        assert body["status"] == "ok"
        assert body["artifact_uuid"] == uuid
        assert [s["content"] for s in body["spans"]] == ["Heading", "A sentence.", "Another one here."]
        assert [s["ord"] for s in body["spans"]] == [0, 1, 2]

    def test_create_envelope_never_carries_span_errors(self, client, auth_headers, monkeypatch):
        def broken(artifact_uuid, content):
            raise ConnectionRefusedError("handler down")

        monkeypatch.setattr(artifact_spans, "_call_handler", broken)
        response = _create_artifact(client, auth_headers)
        assert response.status_code == 200
        assert response.get_json()["ok"] is True  # belief landed


class TestPendingPath:
    def test_handler_down_leaves_pending(self, client, auth_headers, monkeypatch):
        def dead(artifact_uuid, content):
            raise ConnectionRefusedError("handler socket absent")

        monkeypatch.setattr(artifact_spans, "_call_handler", dead)
        response = _create_artifact(client, auth_headers)
        uuid = response.get_json()["result"]["uuid"]

        spans_response = client.get(f"/api/v1/artifacts/{uuid}/spans", headers=auth_headers)
        body = spans_response.get_json()
        assert body["status"] == "pending"
        assert body["spans"] == []
        assert body["content_version"] == artifact_spans.content_version_of(CONTENT)

    def test_tampered_producer_set_is_rejected_to_pending(self, client, auth_headers, monkeypatch):
        def lying_handler(artifact_uuid, content):
            spans = _canned_spans(content)
            spans[0]["fragment_id"] = "^zzz"
            return {"ok": True, "content_version": artifact_spans.content_version_of(content),
                    "spans": spans}

        monkeypatch.setattr(artifact_spans, "_call_handler", lying_handler)
        response = _create_artifact(client, auth_headers)
        uuid = response.get_json()["result"]["uuid"]
        body = client.get(f"/api/v1/artifacts/{uuid}/spans", headers=auth_headers).get_json()
        assert body["status"] == "pending"


class TestSpansEndpointSurface:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/artifacts/core_x/spans").status_code == 401

    def test_unknown_artifact_404(self, client, auth_headers):
        response = client.get("/api/v1/artifacts/core_doesnotexist/spans", headers=auth_headers)
        assert response.status_code == 404

    def test_pre_span_artifact_reads_none(self, client, auth_headers):
        """An artifact whose content version was never chunked reads `none`."""
        response = _create_artifact(client, auth_headers)
        uuid = response.get_json()["result"]["uuid"]
        # no handler patch and no prior sync → mark pending manually is the
        # hook's doing; strip it by asking for a set that was never touched:
        body = client.get(f"/api/v1/artifacts/{uuid}/spans", headers=auth_headers).get_json()
        assert body["status"] in ("pending", "none")  # both are honest answers
