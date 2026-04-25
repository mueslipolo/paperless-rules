"""End-to-end tests of the FastAPI editor backend with a fake paperless.

We bypass the real httpx network path entirely by injecting a duck-typed
fake client into `create_app`. The fake serves a small fixed corpus that
includes the Swisscom OCR fixture, so the engine and bootstrap endpoints
exercise the realistic Swiss-document path. No Docker required.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paperless_rules.config import Config
from paperless_rules.editor.app import create_app
from paperless_rules.paperless_client import PaperlessError

FIXTURES = Path(__file__).parent / "fixtures"
SWISSCOM_TEXT = (FIXTURES / "swisscom_invoice.txt").read_text(encoding="utf-8")


class FakePaperless:
    """Minimal stand-in for PaperlessClient. Duck-typed."""

    def __init__(self, docs: dict[int, dict] | None = None, healthy: bool = True):
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
                d
                for d in results
                if q in (d.get("title", "") or "").lower()
                or q in (d.get("content", "") or "").lower()
            ]
        start = (page - 1) * page_size
        return {
            "count": len(results),
            "next": None,
            "previous": None,
            "results": results[start : start + page_size],
        }

    async def get_document(self, doc_id: int):
        if doc_id not in self.docs:
            raise PaperlessError(f"document {doc_id} not found")
        return self.docs[doc_id]

    async def aclose(self):
        pass


@pytest.fixture
def fake_paperless():
    return FakePaperless(
        docs={
            42: {
                "id": 42,
                "title": "Swisscom Mar 2024",
                "content": SWISSCOM_TEXT,
                "created": "2024-03-15T00:00:00Z",
                "correspondent": None,
                "document_type": None,
                "tags": [],
            },
            43: {
                "id": 43,
                "title": "Empty doc",
                "content": "",
                "created": "2024-04-01T00:00:00Z",
            },
        }
    )


@pytest.fixture
def app_client(tmp_path, fake_paperless):
    cfg = Config(rules_dir=tmp_path / "rules", state_dir=tmp_path / "state")
    app = create_app(cfg, paperless_client=fake_paperless)
    with TestClient(app) as client:
        yield client


# ── health ────────────────────────────────────────────────────────────


class TestHealth:
    def test_reports_paperless_ok(self, app_client):
        r = app_client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["paperless"]["ok"] is True
        assert body["app"]["name"] == "paperless-rules"

    def test_reports_paperless_down(self, tmp_path):
        cfg = Config(rules_dir=tmp_path)
        app = create_app(cfg, paperless_client=FakePaperless(healthy=False))
        with TestClient(app) as c:
            assert c.get("/api/health").json()["paperless"]["ok"] is False

    def test_no_client_configured(self, tmp_path):
        # No paperless_url, no injected client → unconfigured state.
        cfg = Config(rules_dir=tmp_path)
        app = create_app(cfg)
        with TestClient(app) as c:
            body = c.get("/api/health").json()
            assert body["paperless"]["ok"] is False


# ── documents proxy ───────────────────────────────────────────────────


class TestDocumentsProxy:
    def test_list_documents(self, app_client):
        r = app_client.get("/api/documents")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_search_filters_results(self, app_client):
        r = app_client.get("/api/documents?query=Swisscom")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["results"][0]["id"] == 42

    def test_pagination_validation(self, app_client):
        # page < 1 rejected
        assert app_client.get("/api/documents?page=0").status_code == 422
        # page_size > 100 rejected
        assert app_client.get("/api/documents?page_size=999").status_code == 422

    def test_get_document_text(self, app_client):
        r = app_client.get("/api/documents/42/text")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 42
        assert "Swisscom" in body["content"]
        assert "Total à payer" in body["content"]

    def test_get_document_text_missing(self, app_client):
        r = app_client.get("/api/documents/9999/text")
        assert r.status_code == 404


# ── rules CRUD ────────────────────────────────────────────────────────


class TestRulesCRUD:
    def test_save_then_list_then_load(self, app_client):
        text = "issuer: Test\nkeywords: [Test]\n"
        save = app_client.post("/api/rules", json={"filename": "01_test.yml", "yaml": text})
        assert save.status_code == 200

        listing = app_client.get("/api/rules").json()
        assert listing["rules"][0]["filename"] == "01_test.yml"
        assert listing["rules"][0]["issuer"] == "Test"

        load = app_client.get("/api/rules/01_test.yml").json()
        assert load["yaml"] == text

    def test_delete(self, app_client):
        app_client.post("/api/rules", json={"filename": "r.yml", "yaml": "issuer: A\n"})
        r = app_client.delete("/api/rules/r.yml")
        assert r.status_code == 200
        assert r.json()["removed"] is True
        # Idempotent: deleting again returns ok with removed=False.
        r2 = app_client.delete("/api/rules/r.yml")
        assert r2.status_code == 200
        assert r2.json()["removed"] is False

    def test_path_traversal_in_filename_rejected(self, app_client):
        r = app_client.post(
            "/api/rules", json={"filename": "../escape.yml", "yaml": "issuer: X\n"}
        )
        assert r.status_code == 400

    def test_invalid_yaml_rejected(self, app_client):
        r = app_client.post(
            "/api/rules", json={"filename": "r.yml", "yaml": ":\n  : broken"}
        )
        assert r.status_code == 400

    def test_load_missing_returns_404(self, app_client):
        assert app_client.get("/api/rules/missing.yml").status_code == 404


# ── engine: full rule test ────────────────────────────────────────────


class TestEngineEndpoint:
    def test_runs_rule_against_doc(self, app_client):
        rule_yaml = (
            "issuer: Swisscom (Suisse) SA\n"
            "keywords: [Swisscom, Facture]\n"
            "fields:\n"
            "  amount:\n"
            "    regex: \"Total à payer\\\\s+CHF\\\\s+([\\\\d'.,-]+)\"\n"
            "    type: float\n"
        )
        r = app_client.post("/api/test", json={"yaml": rule_yaml, "doc_ids": [42]})
        assert r.status_code == 200
        body = r.json()
        result = body["results"][0]
        assert result["doc_id"] == 42
        assert result["extraction"]["fields"]["amount"]["value"] == 1234.5
        assert result["extraction"]["required_ok"] is True

    def test_invalid_yaml_returns_400(self, app_client):
        r = app_client.post("/api/test", json={"yaml": ":\n  : broken", "doc_ids": [42]})
        assert r.status_code == 400

    def test_missing_doc_reports_per_doc_error(self, app_client):
        r = app_client.post(
            "/api/test",
            json={"yaml": "keywords: [Swisscom]\n", "doc_ids": [42, 9999]},
        )
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 2
        assert "error" in results[1]


# ── regex tester ──────────────────────────────────────────────────────


class TestRegexTester:
    def test_with_text_only(self, app_client):
        r = app_client.post(
            "/api/regex/test",
            json={"pattern": r"\d+", "text": "abc 42 def 100"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"]
        assert body["results"][0]["match_count"] == 2

    def test_with_doc_ids_returns_per_doc_results(self, app_client):
        r = app_client.post(
            "/api/regex/test",
            json={"pattern": r"CHF\s+[\d'.,-]+", "doc_ids": [42, 43]},
        )
        assert r.status_code == 200
        body = r.json()
        results = {x["doc_id"]: x for x in body["results"]}
        assert results[42]["match_count"] >= 1
        assert results[43]["match_count"] == 0

    def test_coercion_preview(self, app_client):
        # The coerced value flows through the same path as the engine, so
        # the editor's preview matches what the runtime will write.
        r = app_client.post(
            "/api/regex/test",
            json={
                "pattern": r"Total à payer\s+CHF\s+([\d'.,-]+)",
                "doc_ids": [42],
                "type": "float",
            },
        )
        body = r.json()
        match = body["results"][0]["matches"][0]
        assert match["coerced"] == 1234.5
        assert match["groups"] == ["1'234.50"]

    def test_invalid_regex_returns_ok_false(self, app_client):
        # The editor calls this on every keystroke — must NOT 5xx on a
        # half-typed pattern. Returns ok=False so the UI shows an inline
        # error indicator instead of crashing.
        r = app_client.post("/api/regex/test", json={"pattern": "[unclosed", "text": "x"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error"]
        assert body["results"] == []

    def test_requires_text_or_doc_ids(self, app_client):
        r = app_client.post("/api/regex/test", json={"pattern": "x"})
        assert r.status_code == 400

    def test_flags(self, app_client):
        # Default has 'm' on. Adding 'i' makes the match case-insensitive.
        r = app_client.post(
            "/api/regex/test",
            json={"pattern": "SWISSCOM", "doc_ids": [42], "flags": "i"},
        )
        assert r.json()["results"][0]["match_count"] >= 1


# ── bootstrap ─────────────────────────────────────────────────────────


class TestBootstrapEndpoint:
    def test_returns_suggestion_struct(self, app_client):
        r = app_client.post("/api/bootstrap", json={"doc_id": 42})
        assert r.status_code == 200
        body = r.json()
        assert "Swisscom" in body["issuer"]
        assert body["currency"] == "CHF"
        assert body["language"] == "fr"
        assert any(f["name"] == "amount" for f in body["fields"])

    def test_missing_doc_returns_404(self, app_client):
        assert app_client.post("/api/bootstrap", json={"doc_id": 9999}).status_code == 404


class TestStaticSPA:
    """The editor SPA is served from `/`. Smoke-check that index.html is
    reachable and contains the expected layout markers — catches breakage
    of the static-mount registration order."""

    def test_index_served_at_root(self, app_client):
        r = app_client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        body = r.text
        assert "paperless·rules" in body
        # Three structural elements proving the regex-first layout shipped.
        assert 'id="pane-corpus"' in body
        assert 'id="regex-tester"' in body
        assert 'id="bootstrap-modal"' in body

    def test_api_routes_still_match_under_static_mount(self, app_client):
        # Static mount is at /, but more specific /api/* routes must be
        # checked first — this test guards against regression of route
        # registration order in create_app().
        assert app_client.get("/api/health").status_code == 200
