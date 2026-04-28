"""Async paperless-ngx REST API wrapper. PaperlessError on any non-2xx or
transport error. Accept header pinned to ``application/json; version=2``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx


class PaperlessError(Exception):
    pass


class PaperlessClient:
    """Async paperless-ngx client. One instance per app."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # ``verify`` accepts True / False / a CA-bundle path.
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": {
                "Authorization": f"Token {token}",
                "Accept": "application/json; version=2",
            },
            "timeout": timeout,
            "verify": verify,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PaperlessClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def health(self) -> dict[str, Any]:
        """Connectivity probe; never raises."""
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

    async def list_documents(
        self, query: str = "", page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if query:
            params["query"] = query
        return await self._get_json("/api/documents/", params=params)

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        return await self._get_json(f"/api/documents/{doc_id}/")

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        """Verify a paperless token; returns ``{"ok": True}`` or None on 401/403.
        Probe is ``GET /api/documents/?page_size=1`` (universal across
        paperless-ngx versions; ``/api/users/me/`` isn't)."""
        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json; version=2",
        }
        try:
            r = await self._client.get(
                "/api/documents/", headers=headers, params={"page_size": 1}
            )
        except httpx.HTTPError as e:
            raise PaperlessError(f"verify_token failed: {e}") from e
        if r.status_code in (401, 403):
            return None
        if r.status_code >= 400:
            raise PaperlessError(f"GET /api/documents/ HTTP {r.status_code}")
        return {"ok": True}

    async def get_preview(self, doc_id: int) -> tuple[bytes, str]:
        """Document PDF preview: raw bytes + content-type."""
        try:
            r = await self._client.get(f"/api/documents/{doc_id}/preview/")
        except httpx.HTTPError as e:
            raise PaperlessError(f"GET preview failed: {e}") from e
        if r.status_code >= 400:
            raise PaperlessError(
                f"GET /api/documents/{doc_id}/preview/ HTTP {r.status_code}"
            )
        return r.content, r.headers.get("content-type", "application/pdf")

    async def iter_documents(
        self,
        query: str = "",
        page_size: int = 100,
        ordering: str = "-id",
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate every document matching ``query``. Newest first by default."""
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

    async def find_one_by_name(
        self, kind: str, name: str
    ) -> dict[str, Any] | None:
        """First record matching ``name`` (case-insensitive). ``kind`` is
        the URL segment: ``correspondents`` / ``document_types`` / ``tags``."""
        data = await self._get_json(
            f"/api/{kind}/", params={"name__iexact": name}
        )
        results = data.get("results") or []
        return results[0] if results else None

    async def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        """Whole custom_fields list. Caller is expected to cache by name —
        older paperless versions don't accept ``name__iexact`` here."""
        data = await self._get_json("/api/custom_fields/", params={"page_size": 200})
        return list(data.get("results") or [])

    async def patch_document(
        self, doc_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
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
