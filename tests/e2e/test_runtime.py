"""End-to-end runtime tests against a real paperless-ngx.

Each test:
  1. Writes a YAML rule into the per-test rules dir.
  2. Picks a seeded document (Swisscom invoice fixture) by title.
  3. Runs `apply_rules_to_document` against the live paperless API.
  4. Re-fetches the doc and asserts the metadata landed (correspondent,
     document_type, tags, custom_fields).

These tests exercise the same code path as `paperless-rules apply <id>`
and `paperless-rules post-consume`, just driven from Python.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from paperless_rules.engine import load_rules
from paperless_rules.runtime.apply import apply_rules_to_document

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _swisscom_rule_yaml() -> str:
    return (
        "issuer: Swisscom (Suisse) SA\n"
        "document_type: Invoice\n"
        "tags: [telecom, monthly]\n"
        "keywords: [Swisscom, Facture]\n"
        "fields:\n"
        "  amount:\n"
        '    regex: "Total à payer\\\\s+CHF\\\\s+([\\\\d\'.,-]+)"\n'
        "    type: float\n"
        "  invoice_number:\n"
        '    regex: "Numéro de facture\\\\s+(\\\\d+)"\n'
        "    type: str\n"
        "  date:\n"
        '    regex: "Date d.émission\\\\s+(\\\\d{2}\\\\.\\\\d{2}\\\\.\\\\d{4})"\n'
        "    type: date\n"
        "required_fields: [amount, date]\n"
        "options:\n"
        "  currency: CHF\n"
        "  date_formats: ['%d.%m.%Y']\n"
    )


def _doc_id_by_title(seeded_doc_ids, admin_token, title_substring: str) -> int:
    """Look up a seeded doc by a substring of its title."""
    from tests.e2e.conftest import PAPERLESS_URL

    headers = {"Authorization": f"Token {admin_token}"}
    r = httpx.get(
        f"{PAPERLESS_URL}/api/documents/?page_size=100",
        headers=headers, timeout=10.0,
    )
    r.raise_for_status()
    for d in r.json()["results"]:
        if title_substring.lower() in (d.get("title") or "").lower():
            return d["id"]
    raise AssertionError(
        f"no seeded doc with title containing {title_substring!r} "
        f"(seen: {[d.get('title') for d in r.json()['results']]})"
    )


# ── apply: write metadata to a real paperless ────────────────────────


class TestApplyHappyPath:
    async def test_extracts_and_writes_metadata(
        self, fresh_rules_dir, paperless_client_factory, seeded_doc_ids, admin_token
    ):
        (fresh_rules_dir / "01_swisscom.yml").write_text(_swisscom_rule_yaml())
        rules = load_rules(fresh_rules_dir)
        assert len(rules) == 1

        doc_id = _doc_id_by_title(seeded_doc_ids, admin_token, "swisscom")
        client = paperless_client_factory()
        try:
            result = await apply_rules_to_document(client, doc_id, rules)
        finally:
            await client.aclose()

        assert result.matched
        assert result.error is None
        assert result.payload is not None
        assert "correspondent" in result.payload
        assert "tags" in result.payload
        assert "custom_fields" in result.payload

        # Re-fetch the doc and verify metadata actually landed.
        from tests.e2e.conftest import PAPERLESS_URL
        headers = {"Authorization": f"Token {admin_token}"}
        r = httpx.get(
            f"{PAPERLESS_URL}/api/documents/{doc_id}/",
            headers=headers, timeout=10.0,
        )
        r.raise_for_status()
        doc = r.json()
        assert doc["correspondent"] is not None
        assert len(doc["tags"]) >= 2
        # Custom fields should include the three extracted values.
        cf_values = [c.get("value") for c in (doc.get("custom_fields") or [])]
        assert any("CHF" in str(v) for v in cf_values)
        assert any(str(v).startswith("2024-") for v in cf_values)


# ── idempotency ──────────────────────────────────────────────────────


class TestIdempotency:
    async def test_second_run_makes_no_changes(
        self, fresh_rules_dir, paperless_client_factory, seeded_doc_ids, admin_token
    ):
        (fresh_rules_dir / "01_swisscom.yml").write_text(_swisscom_rule_yaml())
        rules = load_rules(fresh_rules_dir)
        doc_id = _doc_id_by_title(seeded_doc_ids, admin_token, "swisscom")

        client = paperless_client_factory()
        try:
            r1 = await apply_rules_to_document(client, doc_id, rules)
            r2 = await apply_rules_to_document(client, doc_id, rules)
        finally:
            await client.aclose()

        assert r1.matched and r2.matched
        # Second run produced no payload (engine + merge logic detected the
        # existing metadata is already what the rule would write).
        assert r2.payload is None or r2.payload == {}


# ── no-match path ────────────────────────────────────────────────────


class TestNoMatch:
    async def test_unmatched_doc_left_untouched(
        self, fresh_rules_dir, paperless_client_factory, seeded_doc_ids, admin_token
    ):
        # Rule won't match anything in the corpus.
        (fresh_rules_dir / "01_no_match.yml").write_text(
            "issuer: Phantom Co\n"
            "keywords: [DefinitelyNotInTheFixtures_XYZ_marker]\n"
        )
        rules = load_rules(fresh_rules_dir)
        doc_id = _doc_id_by_title(seeded_doc_ids, admin_token, "swisscom")

        # Snapshot the doc before — runtime must leave it identical.
        from tests.e2e.conftest import PAPERLESS_URL
        headers = {"Authorization": f"Token {admin_token}"}
        before = httpx.get(
            f"{PAPERLESS_URL}/api/documents/{doc_id}/",
            headers=headers, timeout=10.0,
        ).json()

        client = paperless_client_factory()
        try:
            result = await apply_rules_to_document(client, doc_id, rules)
        finally:
            await client.aclose()

        assert not result.matched
        assert result.payload is None

        after = httpx.get(
            f"{PAPERLESS_URL}/api/documents/{doc_id}/",
            headers=headers, timeout=10.0,
        ).json()
        # Tags / correspondent / document_type unchanged.
        assert before.get("correspondent") == after.get("correspondent")
        assert sorted(before.get("tags") or []) == sorted(after.get("tags") or [])


# ── backfill via iter_documents ──────────────────────────────────────


class TestBackfill:
    async def test_backfill_processes_all_matching(
        self, fresh_rules_dir, paperless_client_factory, seeded_doc_ids
    ):
        (fresh_rules_dir / "01_swisscom.yml").write_text(_swisscom_rule_yaml())
        rules = load_rules(fresh_rules_dir)

        client = paperless_client_factory()
        matched = 0
        try:
            async for doc in client.iter_documents():
                result = await apply_rules_to_document(client, doc["id"], rules)
                if result.matched:
                    matched += 1
        finally:
            await client.aclose()

        # The Swisscom fixture is the only one currently matching this rule;
        # other fixtures that don't contain "Swisscom" or "Facture" are skipped.
        assert matched >= 1
