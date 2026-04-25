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


# ── keywords ─────────────────────────────────────────────────────────


def test_keywords_within_bounds(sug):
    assert 1 <= len(sug["keywords"]) <= 6


def test_first_two_keywords_pre_checked(sug):
    assert sug["keywords"][0]["suggested"]
    if len(sug["keywords"]) >= 2:
        assert sug["keywords"][1]["suggested"]


def test_keywords_include_issuer_or_doctype(sug):
    phrases = " | ".join(k["phrase"].lower() for k in sug["keywords"])
    assert "acme" in phrases or "facture" in phrases


def test_no_stop_only_phrases():
    sug = bootstrap_from_text("Le de à du les pour\n" * 5)
    stops = {"le", "de", "à", "du", "les", "pour"}
    for k in sug["keywords"]:
        assert any(w not in stops for w in k["phrase"].lower().split())


# ── fields ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("expected", ["amount", "iban", "date", "due_date"])
def test_field_detected(sug, expected):
    assert expected in {f["name"] for f in sug["fields"]}


def test_amount_value_has_digit(sug):
    amount = next(f for f in sug["fields"] if f["name"] == "amount")
    assert any(c.isdigit() for c in amount["sample_value"])


def test_iban_starts_with_country_code(sug):
    iban = next(f for f in sug["fields"] if f["name"] == "iban")
    assert iban["sample_value"][:2].isalpha() and iban["sample_value"][:2].isupper()


def test_at_most_six_fields(sug):
    assert len(sug["fields"]) <= 6


def test_canonical_fields_pre_suggested(sug):
    suggested = {f["name"] for f in sug["fields"] if f["suggested"]}
    assert "amount" in suggested and "date" in suggested


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
    assert {"issuer", "keywords", "fields", "options"} <= parsed.keys()


def test_empty_regex_strings_intentional(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    for fspec in parsed["fields"].values():
        assert fspec["regex"] == ""


def test_required_only_typed_fields(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    for fname in parsed["required_fields"]:
        assert parsed["fields"][fname]["type"] in ("float", "date")


def test_user_overrides_keywords_and_fields(sug):
    parsed = yaml.safe_load(render_yaml(
        sug, selected_keywords=["only-this"], selected_fields=["amount"]
    ))
    assert parsed["keywords"] == ["only-this"]
    assert list(parsed["fields"].keys()) == ["amount"]


def test_engine_accepts_skeleton_without_crashing(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    r = extract_with_rule(ACME, parsed)
    assert r["matched"] and not r["required_ok"]


# ── schema sanity ────────────────────────────────────────────────────


def test_top_level_keys(sug):
    assert set(sug) == {
        "issuer", "language", "currency",
        "keywords", "fields", "filename_suggestion",
    }


def test_keyword_entry_shape(sug):
    for k in sug["keywords"]:
        assert set(k) == {"phrase", "score", "suggested"}


def test_field_entry_shape(sug):
    for f in sug["fields"]:
        assert set(f) == {"name", "label", "sample_value", "regex_hint", "type", "suggested"}
        assert f["type"] in ("float", "date", "str")
