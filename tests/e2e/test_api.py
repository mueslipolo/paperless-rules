"""End-to-end editor API tests against a real paperless-ngx.

These run the FastAPI app in-process (TestClient) but point its
PaperlessClient at the live paperless container — so the API → paperless
HTTP path is exercised for real, while the FastAPI app itself stays an
in-memory ASGI mount (no need to also containerize paperless-rules).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from paperless_rules.config import Config
from paperless_rules.editor.app import create_app
from paperless_rules.paperless_client import PaperlessClient


@pytest.fixture
def app_client(tmp_path, admin_token, seeded_doc_ids):
    """Editor app wired to the live test paperless via PaperlessClient."""
    from tests.e2e.conftest import PAPERLESS_URL

    cfg = Config(
        paperless_url=PAPERLESS_URL,
        paperless_token=admin_token,
        rules_dir=tmp_path / "rules",
        state_dir=tmp_path / "state",
    )
    client = PaperlessClient(PAPERLESS_URL, admin_token)
    app = create_app(cfg, paperless_client=client)
    with TestClient(app) as c:
        yield c


class TestEditorAgainstRealPaperless:
    def test_health_reports_paperless_connected(self, app_client):
        body = app_client.get("/api/health").json()
        assert body["paperless"]["ok"] is True

    def test_documents_list_paginates(self, app_client, seeded_doc_ids):
        body = app_client.get("/api/documents").json()
        assert body["count"] == len(seeded_doc_ids)

    def test_document_text_returns_real_ocr_content(
        self, app_client, seeded_doc_ids
    ):
        # The Acme fixture is one of the seeded docs; pick the first
        # one whose OCR contains the recognisable marker.
        for doc_id in seeded_doc_ids:
            r = app_client.get(f"/api/documents/{doc_id}/text")
            assert r.status_code == 200
            content = r.json()["content"]
            if "Total à payer" in content:
                # Confirm byte-for-byte ingestion: plain-.txt consume keeps
                # the file content unchanged.
                assert "1 234,50" in content
                return
        pytest.fail("did not find the Acme fixture among seeded docs")

    def test_regex_test_with_doc_ids_returns_per_doc_matches(
        self, app_client, seeded_doc_ids
    ):
        r = app_client.post(
            "/api/regex/test",
            json={
                "pattern": r"EUR\s+([\d ,-]+)",
                "doc_ids": seeded_doc_ids,
                "type": "float",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"]
        # At least one doc has an EUR amount (the Acme fixture).
        assert any(x["match_count"] > 0 for x in body["results"])


    def test_full_rule_test_extracts_amount(
        self, app_client, seeded_doc_ids
    ):
        # Find the Acme doc
        for doc_id in seeded_doc_ids:
            text = app_client.get(f"/api/documents/{doc_id}/text").json()["content"]
            if "Acme" in text:
                break
        else:
            pytest.fail("no Acme doc seeded")

        rule_yaml = (
            "keywords: [Acme, Facture]\n"
            "fields:\n"
            "  amount:\n"
            '    regex: "Total à payer\\\\s+EUR\\\\s+([\\\\d ,-]+)"\n'
            "    type: float\n"
        )
        r = app_client.post(
            "/api/test", json={"yaml": rule_yaml, "doc_ids": [doc_id]}
        )
        assert r.status_code == 200
        result = r.json()["results"][0]
        assert result["extraction"]["fields"]["amount"]["value"] == 1234.5
