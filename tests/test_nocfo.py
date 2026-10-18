"""Client-level tests: MIME guessing and the real file_upload payload.

The regression these cover: NoCFO derives the attachment ``type`` (and
preview generation) from the ``type`` form field, not from the multipart
part's Content-Type.  Omitting it left files stored with an empty type.
"""

from __future__ import annotations

import io

import httpx
import pytest

from nocfo_sync.nocfo import NoCfoClient, NoCfoError, guess_mime


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


# -- retries -------------------------------------------------------------------


def _client_with(handler, monkeypatch, **kw) -> tuple[NoCfoClient, list[float]]:
    sleeps: list[float] = []
    monkeypatch.setattr("nocfo_sync.nocfo.time.sleep", sleeps.append)
    client = NoCfoClient("https://example.test", "tok", "biz", max_retries=3, **kw)
    client._http = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    return client, sleeps


def test_upload_without_key_is_not_retried_after_it_may_have_landed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client_with(handler, monkeypatch, idempotency=False)
    with pytest.raises(NoCfoError):
        client.upload_file("a.pdf", b"x")
    assert len(calls) == 1  # a retry could store the file twice


def test_upload_without_key_retries_when_never_sent(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"id": 1})

    client, _ = _client_with(handler, monkeypatch, idempotency=False)
    assert client.upload_file("a.pdf", b"x") == {"id": 1}
    assert len(calls) == 2


def test_streamed_upload_is_rewound_for_a_retry(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(request.read())
        if len(bodies) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": 1})

    client, _ = _client_with(handler, monkeypatch)  # idempotency key: retried
    client.upload_file("a.txt", io.BytesIO(b"file-body"))
    assert len(bodies) == 2
    assert all(b"file-body" in b for b in bodies)


def test_429_honours_retry_after(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"name": "Biz"})

    client, sleeps = _client_with(handler, monkeypatch)
    assert client.check() == "Biz"
    assert sleeps == [7.0]


def test_create_folder_is_not_retried_on_5xx(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502)

    client, _ = _client_with(handler, monkeypatch)
    with pytest.raises(NoCfoError):
        client.create_folder("x", None)
    assert len(calls) == 1
