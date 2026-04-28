"""Editor API tests against a fake paperless. FastAPI TestClient + duck-typed client."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paperless_rules.config import Config
from paperless_rules.editor.app import create_app
from paperless_rules.paperless_client import PaperlessError

FIXTURES = Path(__file__).parent / "fixtures"
ACME = (FIXTURES / "acme_invoice.txt").read_text(encoding="utf-8")


class FakePaperless:
    def __init__(self, docs=None, healthy=True):
        self.docs = docs or {}
        self.healthy = healthy

    async def health(self):
        if self.healthy:
            return {"ok": True, "url": "test://paperless"}
        return {"ok": False, "url": "test://paperless", "error": "down"}

    async def list_documents(self, query="", page=1, page_size=25):
        results = list(self.docs.values())
        if query:
            q = query.lower()
            results = [
                d for d in results
                if q in (d.get("title", "") or "").lower()
                or q in (d.get("content", "") or "").lower()
            ]
        start = (page - 1) * page_size
        return {
            "count": len(results), "next": None, "previous": None,
            "results": results[start:start + page_size],
        }

    async def get_document(self, doc_id):
        if doc_id not in self.docs:
            raise PaperlessError(f"document {doc_id} not found")
        return self.docs[doc_id]

    async def aclose(self):
        pass


@pytest.fixture
def fake():
    return FakePaperless({
        42: {
            "id": 42, "title": "Acme Mar 2024", "content": ACME,
            "created": "2024-03-15T00:00:00Z",
        },
        43: {"id": 43, "title": "Empty doc", "content": ""},
    })


@pytest.fixture
def app_client(tmp_path, fake):
    # editor_auth_required=False keeps the test suite focused on route
    # behaviour; auth is covered separately in test_editor_auth.py.
    cfg = Config(
        rules_dir=tmp_path / "rules",
        state_dir=tmp_path / "state",
        editor_auth_required=False,
    )
    with TestClient(create_app(cfg, paperless_client=fake)) as c:
        yield c


# ── health ───────────────────────────────────────────────────────────


def test_health_paperless_ok(app_client):
    body = app_client.get("/api/health").json()
    assert body["paperless"]["ok"] is True
    assert body["app"]["name"] == "paperless-rules"


def test_health_paperless_down(tmp_path):
    cfg = Config(rules_dir=tmp_path, editor_auth_required=False)
    with TestClient(create_app(cfg, paperless_client=FakePaperless(healthy=False))) as c:
        assert c.get("/api/health").json()["paperless"]["ok"] is False


def test_health_no_client_configured(tmp_path):
    cfg = Config(rules_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/health").json()["paperless"]["ok"] is False


# ── documents proxy ──────────────────────────────────────────────────


def test_list_documents(app_client):
    assert app_client.get("/api/documents").json()["count"] == 2


def test_search_filters_results(app_client):
    body = app_client.get("/api/documents?query=Acme").json()
    assert body["count"] == 1 and body["results"][0]["id"] == 42


@pytest.mark.parametrize("url,status", [
    ("/api/documents?page=0", 422),
    ("/api/documents?page_size=999", 422),
])
def test_pagination_validation(app_client, url, status):
    assert app_client.get(url).status_code == status


def test_get_document_text(app_client):
    body = app_client.get("/api/documents/42/text").json()
    assert body["id"] == 42 and "Total à payer" in body["content"]


def test_get_document_text_missing(app_client):
    assert app_client.get("/api/documents/9999/text").status_code == 404


# ── rules CRUD ───────────────────────────────────────────────────────


def test_save_then_list_then_load(app_client):
    text = "match: Test\n"
    assert app_client.post("/api/rules", json={"filename": "01_test.yml", "yaml": text}).status_code == 200
    listing = app_client.get("/api/rules").json()
    assert listing["rules"][0] == {
        "filename": "01_test.yml", "name": "test",
        "match": "Test", "field_count": 0, "enabled": True,
    }
    assert app_client.get("/api/rules/01_test.yml").json()["yaml"] == text


def test_delete_idempotent(app_client):
    app_client.post("/api/rules", json={"filename": "r.yml", "yaml": "issuer: A\n"})
    assert app_client.delete("/api/rules/r.yml").json()["removed"] is True
    assert app_client.delete("/api/rules/r.yml").json()["removed"] is False  # idempotent


@pytest.mark.parametrize("body,status", [
    ({"filename": "../escape.yml", "yaml": "issuer: X\n"}, 400),
    ({"filename": "r.yml", "yaml": ":\n  : broken"}, 400),
])
def test_save_rejects(app_client, body, status):
    assert app_client.post("/api/rules", json=body).status_code == status


def test_load_missing_404(app_client):
    assert app_client.get("/api/rules/missing.yml").status_code == 404


# ── full rule test ───────────────────────────────────────────────────


def test_runs_rule_against_doc(app_client):
    rule_yaml = (
        "match: Acme\n"
        "fields:\n"
        "  amount:\n"
        "    regex: \"Total à payer\\\\s+EUR\\\\s+([\\\\d ,-]+)\"\n"
        "    type: float\n"
    )
    body = app_client.post("/api/test", json={"yaml": rule_yaml, "doc_ids": [42]}).json()
    f = body["results"][0]["extraction"]["fields"]["amount"]
    assert f["value"] == 1234.5
    assert body["results"][0]["extraction"]["required_ok"]


def test_invalid_yaml_returns_400(app_client):
    assert app_client.post("/api/test", json={"yaml": ":\n  : broken", "doc_ids": [42]}).status_code == 400


def test_missing_doc_reports_per_doc_error(app_client):
    r = app_client.post("/api/test", json={"yaml": "match: Acme\n", "doc_ids": [42, 9999]})
    results = r.json()["results"]
    assert len(results) == 2 and "error" in results[1]


# ── regex tester ─────────────────────────────────────────────────────


def test_regex_with_text(app_client):
    body = app_client.post("/api/regex/test", json={"pattern": r"\d+", "text": "abc 42 def 100"}).json()
    assert body["ok"] and body["results"][0]["match_count"] == 2


def test_regex_with_doc_ids(app_client):
    body = app_client.post(
        "/api/regex/test",
        json={"pattern": r"EUR\s+[\d ,-]+", "doc_ids": [42, 43]},
    ).json()
    by_id = {x["doc_id"]: x for x in body["results"]}
    assert by_id[42]["match_count"] >= 1
    assert by_id[43]["match_count"] == 0


def test_regex_coercion_preview(app_client):
    body = app_client.post("/api/regex/test", json={
        "pattern": r"Total à payer\s+EUR\s+([\d ,-]+)",
        "doc_ids": [42], "type": "float",
    }).json()
    m = body["results"][0]["matches"][0]
    assert m["coerced"] == 1234.5


def test_invalid_regex_returns_ok_false(app_client):
    # Editor calls this on every keystroke — must return 200 for half-typed
    # patterns, with ok=false so the UI shows an inline error.
    body = app_client.post("/api/regex/test", json={"pattern": "[unclosed", "text": "x"}).json()
    assert body["ok"] is False and body["error"] and body["results"] == []


def test_regex_requires_text_or_doc_ids(app_client):
    assert app_client.post("/api/regex/test", json={"pattern": "x"}).status_code == 400


def test_case_insensitive_flag(app_client):
    body = app_client.post(
        "/api/regex/test",
        json={"pattern": "ACME", "doc_ids": [42], "flags": "i"},
    ).json()
    assert body["results"][0]["match_count"] >= 1


# ── bootstrap ────────────────────────────────────────────────────────


def test_bootstrap_returns_suggestion(app_client):
    body = app_client.post("/api/bootstrap", json={"doc_id": 42}).json()
    assert set(body) == {"match", "exclude", "filename_suggestion", "language", "currency"}
    assert body["currency"] == "EUR" and body["language"] == "fr"
    assert body["match"]                                # non-empty seed (doctype hint)
    assert body["filename_suggestion"].endswith(".yml")


def test_bootstrap_missing_doc_404(app_client):
    assert app_client.post("/api/bootstrap", json={"doc_id": 9999}).status_code == 404


# ── static SPA ───────────────────────────────────────────────────────


def test_index_served_at_root(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "paperless·rules" in body
    # Mock-A-style layout: three resizable panes with section cards.
    assert 'id="workspace"' in body
    assert 'id="card-match"' in body
    assert 'id="card-fields"' in body


def test_api_routes_match_under_static_mount(app_client):
    # Guards against regression where StaticFiles mount intercepts /api/*.
    assert app_client.get("/api/health").status_code == 200
