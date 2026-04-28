"""EDITOR_READONLY=true blocks every mutation — paperless PATCHes and
on-disk rule writes/deletes — while leaving read paths and dry-runs intact.

Used for the "laptop dev mode" preset in .env.dev.example.
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
Total à payer EUR 1234,50
"""


class FakePaperless:
    """Same shape as test_editor_apply.FakePaperless (kept inline so this
    file is independent and the failure modes can't blame a shared stub)."""

    def __init__(self, docs):
        self.docs = docs
        self.patches: list[tuple[int, dict[str, Any]]] = []
        self._next_id = 100

    async def health(self): return {"ok": True, "url": "test://paperless"}
    async def get_document(self, doc_id): return self.docs[doc_id]
    async def list_documents(self, **_):
        return {"count": len(self.docs), "next": None, "previous": None,
                "results": list(self.docs.values())}
    async def iter_documents(self, query="", page_size=50, ordering=""):
        for d in self.docs.values():
            yield d
    async def find_one_by_name(self, kind, name): return None
    async def create(self, kind, payload):
        self._next_id += 1
        return {"id": self._next_id, "name": payload.get("name"),
                "data_type": payload.get("data_type")}
    async def list_custom_fields(self): return []
    async def patch_document(self, doc_id, payload):
        self.patches.append((doc_id, payload)); return {"id": doc_id}
    async def aclose(self): pass


@pytest.fixture
def fake():
    return FakePaperless({
        1: {"id": 1, "title": "Acme", "content": ACME,
            "correspondent": None, "document_type": None, "tags": [], "custom_fields": []},
    })


@pytest.fixture
def readonly_client(tmp_path: Path, fake: FakePaperless):
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
        rules_dir=rules_dir, state_dir=tmp_path / "state",
        editor_auth_required=False,    # laptop preset combo
        editor_readonly=True,
    )
    return create_app(cfg, paperless_client=fake), fake, rules_dir


def test_health_reports_readonly(readonly_client):
    app, _, _ = readonly_client
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["readonly"] is True


def test_read_routes_still_work(readonly_client):
    app, _, _ = readonly_client
    with TestClient(app) as c:
        assert c.get("/api/rules").status_code == 200
        assert c.get("/api/rules/01_acme.yml").status_code == 200
        assert c.get("/api/documents").status_code == 200
        assert c.get("/api/custom_fields").status_code == 200


def test_save_rule_blocked(readonly_client):
    app, _, _ = readonly_client
    with TestClient(app) as c:
        r = c.post("/api/rules", json={"filename": "02.yml", "yaml": "match: 'x'\n"})
    assert r.status_code == 405
    assert "read-only" in r.json()["detail"].lower()


def test_delete_rule_blocked(readonly_client):
    app, _, rules_dir = readonly_client
    with TestClient(app) as c:
        r = c.delete("/api/rules/01_acme.yml")
    assert r.status_code == 405
    # File still on disk
    assert (rules_dir / "01_acme.yml").exists()


def test_post_consume_blocked(readonly_client):
    app, fake, _ = readonly_client
    with TestClient(app) as c:
        r = c.post("/api/post-consume", json={"doc_id": 1})
    assert r.status_code == 405
    assert fake.patches == []


def test_apply_dry_run_allowed(readonly_client):
    app, fake, _ = readonly_client
    with TestClient(app) as c:
        r = c.post("/api/rules/01_acme.yml/apply",
                   json={"doc_ids": [1], "dry_run": True})
    assert r.status_code == 200
    assert r.json()["matched"] == 1
    assert fake.patches == []           # dry-run doesn't write


def test_apply_real_blocked(readonly_client):
    app, fake, _ = readonly_client
    with TestClient(app) as c:
        r = c.post("/api/rules/01_acme.yml/apply",
                   json={"doc_ids": [1], "dry_run": False})
    assert r.status_code == 405
    assert fake.patches == []


def test_writable_default_unaffected(tmp_path: Path, fake: FakePaperless):
    """When EDITOR_READONLY isn't set, the regular write path still works."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    cfg = Config(rules_dir=rules_dir, state_dir=tmp_path / "state",
                 editor_auth_required=False)   # editor_readonly defaults to False
    app = create_app(cfg, paperless_client=fake)
    with TestClient(app) as c:
        r = c.post("/api/rules", json={"filename": "01.yml", "yaml": "match: 'x'\n"})
    assert r.status_code == 200
    assert (rules_dir / "01.yml").exists()
