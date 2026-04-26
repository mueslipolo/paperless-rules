"""Runtime apply.py tests with a fake paperless that records writes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from paperless_rules.paperless_client import PaperlessError
from paperless_rules.runtime.apply import (
    ResolutionCache,
    apply_rules_to_document,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACME = (FIXTURES / "acme_invoice.txt").read_text(encoding="utf-8")


class WriteRecordingPaperless:
    """PaperlessClient stand-in. Records every PATCH so tests can assert payloads."""

    def __init__(self, docs=None):
        self.docs = docs or {}
        self.correspondents: dict[int, dict[str, Any]] = {}
        self.document_types: dict[int, dict[str, Any]] = {}
        self.tags: dict[int, dict[str, Any]] = {}
        self.custom_fields: dict[int, dict[str, Any]] = {}
        self._next_id = {"correspondents": 1, "document_types": 1, "tags": 1, "custom_fields": 1}
        self.patches: list[tuple[int, dict[str, Any]]] = []
        self.fail_create_kinds: set[str] = set()

    def _table(self, kind):
        return getattr(self, kind)

    async def get_document(self, doc_id):
        if doc_id not in self.docs:
            raise PaperlessError(f"doc {doc_id} not found")
        return self.docs[doc_id]

    async def find_one_by_name(self, kind, name):
        for rec in self._table(kind).values():
            if rec.get("name", "").lower() == name.lower():
                return rec
        return None

    async def create(self, kind, payload):
        if kind in self.fail_create_kinds:
            raise PaperlessError(f"simulated {kind} create failure")
        new_id = self._next_id[kind]
        self._next_id[kind] += 1
        rec = {"id": new_id, **payload}
        self._table(kind)[new_id] = rec
        return rec

    async def list_custom_fields(self):
        return list(self.custom_fields.values())

    async def patch_document(self, doc_id, payload):
        self.patches.append((doc_id, payload))
        self.docs.setdefault(doc_id, {"id": doc_id}).update(payload)
        return self.docs[doc_id]

    async def aclose(self):
        pass


def _doc(**overrides):
    base = {
        "id": 42, "title": "", "content": ACME,
        "correspondent": None, "document_type": None,
        "tags": [], "custom_fields": [],
    }
    base.update(overrides)
    return base


def _rule():
    """Realistic rule using the new unified schema. Reserved field names
    (correspondent / document_type / tags / title) hit paperless built-ins;
    `amount`, `date`, `invoice_number` become custom fields."""
    return {
        "match": "Acme",
        "exclude": "",
        "fields": {
            "amount":         {"regex": r"Total à payer\s+EUR\s+([\d ,-]+)", "type": "float"},
            "date":           {"regex": r"Date d'émission\s+(\d{2}\.\d{2}\.\d{4})", "type": "date"},
            "invoice_number": {"regex": r"Numéro de facture\s+(\d+)", "type": "str"},
            "correspondent":  {"value": "Acme Télécom (Europe) SARL"},
            "document_type":  {"value": "Invoice"},
            "tags":           {"value": ["telecom", "monthly"]},
            "title":          {"template": "{date} Acme #{invoice_number} EUR{amount}"},
        },
        "required": ["amount", "date"],
        "options": {"currency": "EUR", "date_formats": ["%d.%m.%Y"]},
    }


# ── happy path ───────────────────────────────────────────────────────


async def test_creates_correspondent_doctype_tags():
    client = WriteRecordingPaperless({42: _doc()})
    result = await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    assert result.matched and result.error is None
    _, payload = client.patches[0]
    assert payload["correspondent"] == 1
    assert payload["document_type"] == 1
    assert sorted(payload["tags"]) == [1, 2]


async def test_title_template_rendered():
    client = WriteRecordingPaperless({42: _doc()})
    await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    _, payload = client.patches[0]
    assert payload["title"] == "2024-03-15 Acme #987654321 EUR1234.5"


async def test_extracted_values_become_custom_fields():
    client = WriteRecordingPaperless({42: _doc()})
    await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    cf_values = {c["value"] for c in client.patches[0][1]["custom_fields"]}
    assert "EUR1234.50" in cf_values
    assert "2024-03-15" in cf_values
    assert "987654321" in cf_values


async def test_internal_field_skipped_from_custom_fields():
    rule = _rule()
    rule["fields"]["raw_id"] = {"regex": r"client\s+(\d+)", "internal": True}
    client = WriteRecordingPaperless({42: _doc()})
    await apply_rules_to_document(client, 42, [("01.yml", rule)])
    cf_names_keys = [c["field"] for c in client.patches[0][1]["custom_fields"]]
    # The custom_fields list keys by ID. Look at created custom_fields by name:
    created_names = {cf["name"] for cf in client.custom_fields.values()}
    assert "raw_id" not in created_names  # internal → not published


async def test_monetary_uses_rule_currency():
    rule = _rule()
    rule["options"]["currency"] = "USD"
    client = WriteRecordingPaperless({42: _doc()})
    await apply_rules_to_document(client, 42, [("01.yml", rule)])
    cf_values = [c["value"] for c in client.patches[0][1]["custom_fields"]]
    assert any(v.startswith("USD") for v in cf_values)


# ── idempotency / overwrite semantics ────────────────────────────────


async def test_second_run_makes_no_changes():
    client = WriteRecordingPaperless({42: _doc()})
    cache = ResolutionCache()
    await apply_rules_to_document(client, 42, [("01.yml", _rule())], cache=cache)
    before = len(client.patches)
    r2 = await apply_rules_to_document(client, 42, [("01.yml", _rule())], cache=cache)
    assert r2.matched
    assert len(client.patches) == before
    assert r2.payload is None


async def test_existing_correspondent_not_overwritten():
    client = WriteRecordingPaperless({42: _doc(correspondent=99)})
    client.correspondents[99] = {"id": 99, "name": "Manual Override"}
    await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    _, payload = client.patches[0]
    assert "correspondent" not in payload


async def test_existing_title_not_overwritten():
    client = WriteRecordingPaperless({42: _doc(title="Manually edited title")})
    await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    _, payload = client.patches[0]
    assert "title" not in payload


async def test_overwrite_flag_replaces_correspondent():
    client = WriteRecordingPaperless({42: _doc(correspondent=99)})
    client.correspondents[99] = {"id": 99, "name": "Manual Override"}
    await apply_rules_to_document(client, 42, [("01.yml", _rule())], overwrite_existing=True)
    assert "correspondent" in client.patches[0][1]


# ── tags additive ────────────────────────────────────────────────────


async def test_existing_tags_preserved():
    client = WriteRecordingPaperless({42: _doc(tags=[99])})
    client.tags[99] = {"id": 99, "name": "manual-tag"}
    await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    payload = client.patches[0][1]
    assert 99 in payload["tags"] and len(payload["tags"]) == 3


# ── no-match / error paths ───────────────────────────────────────────


async def test_no_rule_matches_no_patch():
    client = WriteRecordingPaperless({42: _doc()})
    rule = _rule()
    rule["match"] = "DefinitelyNotInDocument_XYZ"
    result = await apply_rules_to_document(client, 42, [("01.yml", rule)])
    assert not result.matched
    assert client.patches == []
    assert result.error is None


async def test_doc_fetch_error_returns_error():
    client = WriteRecordingPaperless({})
    result = await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    assert result.error is not None
    assert client.patches == []


async def test_custom_field_creation_failure_skips_field():
    client = WriteRecordingPaperless({42: _doc()})
    client.fail_create_kinds = {"custom_fields"}
    result = await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    assert result.matched
    assert {"amount", "date", "invoice_number"} <= set(result.skipped_fields)
    payload = client.patches[0][1]
    assert "custom_fields" not in payload  # all skipped
    assert "correspondent" in payload      # built-ins still went through


async def test_correspondent_creation_failure_silently_omits():
    client = WriteRecordingPaperless({42: _doc()})
    client.fail_create_kinds = {"correspondents"}
    result = await apply_rules_to_document(client, 42, [("01.yml", _rule())])
    assert result.matched
    payload = client.patches[0][1]
    assert "correspondent" not in payload
    assert "document_type" in payload


# ── dry run ──────────────────────────────────────────────────────────


async def test_dry_run_does_not_patch():
    client = WriteRecordingPaperless({42: _doc()})
    result = await apply_rules_to_document(client, 42, [("01.yml", _rule())], dry_run=True)
    assert result.dry_run and result.payload is not None
    assert client.patches == []


# ── cache ────────────────────────────────────────────────────────────


async def test_resolution_cache_reused_across_docs():
    docs = {42: _doc(), 43: _doc(id=43, title="Acme Apr")}
    client = WriteRecordingPaperless(docs)
    cache = ResolutionCache()
    await apply_rules_to_document(client, 42, [("01.yml", _rule())], cache=cache)
    await apply_rules_to_document(client, 43, [("01.yml", _rule())], cache=cache)
    assert len(client.correspondents) == 1
    assert len(client.tags) == 2
