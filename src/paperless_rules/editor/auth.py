"""Editor auth gate — paperless API token verification with TTL cache."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException, Request

from paperless_rules.paperless_client import PaperlessClient, PaperlessError

_CACHE: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
_TTL_SECONDS = 60.0
# Bound the cache to defeat token-spray attempts that could otherwise
# grow it without limit. Real workloads have one or two active tokens.
_CACHE_MAX = 256


async def _verify(token: str, client: PaperlessClient) -> dict[str, Any] | None:
    now = time.monotonic()
    cached = _CACHE.get(token)
    if cached and cached[1] > now:
        _CACHE.move_to_end(token)
        return cached[0]
    user = await client.verify_token(token)
    if user is not None:
        _CACHE[token] = (user, now + _TTL_SECONDS)
        _CACHE.move_to_end(token)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return user


def make_auth_dep(state: Any, *, required: bool):
    """FastAPI dep that requires a valid paperless token. ``required=False``
    makes it a no-op (trusted LAN, tests)."""

    async def dep(request: Request) -> dict[str, Any] | None:
        if not required:
            return None
        if state.paperless is None:
            raise HTTPException(503, "paperless not configured")
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("token "):
            raise HTTPException(
                401, "missing paperless token", headers={"WWW-Authenticate": "Token"}
            )
        token = h.split(None, 1)[1].strip()
        if not token:
            raise HTTPException(401, "empty token")
        try:
            user = await _verify(token, state.paperless)
        except PaperlessError as e:
            # Network blip / paperless 5xx — tell the caller it's an upstream
            # problem, not a bad credential.
            raise HTTPException(502, f"could not reach paperless: {e}") from e
        if user is None:
            raise HTTPException(401, "invalid paperless token")
        return user

    return dep
