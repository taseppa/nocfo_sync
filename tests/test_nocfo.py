"""Client-level tests: MIME guessing and the real file_upload payload.

The regression these cover: NoCFO derives the attachment ``type`` (and
preview generation) from the ``type`` form field, not from the multipart
part's Content-Type.  Omitting it left files stored with an empty type.
"""

from __future__ import annotations

import httpx

from nocfo_sync.nocfo import NoCfoClient, guess_mime


class CapturingClient:
    """NoCfoClient wired to a MockTransport that records the request."""

    def __init__(self):
        self.request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            self.request = request
            return httpx.Response(200, json={"id": 1, "name": "x"})

        self.client = NoCfoClient("https://example.test", "tok", "biz")
        self.client._http = httpx.Client(
            base_url="https://example.test",
            headers={"Authorization": "Token tok"},
            transport=httpx.MockTransport(handler),
        )

    @property
    def body(self) -> bytes:
        assert self.request is not None
        return self.request.content


def test_guess_mime_known_types():
    assert guess_mime("a.pdf") == "application/pdf"
    assert guess_mime("b.JPG") == "image/jpeg"
    assert guess_mime("c.png") == "image/png"


def test_guess_mime_handles_extensions_mimetypes_misses():
    assert guess_mime("photo.heic") == "image/heic"
    assert guess_mime("photo.heif") == "image/heif"
    assert guess_mime("anim.webp") == "image/webp"


def test_guess_mime_falls_back_to_octet_stream():
    assert guess_mime("data.zzzunknown") == "application/octet-stream"
    assert guess_mime("noext") == "application/octet-stream"


def test_upload_sends_type_form_field():
    c = CapturingClient()
    c.client.upload_file("a.pdf", b"%PDF-1.4", mime="application/pdf")
    body = c.body
    assert b'name="type"' in body
    assert b"application/pdf" in body


def test_upload_infers_type_when_mime_not_supplied():
    c = CapturingClient()
    c.client.upload_file("note.txt", b"hello")
    body = c.body
    assert b'name="type"' in body
    assert b"text/plain" in body
    # the name still travels along too
    assert b'name="name"' in body
