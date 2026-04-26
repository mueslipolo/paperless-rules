"""Bootstrap unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from paperless_rules.bootstrap import bootstrap_from_text, render_yaml
from paperless_rules.engine import extract_with_rule

FIXTURES = Path(__file__).parent / "fixtures"
ACME = (FIXTURES / "acme_invoice.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sug():
    return bootstrap_from_text(ACME)


# ── issuer / language / currency ─────────────────────────────────────


def test_acme_issuer_detected(sug):
    assert "Acme" in sug["issuer"] and "SARL" in sug["issuer"]


def test_skips_generic_header():
    text = "INVOICE\nGlobex Industries Inc\n123 Main Street\n10001 New York\n"
    assert "Inc" in bootstrap_from_text(text)["issuer"]


def test_no_company_falls_back_to_longest():
    assert bootstrap_from_text("Hello\nThis is a longer line\nshort\n")["issuer"] != ""


def test_empty_text():
    assert bootstrap_from_text("")["issuer"] == ""


@pytest.mark.parametrize("text,expected", [
    (ACME, "fr"),
    ("Rechnung der Globex GmbH\nDie Rechnung ist fällig am 30.04.2024.\n", "de"),
    ("The invoice is due on 2024-04-30. Please pay by the due date.\n", "en"),
])
def test_language_detection(text, expected):
    assert bootstrap_from_text(text)["language"] == expected


@pytest.mark.parametrize("text,expected", [
    (ACME, "EUR"),
    ("Total: USD 100.00\n", "USD"),
    ("No currency code in this text.\n", "EUR"),
])
def test_currency_detection(text, expected):
    assert bootstrap_from_text(text)["currency"] == expected


# ── match regex ──────────────────────────────────────────────────────


def test_match_combines_issuer_and_doctype(sug):
    # Acme + Facture in the fixture → "Acme.*?Facture"
    assert sug["match"] == "Acme.*?Facture"


def test_match_falls_back_to_issuer_alone():
    # No doctype hint in the text → match is just the issuer's first word.
    text = "Globex Inc\nSome unrelated correspondence here.\n"
    assert bootstrap_from_text(text)["match"] == "Globex"


def test_match_falls_back_to_doctype_alone():
    # Issuer has no usable word → match is just the doctype hint.
    text = "AG\nFacture\n"  # "AG" is a suffix word, gets filtered out
    sug = bootstrap_from_text(text)
    assert "Facture" in sug["match"]


def test_match_actually_fires_against_source(sug):
    # The bootstrap regex must match the document it was generated from —
    # otherwise the rule is broken on day one.
    rule = {"match": sug["match"], "fields": {}, "required_fields": []}
    assert extract_with_rule(ACME, rule)["matched"]


# ── filename ─────────────────────────────────────────────────────────


def test_acme_filename(sug):
    assert sug["filename_suggestion"] == "01_acme_telecom_invoice.yml"


def test_strips_legal_suffix():
    assert "corp" not in bootstrap_from_text(
        "Globex Corp\nInvoice 1234\nAmount: USD 100\n"
    )["filename_suggestion"]


def test_reminder_doctype():
    assert "reminder" in bootstrap_from_text(
        "Globex Inc\nReminder\nOverdue USD 100\n"
    )["filename_suggestion"]


# ── render_yaml ──────────────────────────────────────────────────────


def test_skeleton_is_valid_yaml(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert {"issuer", "match", "exclude", "fields", "required_fields", "options"} <= parsed.keys()


def test_skeleton_match_uses_detected_regex(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert parsed["match"] == sug["match"]


def test_skeleton_fields_block_is_empty(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert parsed["fields"] == {}
    assert parsed["required_fields"] == []


def test_user_overrides_match(sug):
    parsed = yaml.safe_load(render_yaml(sug, match="custom-regex-here"))
    assert parsed["match"] == "custom-regex-here"


def test_engine_accepts_skeleton(sug):
    # Skeleton should match the source document since the bootstrap built
    # the regex from that source.
    parsed = yaml.safe_load(render_yaml(sug))
    r = extract_with_rule(ACME, parsed)
    assert r["matched"]


# ── schema sanity ────────────────────────────────────────────────────


def test_top_level_keys(sug):
    assert set(sug) == {
        "issuer", "language", "currency", "match", "filename_suggestion",
    }
