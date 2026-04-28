"""Tests for the editor's paperless-token auth gate.

Behaviour we want to lock in:
- /api/health is always reachable (no token), and reports `auth_required`.
- When `editor_auth_required=True`, every other /api/* returns 401 without
  a valid `Authorization: Token …` header.
- When the token is accepted by paperless's /api/users/me/, calls succeed.
- When the token is rejected (paperless returns 401), the route returns 401.
- When `editor_auth_required=False`, no header is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from paperless_rules.config import Config
from paperless_rules.editor import auth as auth_mod
from paperless_rules.editor.app import create_app


class FakePaperless:
    """Minimal stub: tracks calls to verify_token and returns a configured user."""

    def __init__(self, *, valid_tokens: dict[str, dict[str, Any]] | None = None):
        self.valid_tokens = valid_tokens or {}
        self.verify_calls = 0

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "url": "http://paperless.test"}

    async def list_documents(self, **_):
        return {"count": 0, "next": None, "previous": None, "results": []}

    async def aclose(self) -> None:
        pass

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        self.verify_calls += 1
        return self.valid_tokens.get(token)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Auth dep keeps an in-process token cache; reset it between tests."""
    auth_mod._CACHE.clear()
    yield
    auth_mod._CACHE.clear()


@pytest.fixture
def fake():
    return FakePaperless(valid_tokens={"good-token": {"id": 1, "username": "yves"}})


@pytest.fixture
def auth_client(tmp_path, fake):
    cfg = Config(
        rules_dir=tmp_path / "rules",
        state_dir=tmp_path / "state",
        editor_auth_required=True,
    )
    with TestClient(create_app(cfg, paperless_client=fake)) as c:
        yield c, fake


def test_health_open_when_auth_required(auth_client):
    c, _ = auth_client
    body = c.get("/api/health").json()
    assert body["auth_required"] is True
    assert "paperless" in body


def test_protected_route_401_without_token(auth_client):
    c, _ = auth_client
    r = c.get("/api/documents")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Token"


def test_protected_route_401_with_invalid_token(auth_client):
    c, fake = auth_client
    r = c.get("/api/documents", headers={"Authorization": "Token bogus"})
    assert r.status_code == 401
    assert fake.verify_calls == 1


def test_protected_route_200_with_valid_token(auth_client):
    c, _ = auth_client
    r = c.get("/api/documents", headers={"Authorization": "Token good-token"})
    assert r.status_code == 200


def test_token_cached_within_ttl(auth_client):
    c, fake = auth_client
    headers = {"Authorization": "Token good-token"}
    c.get("/api/documents", headers=headers)
    c.get("/api/documents", headers=headers)
    c.get("/api/documents", headers=headers)
    # First call hits paperless; subsequent stay within the TTL window.
    assert fake.verify_calls == 1


def test_disabled_auth_skips_gate(tmp_path, fake):
    cfg = Config(
        rules_dir=tmp_path / "rules",
        state_dir=tmp_path / "state",
        editor_auth_required=False,
    )
    with TestClient(create_app(cfg, paperless_client=fake)) as c:
        r = c.get("/api/documents")  # no header
        assert r.status_code == 200
        # auth dep was a no-op → never called paperless
        assert fake.verify_calls == 0


def test_health_reports_auth_required_false_when_disabled(tmp_path, fake):
    cfg = Config(
        rules_dir=tmp_path / "rules",
        state_dir=tmp_path / "state",
        editor_auth_required=False,
    )
    with TestClient(create_app(cfg, paperless_client=fake)) as c:
        assert c.get("/api/health").json()["auth_required"] is False


async def test_auth_cache_is_bounded(monkeypatch):
    """Token spray must not grow the cache without limit."""
    monkeypatch.setattr(auth_mod, "_CACHE_MAX", 4)
    auth_mod._CACHE.clear()

    class _Fake:
        async def verify_token(self, token):
            return {"id": 1, "username": "u"}

    client = _Fake()
    for i in range(20):
        await auth_mod._verify(f"t-{i}", client)  # type: ignore[arg-type]
    assert len(auth_mod._CACHE) == 4
    # The most recently inserted ones survive (LRU).
    assert "t-19" in auth_mod._CACHE
    assert "t-0" not in auth_mod._CACHE
    auth_mod._CACHE.clear()
