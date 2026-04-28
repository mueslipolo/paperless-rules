"""Tests for /api/post-consume and /api/rules/{filename}/apply.

Both endpoints write to paperless via apply_rules_to_document, so we use a
richer fake than test_editor_api.py — it tracks patch_document calls so we
can assert dry-run vs commit behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from paperless_rules.config import Config
from paperless_rules.editor.app import create_app

ACME = """Acme Telecom (Europe) SARL
Numéro de facture 4521
Date d'émission 15.03.2024
Total à payer EUR 1234,50
"""


class FakePaperless:
    """Minimal stub covering everything apply_rules_to_document touches."""

    def __init__(self, docs: dict[int, dict[str, Any]]):
        self.docs = docs
        self.patches: list[tuple[int, dict[str, Any]]] = []
        self._next_id = 100

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "url": "test://paperless"}

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        return self.docs[doc_id]

    async def list_documents(self, query="", page=1, page_size=25) -> dict[str, Any]:
        return {
            "count": len(self.docs),
            "next": None,
            "previous": None,
            "results": list(self.docs.values()),
        }

    async def iter_documents(self, query="", page_size=50, ordering=""):
        for d in self.docs.values():
            yield d

    async def find_one_by_name(self, kind: str, name: str):
        return None

    async def create(self, kind: str, payload: dict[str, Any]):
        self._next_id += 1
        return {
            "id": self._next_id,
            "name": payload.get("name"),
            "data_type": payload.get("data_type"),
        }

    async def list_custom_fields(self):
        return []

    async def patch_document(self, doc_id: int, payload: dict[str, Any]):
        self.patches.append((doc_id, payload))
        return {"id": doc_id}

    async def aclose(self):
        pass


@pytest.fixture
def fake() -> FakePaperless:
    return FakePaperless(
        {
            1: {
                "id": 1,
                "title": "Acme #1",
                "content": ACME,
                "correspondent": None,
                "document_type": None,
                "tags": [],
                "custom_fields": [],
            },
            2: {
                "id": 2,
                "title": "Acme #2",
                "content": ACME.replace("4521", "4522"),
                "correspondent": None,
                "document_type": None,
                "tags": [],
                "custom_fields": [],
            },
            3: {
                "id": 3,
                "title": "Other vendor",
                "content": "different doc, no match here",
                "correspondent": None,
                "document_type": None,
                "tags": [],
                "custom_fields": [],
            },
        }
    )


@pytest.fixture
def app_with_rule(tmp_path: Path, fake: FakePaperless):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "01_acme.yml").write_text(
        "match: 'Acme'\n"
        "fields:\n"
        "  invoice_number:\n"
        "    regex: 'Numéro de facture\\s+(\\d+)'\n"
        "    type: str\n",
        encoding="utf-8",
    )
    cfg = Config(
        rules_dir=rules_dir,
        state_dir=tmp_path / "state",
        editor_auth_required=False,
    )
    return create_app(cfg, paperless_client=fake), fake


# ── /api/post-consume ────────────────────────────────────────────────


def test_post_consume_matches_and_patches(app_with_rule):
    app, fake = app_with_rule
    with TestClient(app) as c:
        body = c.post("/api/post-consume", json={"doc_id": 1}).json()
    assert body["matched"] is True
    assert body["rule_filename"] == "01_acme.yml"
    assert body["payload"]["custom_fields"][0]["value"] == "4521"
    assert len(fake.patches) == 1


def test_post_consume_no_match_no_patch(app_with_rule):
    app, fake = app_with_rule
    with TestClient(app) as c:
        body = c.post("/api/post-consume", json={"doc_id": 3}).json()
    assert body["matched"] is False
    assert fake.patches == []


def test_post_consume_no_rules_skipped(tmp_path: Path, fake: FakePaperless):
    cfg = Config(rules_dir=tmp_path, state_dir=tmp_path, editor_auth_required=False)
    app = create_app(cfg, paperless_client=fake)
    with TestClient(app) as c:
        body = c.post("/api/post-consume", json={"doc_id": 1}).json()
    assert body["matched"] is False
    assert body["skipped"] == "no rules loaded"


# ── /api/rules/{filename}/apply ──────────────────────────────────────


def test_apply_dry_run_corpus_no_patches(app_with_rule):
    app, fake = app_with_rule
    with TestClient(app) as c:
        body = c.post(
            "/api/rules/01_acme.yml/apply",
            json={
                "doc_ids": [1, 2, 3],
                "dry_run": True,
            },
        ).json()
    assert body["scanned"] == 3
    assert body["matched"] == 2  # 1 + 2 match, 3 doesn't
    assert body["written"] == 0  # dry-run never writes
    assert body["dry_run"] is True
    assert fake.patches == []


def test_apply_commit_corpus_writes(app_with_rule):
    app, fake = app_with_rule
    with TestClient(app) as c:
        body = c.post(
            "/api/rules/01_acme.yml/apply",
            json={
                "doc_ids": [1, 2],
                "dry_run": False,
            },
        ).json()
    assert body["matched"] == 2
    assert body["written"] == 2
    assert len(fake.patches) == 2
    # Each patch carries the extracted invoice_number
    nums = sorted(p[1]["custom_fields"][0]["value"] for p in fake.patches)
    assert nums == ["4521", "4522"]


def test_apply_library_scope_iterates_paperless(app_with_rule):
    app, fake = app_with_rule
    with TestClient(app) as c:
        body = c.post("/api/rules/01_acme.yml/apply", json={"dry_run": True}).json()
    assert body["scanned"] == 3  # iter_documents yielded all 3
    assert body["matched"] == 2
    assert body["truncated"] is False
    assert fake.patches == []


def test_apply_max_docs_truncates(app_with_rule):
    app, _fake = app_with_rule
    with TestClient(app) as c:
        body = c.post(
            "/api/rules/01_acme.yml/apply",
            json={
                "dry_run": True,
                "max_docs": 1,
            },
        ).json()
    assert body["scanned"] == 1
    assert body["truncated"] is True


def test_apply_unknown_rule_404(app_with_rule):
    app, _ = app_with_rule
    with TestClient(app) as c:
        r = c.post("/api/rules/missing.yml/apply", json={"doc_ids": [1]})
    assert r.status_code == 404


def test_apply_invalid_yaml_400(tmp_path: Path, fake: FakePaperless):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "broken.yml").write_text("not: a: valid: yaml: ::\n")
    cfg = Config(rules_dir=rules_dir, state_dir=tmp_path / "state", editor_auth_required=False)
    app = create_app(cfg, paperless_client=fake)
    with TestClient(app) as c:
        r = c.post("/api/rules/broken.yml/apply", json={"doc_ids": [1]})
    assert r.status_code == 400
