"""Tier 2 unit tests for the bootstrap heuristics. Pure Python, no Docker."""
from __future__ import annotations

from pathlib import Path

import yaml

from paperless_rules.bootstrap import bootstrap_from_text, render_yaml
from paperless_rules.engine import extract_with_rule

FIXTURES = Path(__file__).parent / "fixtures"
SWISSCOM_TEXT = (FIXTURES / "swisscom_invoice.txt").read_text(encoding="utf-8")


# ── issuer detection ──────────────────────────────────────────────────


class TestIssuerDetection:
    def test_swisscom_extracted(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        # The SA-suffixed line should win over the address lines below.
        assert "Swisscom" in sug["issuer"]
        assert "SA" in sug["issuer"]

    def test_skips_generic_header_word(self):
        text = "FACTURE\nMy Company AG\nKönigstrasse 1\n8000 Zürich\n"
        sug = bootstrap_from_text(text)
        assert sug["issuer"] != "FACTURE"
        assert "AG" in sug["issuer"]

    def test_no_company_falls_back_to_longest(self):
        text = "Hello world\nThis is a longer line of text\nshort\n"
        sug = bootstrap_from_text(text)
        # No company suffix, no clearly capitalised name → fallback path.
        assert sug["issuer"] != ""

    def test_empty_text_returns_empty_issuer(self):
        assert bootstrap_from_text("")["issuer"] == ""


# ── language and currency ─────────────────────────────────────────────


class TestLanguageDetection:
    def test_french_fixture(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        assert sug["language"] == "fr"

    def test_german_text(self):
        text = (
            "Rechnung der Schweizerischen Post\n"
            "Sie haben den Betrag von CHF 50 zu zahlen.\n"
            "Die Rechnung ist fällig am 30.04.2024.\n"
            "Wir bitten Sie um pünktliche Zahlung.\n"
        )
        assert bootstrap_from_text(text)["language"] == "de"

    def test_english_text(self):
        text = (
            "The invoice is due on 2024-04-30.\n"
            "Please pay the amount of USD 50 by the due date.\n"
            "Thank you for your business.\n"
        )
        assert bootstrap_from_text(text)["language"] == "en"


class TestCurrencyDetection:
    def test_chf_in_fixture(self):
        assert bootstrap_from_text(SWISSCOM_TEXT)["currency"] == "CHF"

    def test_eur_text(self):
        text = "Total: EUR 100.00\n"
        assert bootstrap_from_text(text)["currency"] == "EUR"

    def test_default_chf_when_unknown(self):
        text = "Some text without any currency code.\n"
        assert bootstrap_from_text(text)["currency"] == "CHF"


# ── keyword candidates ────────────────────────────────────────────────


class TestKeywords:
    def test_returns_at_most_six(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        assert 1 <= len(sug["keywords"]) <= 6

    def test_first_two_pre_checked(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        # First two are suggested by default per spec section 6.
        assert sug["keywords"][0]["suggested"]
        assert sug["keywords"][1]["suggested"]
        if len(sug["keywords"]) > 2:
            assert not sug["keywords"][2]["suggested"]

    def test_top_keywords_include_issuer_or_doctype(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        phrases = " | ".join(k["phrase"].lower() for k in sug["keywords"])
        # Either the issuer name or the doctype hint should be in the top set
        # — these are the two strongest signals the heuristic recognises.
        assert "swisscom" in phrases or "facture" in phrases

    def test_scores_are_sorted_desc(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        scores = [k["score"] for k in sug["keywords"]]
        assert scores == sorted(scores, reverse=True)

    def test_stop_only_phrases_excluded(self):
        text = "Le de à du les pour\n" * 5
        sug = bootstrap_from_text(text)
        # Nothing scoreable at all.
        for k in sug["keywords"]:
            words = k["phrase"].lower().split()
            # If a phrase made it in, at least one word must be non-stop.
            assert any(w not in {"le", "de", "à", "du", "les", "pour"} for w in words)


# ── field candidates ──────────────────────────────────────────────────


class TestFieldCandidates:
    def test_amount_detected(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        names = {f["name"] for f in sug["fields"]}
        assert "amount" in names

    def test_amount_sample_value_is_swiss_format(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        amount = next(f for f in sug["fields"] if f["name"] == "amount")
        # Either the apostrophe form or one of the line-item amounts.
        assert any(c.isdigit() for c in amount["sample_value"])

    def test_iban_detected(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        names = {f["name"] for f in sug["fields"]}
        assert "iban" in names

    def test_iban_sample_starts_with_ch(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        iban = next(f for f in sug["fields"] if f["name"] == "iban")
        assert iban["sample_value"].startswith("CH")

    def test_date_detected(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        types = {f["type"] for f in sug["fields"]}
        assert "date" in types

    def test_due_date_separated_from_date(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        names = {f["name"] for f in sug["fields"]}
        # The fixture has both "Date d'émission" and "Échéance" — bootstrap
        # should distinguish them via the label-hint dictionary.
        assert "date" in names and "due_date" in names

    def test_at_most_six_fields(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        assert len(sug["fields"]) <= 6

    def test_unambiguous_fields_are_pre_suggested(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        suggested = {f["name"] for f in sug["fields"] if f["suggested"]}
        # amount and date are the canonical "always suggest" set.
        assert "amount" in suggested
        assert "date" in suggested


class TestPatternRegexes:
    """Spot-checks on the underlying regexes — these are the patterns the
    runtime will see if a user copies them into a rule."""

    def test_ahv_pattern(self):
        text = "Patient AHV: 756.1234.5678.90 — visit on 01.04.2024\n"
        sug = bootstrap_from_text(text)
        names = {f["name"] for f in sug["fields"]}
        assert "ahv" in names

    def test_gln_pattern(self):
        text = "Provider GLN 7601000123456 issued report\n"
        sug = bootstrap_from_text(text)
        names = {f["name"] for f in sug["fields"]}
        assert "gln" in names


# ── filename suggestion ───────────────────────────────────────────────


class TestFilenameSuggestion:
    def test_swisscom_invoice_filename(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        # Should encode issuer + doc type.
        assert sug["filename_suggestion"] == "01_swisscom_invoice.yml"

    def test_strips_legal_suffix(self):
        text = "Acme Corp\nInvoice number 1234\nAmount: CHF 100\n"
        sug = bootstrap_from_text(text)
        # The "_corp" suffix is stripped — keeps filenames clean.
        assert "corp" not in sug["filename_suggestion"]

    def test_reminder_doctype(self):
        text = "Acme AG\nMahnung\nÜberfällig CHF 100\nDate 01.01.2024\n"
        sug = bootstrap_from_text(text)
        assert "reminder" in sug["filename_suggestion"]


# ── render_yaml ───────────────────────────────────────────────────────


class TestRenderYAML:
    def test_skeleton_is_valid_yaml(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(render_yaml(sug))
        assert isinstance(parsed, dict)
        assert {"issuer", "keywords", "fields", "options"} <= parsed.keys()

    def test_empty_regex_strings_intentional(self):
        # Bootstrap only generates structure — the user fills in regexes.
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(render_yaml(sug))
        for fspec in parsed["fields"].values():
            assert fspec["regex"] == ""

    def test_required_fields_only_for_typed_fields(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(render_yaml(sug))
        # str fields like iban/reference shouldn't gate the rule by default —
        # the user can add them manually if they care.
        required = set(parsed["required_fields"])
        for fname in required:
            assert parsed["fields"][fname]["type"] in ("float", "date")

    def test_keywords_default_to_suggested(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(render_yaml(sug))
        suggested = [k["phrase"] for k in sug["keywords"] if k["suggested"]]
        assert parsed["keywords"] == suggested

    def test_user_overrides_keywords(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(
            render_yaml(sug, selected_keywords=["only-this"])
        )
        assert parsed["keywords"] == ["only-this"]

    def test_user_overrides_fields(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(
            render_yaml(sug, selected_fields=["amount"])
        )
        assert list(parsed["fields"].keys()) == ["amount"]

    def test_engine_accepts_skeleton_without_crashing(self):
        # The skeleton must parse cleanly through the engine. With empty
        # regexes, every field is treated as "no match" and required_ok
        # is False — but the engine must not raise.
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        parsed = yaml.safe_load(render_yaml(sug))
        result = extract_with_rule(SWISSCOM_TEXT, parsed)
        assert not result["required_ok"]
        # Keywords should match (they're real strings from the doc).
        assert result["matched"]


# ── full schema sanity ────────────────────────────────────────────────


class TestSchemaShape:
    """Pin down the bootstrap response shape — the API depends on this."""

    def test_top_level_keys(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        assert set(sug) == {
            "issuer", "language", "currency",
            "keywords", "fields", "filename_suggestion",
        }

    def test_keyword_entry_keys(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        for k in sug["keywords"]:
            assert set(k) == {"phrase", "score", "suggested"}
            assert isinstance(k["phrase"], str)
            assert isinstance(k["score"], (int, float))
            assert isinstance(k["suggested"], bool)

    def test_field_entry_keys(self):
        sug = bootstrap_from_text(SWISSCOM_TEXT)
        for f in sug["fields"]:
            assert set(f) == {
                "name", "label", "sample_value",
                "regex_hint", "type", "suggested",
            }
            assert f["type"] in ("float", "date", "str")
