"""Async paperless-ngx REST API wrapper.

Thin httpx-based client. The editor uses the read methods (list, get, search);
the runtime uses these plus the resolve/create/PATCH methods for writing
metadata back. PaperlessError is raised for any unexpected non-200 response
or transport error so callers can translate cleanly into HTTP responses.

We pin the Accept header to a stable shape; paperless-ngx versions its API
with the `application/json; version=N` content type. version=2 is the
current stable contract used by paperless-rules — newer versions remain
backward compatible with this header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx


class PaperlessError(Exception):
    """Raised when a paperless API call fails or returns unexpected data."""


class PaperlessClient:
    """Async paperless-ngx client. One instance per app, reused across calls.

    Pass `transport=httpx.MockTransport(...)` in tests to avoid real network.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json; version=2",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PaperlessClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ── health ────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """Probe paperless connectivity. Never raises; returns status dict."""
        try:
            r = await self._client.get("/api/")
        except httpx.HTTPError as e:
            return {"ok": False, "url": self.base_url, "error": str(e)}
        if r.status_code == 200:
            return {"ok": True, "url": self.base_url}
        return {
            "ok": False,
            "url": self.base_url,
            "error": f"HTTP {r.status_code}",
        }

    # ── documents ─────────────────────────────────────────────────────

    async def list_documents(
        self, query: str = "", page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        """Paginated document list. Returns the paperless response shape:
        `{count, next, previous, results: [{id, title, content, created, ...}]}`.
        """
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if query:
            params["query"] = query
        return await self._get_json("/api/documents/", params=params)

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        """Full document record including OCR `content` field."""
        return await self._get_json(f"/api/documents/{doc_id}/")

    async def iter_documents(
        self,
        query: str = "",
        page_size: int = 100,
        ordering: str = "-id",
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-iterate every document matching `query`, paginating until done.

        `ordering=-id` makes us see newest first — convenient for a poller
        that wants to short-circuit once it sees a doc it has already processed.
        """
        page = 1
        while True:
            params: dict[str, Any] = {
                "page": page,
                "page_size": page_size,
                "ordering": ordering,
            }
            if query:
                params["query"] = query
            data = await self._get_json("/api/documents/", params=params)
            for doc in data.get("results") or []:
                yield doc
            if not data.get("next"):
                return
            page += 1

    # ── metadata resolve / create (correspondents, types, tags, fields) ──

    async def find_one_by_name(
        self, kind: str, name: str
    ) -> dict[str, Any] | None:
        """First record matching `name` exactly (case-insensitive). None if absent.

        `kind` is the URL segment: `correspondents`, `document_types`, `tags`.
        Paperless inherits Django filter semantics, so `name__iexact` works.
        """
        data = await self._get_json(
            f"/api/{kind}/", params={"name__iexact": name}
        )
        results = data.get("results") or []
        return results[0] if results else None

    async def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to /api/<kind>/ and return the created record."""
        try:
            r = await self._client.post(f"/api/{kind}/", json=payload)
        except httpx.HTTPError as e:
            raise PaperlessError(f"paperless POST /api/{kind}/ failed: {e}") from e
        if r.status_code >= 400:
            raise PaperlessError(
                f"paperless POST /api/{kind}/ HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError as e:
            raise PaperlessError(
                f"paperless returned non-JSON for POST /api/{kind}/"
            ) from e

    async def list_custom_fields(self) -> list[dict[str, Any]]:
        """Custom fields don't take name__iexact in older paperless versions —
        fetch the full list once and cache by name on the caller side.
        """
        data = await self._get_json("/api/custom_fields/", params={"page_size": 200})
        return list(data.get("results") or [])

    async def patch_document(
        self, doc_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/documents/{id}/. Used to write back metadata."""
        try:
            r = await self._client.patch(f"/api/documents/{doc_id}/", json=payload)
        except httpx.HTTPError as e:
            raise PaperlessError(f"PATCH failed: {e}") from e
        if r.status_code >= 400:
            raise PaperlessError(
                f"PATCH /api/documents/{doc_id}/ HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError as e:
            raise PaperlessError("paperless returned non-JSON for PATCH") from e

    # ── internals ─────────────────────────────────────────────────────

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise PaperlessError(f"paperless request failed: {e}") from e
        if r.status_code == 404:
            raise PaperlessError(f"not found: {path}")
        if r.status_code >= 400:
            raise PaperlessError(
                f"paperless {path} returned HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError as e:
            raise PaperlessError(f"paperless returned non-JSON for {path}") from e
