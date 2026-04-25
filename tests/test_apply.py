"""Tests for the runtime apply logic.

Uses a richer FakePaperless that records writes (PATCHes, creates) so each
test can assert exactly what would have been sent over the wire — no Docker,
no httpx.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paperless_rules.paperless_client import PaperlessError
from paperless_rules.runtime.apply import (
    ResolutionCache,
    apply_rules_to_document,
)

FIXTURES = Path(__file__).parent / "fixtures"
SWISSCOM_TEXT = (FIXTURES / "swisscom_invoice.txt").read_text(encoding="utf-8")


# ── fake paperless that records writes ───────────────────────────────


class WriteRecordingPaperless:
    """Duck-typed PaperlessClient with read+write surface used by apply.py.

    Maintains in-memory tables for correspondents/document_types/tags/custom_fields
    keyed by integer IDs starting at 1, and records every PATCH so tests can
    assert on the exact payloads sent.
    """

    def __init__(self, docs: dict[int, dict[str, Any]] | None = None):
        self.docs = docs or {}
        self.correspondents: dict[int, dict[str, Any]] = {}
        self.document_types: dict[int, dict[str, Any]] = {}
        self.tags: dict[int, dict[str, Any]] = {}
        self.custom_fields: dict[int, dict[str, Any]] = {}
        self._next_id: dict[str, int] = {
            "correspondents": 1,
            "document_types": 1,
            "tags": 1,
            "custom_fields": 1,
        }
        self.patches: list[tuple[int, dict[str, Any]]] = []
        # Hook a test can flip to make custom_field create fail (simulates a
        # paperless permission / type-collision error).
        self.fail_create_kinds: set[str] = set()

    def _table(self, kind: str) -> dict[int, dict[str, Any]]:
        return getattr(self, kind)

    async def get_document(self, doc_id: int):
        if doc_id not in self.docs:
            raise PaperlessError(f"doc {doc_id} not found")
        return self.docs[doc_id]

    async def find_one_by_name(self, kind: str, name: str):
        for rec in self._table(kind).values():
            if rec.get("name", "").lower() == name.lower():
                return rec
        return None

    async def create(self, kind: str, payload: dict[str, Any]):
        if kind in self.fail_create_kinds:
            raise PaperlessError(f"simulated create failure for {kind}")
        new_id = self._next_id[kind]
        self._next_id[kind] += 1
        rec = {"id": new_id, **payload}
        self._table(kind)[new_id] = rec
        return rec

    async def list_custom_fields(self):
        return list(self.custom_fields.values())

    async def patch_document(self, doc_id: int, payload: dict[str, Any]):
        self.patches.append((doc_id, payload))
        # Apply the patch to the in-memory doc so subsequent lookups see it.
        self.docs.setdefault(doc_id, {"id": doc_id})
        self.docs[doc_id].update(payload)
        return self.docs[doc_id]

    async def aclose(self):
        pass


def _swisscom_doc(**overrides) -> dict[str, Any]:
    base = {
        "id": 42,
        "title": "Swisscom Mar 2024",
        "content": SWISSCOM_TEXT,
        "correspondent": None,
        "document_type": None,
        "tags": [],
        "custom_fields": [],
        "modified": "2024-03-15T10:00:00Z",
    }
    base.update(overrides)
    return base


def _full_swisscom_rule() -> dict[str, Any]:
    return {
        "issuer": "Swisscom (Suisse) SA",
        "document_type": "Invoice",
        "tags": ["telecom", "monthly"],
        "keywords": ["Swisscom", "Facture"],
        "fields": {
            "amount": {
                "regex": r"Total à payer\s+CHF\s+([\d'.,-]+)",
                "type": "float",
            },
            "invoice_number": {
                "regex": r"Numéro de facture\s+(\d+)",
                "type": "str",
            },
            "date": {
                "regex": r"Date d'émission\s+(\d{2}\.\d{2}\.\d{4})",
                "type": "date",
            },
        },
        "required_fields": ["amount", "date"],
        "options": {"currency": "CHF", "date_formats": ["%d.%m.%Y"]},
    }


# ── happy path ───────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_creates_correspondent_and_doctype_if_missing(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rules = [("01_swisscom.yml", _full_swisscom_rule())]
        result = await apply_rules_to_document(client, 42, rules)

        assert result.matched
        assert result.rule_filename == "01_swisscom.yml"
        assert result.error is None
        assert len(client.patches) == 1
        _, payload = client.patches[0]
        # IDs are 1 because they were freshly created.
        assert payload["correspondent"] == 1
        assert payload["document_type"] == 1
        assert sorted(payload["tags"]) == [1, 2]

    @pytest.mark.asyncio
    async def test_extracted_values_become_custom_fields(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rules = [("01_swisscom.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules)

        _, payload = client.patches[0]
        cf = {c["field"]: c["value"] for c in payload["custom_fields"]}
        # All three custom fields were created (ids 1..3) and assigned values.
        assert "CHF1234.50" in cf.values()
        assert "2024-03-15" in cf.values()
        assert "987654321" in cf.values()

    @pytest.mark.asyncio
    async def test_monetary_uses_rule_currency(self):
        rule = _full_swisscom_rule()
        rule["options"]["currency"] = "EUR"
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        await apply_rules_to_document(client, 42, [("01.yml", rule)])
        _, payload = client.patches[0]
        cf_values = [c["value"] for c in payload["custom_fields"]]
        assert any(v.startswith("EUR") for v in cf_values)


# ── idempotency ──────────────────────────────────────────────────────


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_second_run_makes_no_changes(self):
        # First run sets correspondent + tags + custom fields.
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rules = [("01.yml", _full_swisscom_rule())]
        cache = ResolutionCache()
        await apply_rules_to_document(client, 42, rules, cache=cache)
        # After the first run, the doc has all metadata. A second run finds
        # no diff and emits no PATCH (or an empty one — we want zero).
        before = len(client.patches)
        result2 = await apply_rules_to_document(client, 42, rules, cache=cache)
        assert result2.matched
        assert len(client.patches) == before  # no second PATCH
        assert result2.payload is None

    @pytest.mark.asyncio
    async def test_existing_correspondent_not_overwritten(self):
        # User manually set a different correspondent — rules must respect it.
        client = WriteRecordingPaperless({42: _swisscom_doc(correspondent=99)})
        # Pre-seed a correspondent with id 99 so the lookup makes sense.
        client.correspondents[99] = {"id": 99, "name": "Manual Override"}
        rules = [("01.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules)
        _, payload = client.patches[0]
        assert "correspondent" not in payload  # untouched
        # But tags + custom fields still apply.
        assert "tags" in payload or "custom_fields" in payload

    @pytest.mark.asyncio
    async def test_overwrite_flag_replaces_correspondent(self):
        client = WriteRecordingPaperless({42: _swisscom_doc(correspondent=99)})
        client.correspondents[99] = {"id": 99, "name": "Manual Override"}
        rules = [("01.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules, overwrite_existing=True)
        _, payload = client.patches[0]
        assert "correspondent" in payload  # was overwritten


# ── tags additive ────────────────────────────────────────────────────


class TestTagsAdditive:
    @pytest.mark.asyncio
    async def test_existing_tags_preserved(self):
        # Doc has manual tag id=99; rule adds "telecom" and "monthly".
        client = WriteRecordingPaperless({42: _swisscom_doc(tags=[99])})
        client.tags[99] = {"id": 99, "name": "manual-tag"}
        rules = [("01.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules)
        _, payload = client.patches[0]
        assert 99 in payload["tags"]  # manual tag survived
        assert len(payload["tags"]) == 3

    @pytest.mark.asyncio
    async def test_no_tag_change_when_already_a_superset(self):
        # All rule tags already on the doc → no tags entry in the payload
        # (we check sorted-equality, not identity).
        client = WriteRecordingPaperless({42: _swisscom_doc(tags=[1, 2, 99])})
        client.tags[1] = {"id": 1, "name": "telecom"}
        client.tags[2] = {"id": 2, "name": "monthly"}
        client.tags[99] = {"id": 99, "name": "manual"}
        rules = [("01.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules)
        # Either no patch at all, or patch with no `tags` key.
        if client.patches:
            _, payload = client.patches[0]
            assert "tags" not in payload


# ── no-match path ────────────────────────────────────────────────────


class TestNoMatch:
    @pytest.mark.asyncio
    async def test_no_rule_matches_no_patch(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rule = _full_swisscom_rule()
        rule["keywords"] = ["DefinitelyNotInTheDocument_XYZ"]
        result = await apply_rules_to_document(client, 42, [("01.yml", rule)])
        assert not result.matched
        assert client.patches == []
        assert result.payload is None
        assert result.error is None  # explicitly: no error, no flag, just nothing

    @pytest.mark.asyncio
    async def test_doc_fetch_error_returns_error(self):
        client = WriteRecordingPaperless({})  # doc 42 does not exist
        rules = [("01.yml", _full_swisscom_rule())]
        result = await apply_rules_to_document(client, 42, rules)
        assert result.error is not None
        assert client.patches == []


# ── error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_custom_field_creation_failure_skips_field(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        client.fail_create_kinds = {"custom_fields"}
        rules = [("01.yml", _full_swisscom_rule())]
        result = await apply_rules_to_document(client, 42, rules)
        # The runtime kept going — correspondent / type / tags still got PATCHed.
        assert result.matched
        assert "amount" in result.skipped_fields
        assert "date" in result.skipped_fields
        assert "invoice_number" in result.skipped_fields
        # custom_fields key absent from payload because all writes were skipped.
        _, payload = client.patches[0]
        assert "custom_fields" not in payload
        # But correspondent/tags/document_type still present.
        assert "correspondent" in payload

    @pytest.mark.asyncio
    async def test_correspondent_creation_failure_silently_omits(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        client.fail_create_kinds = {"correspondents"}
        rules = [("01.yml", _full_swisscom_rule())]
        result = await apply_rules_to_document(client, 42, rules)
        # Runtime continued past the failed correspondent create.
        assert result.matched
        _, payload = client.patches[0]
        assert "correspondent" not in payload
        # Other writes proceeded.
        assert "document_type" in payload


# ── dry run ──────────────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_patch(self):
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rules = [("01.yml", _full_swisscom_rule())]
        result = await apply_rules_to_document(client, 42, rules, dry_run=True)
        assert result.dry_run
        assert result.payload is not None
        assert client.patches == []  # nothing went over the wire

    @pytest.mark.asyncio
    async def test_dry_run_still_creates_lookup_records(self):
        # Trade-off: we DO create correspondents/tags/etc. during a dry run
        # because we need their IDs to build the would-be PATCH payload.
        # This pins that behaviour so nobody is surprised by it later.
        client = WriteRecordingPaperless({42: _swisscom_doc()})
        rules = [("01.yml", _full_swisscom_rule())]
        await apply_rules_to_document(client, 42, rules, dry_run=True)
        assert len(client.correspondents) == 1
        assert len(client.tags) == 2


# ── cache reuse across docs ──────────────────────────────────────────


class TestCache:
    @pytest.mark.asyncio
    async def test_resolution_cache_reused_across_calls(self):
        docs = {
            42: _swisscom_doc(),
            43: _swisscom_doc(id=43, title="Swisscom Apr"),
        }
        client = WriteRecordingPaperless(docs)
        rules = [("01.yml", _full_swisscom_rule())]
        cache = ResolutionCache()
        await apply_rules_to_document(client, 42, rules, cache=cache)
        await apply_rules_to_document(client, 43, rules, cache=cache)
        # Only one correspondent was created — second doc reused the cache.
        assert len(client.correspondents) == 1
        # Tag table same: 2 entries, not 4.
        assert len(client.tags) == 2
