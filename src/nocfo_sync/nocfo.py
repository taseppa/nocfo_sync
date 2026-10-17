"""NoCFO REST client.

Covers the documented files API plus the *undocumented* folders API that
we discovered by probing (see README): ``/v1/business/{slug}/folders/``.

The folders endpoints are not in the public OpenAPI schema; they may
change without notice.  All calls here fail loudly on unexpected shapes.
"""

from __future__ import annotations

import mimetypes
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

# Extensions that ``mimetypes`` does not reliably know (varies by Python
# build/platform).  These feed NoCFO's ``type`` field, so be explicit.
_EXTRA_MIME_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def guess_mime(name: str) -> str:
    """Best-effort MIME type for a filename.

    NoCFO stores this in the attachment's ``type`` field.  When ``type`` is
    empty the file is not classified and the server never generates a preview
    thumbnail, so the file "does not look like a file" in the app (see README).
    """
    lower = name.lower()
    for ext, mime in _EXTRA_MIME_TYPES.items():
        if lower.endswith(ext):
            return mime
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class NoCfoError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class NocfoFolder:
    id: int
    name: str
    parent: int | None
    breadcrumbs: list[dict] | None = None  # only from detail endpoint
    children: list[dict] | None = None
    has_content: bool | None = None


class NoCfoClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str, token: str, business_slug: str,
                 timeout: float = 120.0, max_retries: int = 4,
                 idempotency: bool = True):
        self.base_url = base_url.rstrip("/")
        self.business_slug = business_slug
        self.max_retries = max_retries
        self.idempotency = idempotency  # may be disabled at runtime (see below)
        self.notices: list[str] = []
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # -- low level -------------------------------------------------------

    def _request(self, method: str, path: str, retry_5xx: bool = True, **kwargs) -> Any:
        retry_statuses = set(self.RETRY_STATUSES)
        if not retry_5xx:
            retry_statuses -= {500, 502, 503, 504}
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                last = e
                if attempt == self.max_retries:
                    raise NoCfoError(f"{method} {path}: network error: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code >= 400:
                if resp.status_code in retry_statuses and attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise NoCfoError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:500]}",
                    resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise NoCfoError(f"{method} {path}: exhausted retries: {last}")

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = 1
        params = dict(params or {})
        params.setdefault("page_size", 100)
        while True:
            params["page"] = page
            data = self._request("GET", path, params=params)
            yield from data.get("results", [])
            if not data.get("next"):
                return
            page = data["next"]

    # -- businesses ------------------------------------------------------

    def check(self) -> str:
        """Verify auth + business slug; returns the business name."""
        data = self._request("GET", f"/v1/business/{self.business_slug}/")
        return data.get("name", self.business_slug)

    # -- token / business discovery (no slug needed) ---------------------

    def current_user(self) -> dict:
        """``GET /v1/user/`` — validates the token without a business slug.

        Used by the TUI on first run to check a freshly entered token.
        """
        return self._request("GET", "/v1/user/")

    def list_businesses(self) -> list[dict]:
        """``GET /v1/business/`` — businesses the token can access.

        Also slug-independent, so it both proves the token works and gives
        the TUI something to offer when no business slug is configured yet.
        """
        return list(self._paginate("/v1/business/"))

    # -- folders (undocumented!) ----------------------------------------

    def list_root_folders(self) -> list[NocfoFolder]:
        return [
            NocfoFolder(id=r["id"], name=r["name"], parent=r.get("parent"))
            for r in self._paginate(f"/v1/business/{self.business_slug}/folders/")
        ]

    def get_folder(self, folder_id: int) -> NocfoFolder:
        r = self._request("GET", f"/v1/business/{self.business_slug}/folders/{folder_id}/")
        return NocfoFolder(
            id=r["id"], name=r["name"], parent=r.get("parent"),
            breadcrumbs=r.get("breadcrumbs"), children=r.get("children"),
            has_content=r.get("has_content"),
        )

    def create_folder(self, name: str, parent: int | None) -> NocfoFolder:
        body: dict[str, Any] = {"name": name}
        if parent is not None:
            body["parent"] = parent
        r = self._request(
            "POST", f"/v1/business/{self.business_slug}/folders/", json=body
        )
        return NocfoFolder(id=r["id"], name=r["name"], parent=r.get("parent"))

    def delete_folder(self, folder_id: int) -> None:
        # NOTE: returns a 500 error page (not JSON) when the folder is not empty.
        self._request("DELETE", f"/v1/business/{self.business_slug}/folders/{folder_id}/")

    # -- files -----------------------------------------------------------

    def upload_file(
        self,
        name: str,
        content: bytes,
        folder_id: int | None = None,
        mime: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Upload via multipart.

        Sends ``Idempotency-Key`` when enabled so network retries cannot
        create duplicates.  Some NoCFO environments (observed on
        api-tst.nocfo.io) return 500 for any request carrying that header;
        on such a 500 we transparently retry once without the key and turn
        idempotency off for the rest of the session.

        The ``type`` form field is what NoCFO stores as the attachment type
        and what gates server-side preview generation; the multipart part's
        own ``Content-Type`` is ignored by the server.
        """
        if len(name) > 256:
            stem, dot, ext = name.rpartition(".")
            if dot and len(ext) < 20:
                name = name[: 256 - len(ext) - 1] + "." + ext
            else:
                name = name[:256]
        mime = mime or guess_mime(name)
        data: dict[str, str] = {"name": name, "type": mime}
        if folder_id is not None:
            data["folder_id"] = str(folder_id)
        if notes:
            data["notes"] = notes
        path = f"/v1/business/{self.business_slug}/file_upload/"
        files = {"file": (name, content, mime)}

        if self.idempotency:
            try:
                return self._request(
                    "POST", path, retry_5xx=False, data=data, files=files,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
            except NoCfoError as e:
                if e.status_code != 500:
                    raise
                self.idempotency = False
                self.notices.append(
                    "NoCFO returned 500 for file_upload with Idempotency-Key; "
                    "retrying without it and disabling idempotency for this run."
                )
        return self._request("POST", path, data=data, files=files)

    def list_files(self, folder: int | None = None, root: bool | None = None,
                   is_used: bool | None = None, search: str | None = None) -> Iterator[dict]:
        params: dict[str, Any] = {}
        if folder is not None:
            params["folder"] = folder
        if root is not None:
            params["root"] = str(root).lower()
        if is_used is not None:
            params["is_used"] = str(is_used).lower()
        if search is not None:
            params["search"] = search
        yield from self._paginate(f"/v1/business/{self.business_slug}/files/", params)

    def delete_file(self, file_id: int) -> None:
        self._request("DELETE", f"/v1/business/{self.business_slug}/files/{file_id}/")
