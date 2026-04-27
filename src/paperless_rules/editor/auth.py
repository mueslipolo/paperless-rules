"""Editor auth gate — single sign-on via paperless API tokens.

The editor doesn't manage its own users. Instead it accepts the same
``Authorization: Token <user-token>`` header paperless does, verifies the
token by calling paperless's ``/api/users/me/`` once, and TTL-caches the
result so subsequent requests don't hammer paperless.

Revoking the token in paperless logs the user out within ``_TTL_SECONDS``.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Request

from paperless_rules.paperless_client import PaperlessClient, PaperlessError

# token → (user_dict, expires_at_monotonic). In-process only; cleared at
# restart, which is fine — users just paste the token again.
_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_TTL_SECONDS = 60.0


async def _verify(token: str, client: PaperlessClient) -> dict[str, Any] | None:
    now = time.monotonic()
    cached = _CACHE.get(token)
    if cached and cached[1] > now:
        return cached[0]
    user = await client.verify_token(token)
    if user is not None:
        _CACHE[token] = (user, now + _TTL_SECONDS)
    return user


def make_auth_dep(state: Any, *, required: bool):
    """Build a FastAPI dependency that gates a route behind a valid paperless token.

    `state` is the editor's mutable container with a `paperless: PaperlessClient`
    attribute (so the dep picks up the client even if it's initialised in
    `lifespan`, after `make_auth_dep` is first called).

    When `required` is False, the dep is a no-op — useful for trusted-LAN
    deployments and the test suite.
    """
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
