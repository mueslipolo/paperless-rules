"""Tier-3 e2e fixtures.

Brings up `docker-compose.test.yml` once per session, mints an admin API
token via `manage.py shell`, waits for paperless to ingest the engine
fixture .txt files (mounted into its consume directory), and yields a
ready-to-use `PaperlessClient` plus the seeded document IDs.

Auto-skipped when no compose CLI is available — `docker compose`,
`podman compose`, plain `docker-compose` and `podman-compose` are tried
in that order. Set KEEP_E2E_STACK=1 to keep containers around between
runs (useful while iterating on tests).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.test.yml"
PAPERLESS_URL = "http://127.0.0.1:18000"
E2E_DIR = Path(__file__).parent.resolve()


def pytest_collection_modifyitems(config, items):
    """Apply the `e2e` marker to every test under tests/e2e/ so the default
    `-m 'not e2e'` filter (set in pyproject.toml) keeps them out of normal
    runs. Conftest-level `pytestmark = ...` doesn't propagate to test modules,
    so we attach the marker imperatively during collection."""
    e2e_marker = pytest.mark.e2e
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if E2E_DIR == item_path.parent or E2E_DIR in item_path.parents:
            item.add_marker(e2e_marker)


def _detect_compose_cmd() -> list[str] | None:
    """Pick the first working compose CLI on this host. Order favours
    `docker` (CI, cloud) before `podman` (local dev on the user's box)."""
    candidates: list[list[str]] = [
        ["docker", "compose"],
        ["podman", "compose"],
        ["docker-compose"],
        ["podman-compose"],
    ]
    for cand in candidates:
        if shutil.which(cand[0]) is None:
            continue
        try:
            r = subprocess.run(
                [*cand, "version"], capture_output=True, timeout=10
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if r.returncode == 0:
            return cand
    return None


COMPOSE_CMD = _detect_compose_cmd()


def _compose(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    if COMPOSE_CMD is None:
        pytest.skip("no compose CLI available")
    return subprocess.run(
        [*COMPOSE_CMD, "-f", str(COMPOSE_FILE), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=check,
        **kw,
    )


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    """Bring up the paperless test stack for the whole session."""
    if COMPOSE_CMD is None:
        pytest.skip("compose unavailable; install docker or podman to run tier-3 tests")
    _compose("up", "-d")
    try:
        _wait_for_paperless()
        yield
    finally:
        if os.environ.get("KEEP_E2E_STACK") != "1":
            _compose("down", "-v", check=False, timeout=120)


def _wait_for_paperless(timeout: float = 240.0) -> None:
    """Poll /api/ until paperless responds 200 (with auth header it's 200,
    without it 401 — both confirm the server is up)."""
    deadline = time.monotonic() + timeout
    last_err = "no attempt yet"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{PAPERLESS_URL}/api/", timeout=5.0)
            # Either 200 (open) or 401 (needs auth) prove the server is alive.
            if r.status_code in (200, 401):
                return
            last_err = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last_err = str(e)
        time.sleep(2.0)
    raise RuntimeError(
        f"paperless did not become healthy within {timeout:.0f}s ({last_err})"
    )


@pytest.fixture(scope="session")
def admin_token(compose_stack) -> str:
    """Mint and return a paperless admin API token.

    Paperless creates the admin user from PAPERLESS_ADMIN_USER/PASSWORD env
    vars on first boot; we then run a short Django shell snippet inside
    the container to extract or create a DRF auth token.
    """
    script = (
        "from django.contrib.auth import get_user_model\n"
        "from rest_framework.authtoken.models import Token\n"
        "u = get_user_model().objects.get(username='admin')\n"
        "t, _ = Token.objects.get_or_create(user=u)\n"
        "print(t.key)\n"
    )
    # Retry briefly — admin creation can lag a few seconds behind health.
    deadline = time.monotonic() + 60.0
    last_err = ""
    while time.monotonic() < deadline:
        r = _compose(
            "exec", "-T", "test-paperless",
            "python", "manage.py", "shell", "-c", script,
            check=False,
        )
        if r.returncode == 0:
            for line in reversed(r.stdout.splitlines()):
                line = line.strip()
                if len(line) >= 32 and all(c.isalnum() for c in line):
                    return line
        last_err = r.stderr or r.stdout
        time.sleep(2.0)
    raise RuntimeError(f"could not mint admin token: {last_err[-400:]}")


@pytest.fixture(scope="session")
def paperless_client_factory(admin_token):
    """Returns a callable that builds a fresh PaperlessClient. Used so each
    test can hold its own client instance with its own httpx loop binding."""
    from paperless_rules.paperless_client import PaperlessClient

    def factory():
        return PaperlessClient(PAPERLESS_URL, admin_token)

    return factory


@pytest.fixture(scope="session")
def seeded_doc_ids(compose_stack, admin_token) -> list[int]:
    """Wait for paperless to ingest every fixture .txt and return their IDs.

    The compose file mounts tests/fixtures/ → /usr/src/paperless/consume.
    Paperless's consumer poller picks them up; .txt files skip OCR so
    `content` is byte-identical to the fixture file.
    """
    expected = {p.stem for p in (PROJECT_ROOT / "tests" / "fixtures").glob("*.txt")}
    if not expected:
        pytest.skip("no .txt fixtures to seed")

    headers = {"Authorization": f"Token {admin_token}"}
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        r = httpx.get(
            f"{PAPERLESS_URL}/api/documents/?page_size=100",
            headers=headers,
            timeout=10.0,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            seen = {d["title"] for d in results}
            # paperless titles a .txt file by its stem.
            if expected.issubset(seen):
                return [d["id"] for d in results]
        time.sleep(3.0)
    raise RuntimeError(
        f"paperless did not ingest all fixtures (expected={expected})"
    )


@pytest.fixture
def fresh_rules_dir(tmp_path) -> Path:
    """Per-test rules directory. Tests that need a runtime build a rule file
    in here, point Config at it, and run apply / poller."""
    d = tmp_path / "rules"
    d.mkdir()
    return d
