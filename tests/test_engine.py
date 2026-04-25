"""Tier 1 unit tests for the engine. Pure Python, no Docker, fast.

Most assertions exercise behaviour that's hard to spot in code review:
Swiss-specific number coercion (apostrophes, NBSP, decimal separator
disambiguation), regex semantics on keywords, required_fields gating.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperless_rules.engine import (
    coerce_value,
    extract_with_rule,
    find_matching_rule,
    load_rules,
)

FIXTURES = Path(__file__).parent / "fixtures"
SWISSCOM_TEXT = (FIXTURES / "swisscom_invoice.txt").read_text(encoding="utf-8")


# ── coercion: float ───────────────────────────────────────────────────


class TestFloatCoercion:
    def test_simple(self):
        assert coerce_value("89.50", "float") == 89.5

    def test_swiss_apostrophe_thousands(self):
        # The canonical Swiss invoice format.
        assert coerce_value("1'234.50", "float") == 1234.5

    def test_typographic_apostrophe(self):
        # U+2019 RIGHT SINGLE QUOTATION MARK.
        assert coerce_value("1’234.50", "float") == 1234.5

    def test_modifier_letter_apostrophe(self):
        # U+02BC MODIFIER LETTER APOSTROPHE — emitted by some OCR engines
        # in place of U+0027.
        assert coerce_value("1ʼ234.50", "float") == 1234.5

    def test_nbsp_thousands_separator(self):
        # U+00A0 NO-BREAK SPACE — sometimes used as group sep in DE/FR text.
        assert coerce_value("1 234.50", "float") == 1234.5

    def test_european_comma_decimal(self):
        assert coerce_value("89,50", "float") == 89.5

    def test_european_format_with_thousands(self):
        # DE/FR: dot=thousands, comma=decimal. Rightmost wins.
        assert coerce_value("1.234,50", "float") == 1234.5

    def test_us_format_with_thousands(self):
        # US: comma=thousands, dot=decimal. Rightmost wins.
        assert coerce_value("1,234.50", "float") == 1234.5

    def test_negative_amount(self):
        # Discount lines: "Remise fidélité  CHF  -10.00"
        assert coerce_value("-10.00", "float") == -10.0

    def test_garbage_returns_none(self):
        assert coerce_value("not a number", "float") is None

    def test_empty_returns_none(self):
        assert coerce_value("", "float") is None


# ── coercion: date ────────────────────────────────────────────────────


class TestDateCoercion:
    def test_swiss_dot_format(self):
        assert coerce_value("15.03.2024", "date") == "2024-03-15"

    def test_iso_format(self):
        assert coerce_value("2024-03-15", "date") == "2024-03-15"

    def test_slash_format(self):
        assert coerce_value("15/03/2024", "date") == "2024-03-15"

    def test_two_digit_year(self):
        assert coerce_value("15.03.24", "date") == "2024-03-15"

    def test_user_format_takes_precedence(self):
        # The default fallback list does not include %m/%d/%Y, but the
        # user can supply it via options.date_formats.
        assert coerce_value("03/15/2024", "date", ["%m/%d/%Y"]) == "2024-03-15"

    def test_garbage_returns_none(self):
        assert coerce_value("not a date", "date") is None


# ── coercion: str ─────────────────────────────────────────────────────


class TestStrCoercion:
    def test_trims_whitespace(self):
        assert coerce_value("  hello  ", "str") == "hello"


# ── extract_with_rule: keywords + excludes ────────────────────────────


def make_rule(**kw):
    """Helper producing a rule dict with sensible defaults."""
    return {
        "issuer": kw.get("issuer", "Test"),
        "keywords": kw.get("keywords", []),
        "exclude_keywords": kw.get("exclude_keywords", []),
        "fields": kw.get("fields", {}),
        "required_fields": kw.get("required_fields"),
        "options": kw.get("options", {}),
    }


class TestKeywordMatching:
    def test_all_keywords_match(self):
        rule = make_rule(keywords=["Swisscom", "Facture"])
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]
        assert result["missing_keywords"] == []

    def test_one_missing_keyword_fails(self):
        rule = make_rule(keywords=["Swisscom", "AbsentWord"])
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert not result["matched"]
        assert result["missing_keywords"] == ["AbsentWord"]

    def test_keyword_is_regex_not_literal(self):
        # "." matches any char — Swiss.om matches Swisscom.
        rule = make_rule(keywords=["Swiss.om"])
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]

    def test_multiline_keyword_match(self):
        # MULTILINE: ^ / $ match line boundaries.
        rule = make_rule(keywords=[r"^Total à payer"])
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]


class TestExcludeKeywords:
    def test_no_excludes_means_match(self):
        rule = make_rule(
            keywords=["Swisscom"], exclude_keywords=["Mahnung", "Rappel"]
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]
        assert result["excluded_by"] is None

    def test_exclude_match_disqualifies(self):
        rule = make_rule(
            keywords=["Swisscom"], exclude_keywords=["Facture"]
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert not result["matched"]
        assert result["excluded_by"] == "Facture"


# ── extract_with_rule: field spec forms ───────────────────────────────


class TestFieldFormBareString:
    def test_bare_string_with_inferred_type(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"amount": r"Total à payer\s+CHF\s+([\d'.,-]+)"},
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["amount"]
        assert f["ok"]
        assert f["type"] == "float"
        assert f["value"] == 1234.50

    def test_inferred_type_from_date_name(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"date": r"Échéance\s+(\d{2}\.\d{2}\.\d{4})"},
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["date"]
        assert f["ok"]
        assert f["type"] == "date"
        assert f["value"] == "2024-04-14"


class TestFieldFormList:
    def test_first_match_wins(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={
                "invoice_number": [
                    r"NotPresent\s+(\d+)",
                    r"Numéro de facture\s+(\d+)",
                    r"Facture Nr\.\s+(\d+)",
                ]
            },
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["invoice_number"]
        assert f["ok"]
        assert f["raw"] == "987654321"
        assert f["pattern"] == r"Numéro de facture\s+(\d+)"


class TestFieldFormDict:
    def test_dict_with_explicit_type(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={
                "ref": {
                    "regex": r"Numéro de client\s+(\d+)",
                    "type": "str",
                }
            },
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["ref"]
        assert f["ok"]
        assert f["type"] == "str"
        assert f["value"] == "1234567890"

    def test_dict_with_list_regex(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={
                "amount": {
                    "regex": [
                        r"NotPresent\s+CHF\s+([\d'.,-]+)",
                        r"Total à payer\s+CHF\s+([\d'.,-]+)",
                    ],
                    "type": "float",
                }
            },
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["fields"]["amount"]["value"] == 1234.50


# ── extract_with_rule: required_fields semantics ──────────────────────


class TestRequiredFields:
    def test_default_all_fields_required(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={
                "amount": r"Total à payer\s+CHF\s+([\d'.,-]+)",
                "missing": r"This pattern does not match anything XYZ123",
            },
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]  # keywords are fine
        assert not result["required_ok"]  # but the missing field disqualifies

    def test_explicit_required_only(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={
                "amount": r"Total à payer\s+CHF\s+([\d'.,-]+)",
                "optional": r"This pattern does not match anything XYZ123",
            },
            required_fields=["amount"],
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["required_ok"]

    def test_empty_required_list_passes_with_keywords_only(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"missing": r"XYZ_DOES_NOT_MATCH"},
            required_fields=[],
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["required_ok"]


# ── extract_with_rule: error and edge cases ───────────────────────────


class TestErrorHandling:
    def test_invalid_regex_reports_error(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"amount": {"regex": "[unclosed", "type": "float"}},
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["amount"]
        assert not f["ok"]
        assert f["error"] is not None
        assert "invalid regex" in f["error"]

    def test_unknown_yaml_keys_ignored(self):
        rule = make_rule(keywords=["Swisscom"])
        rule["future_feature"] = {"some": "data"}
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        assert result["matched"]

    def test_no_regex_in_dict_spec(self):
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"amount": {"type": "float"}},
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["amount"]
        assert not f["ok"]
        assert f["error"] == "no regex defined"

    def test_none_text_treated_as_empty(self):
        rule = make_rule(keywords=["Swisscom"])
        result = extract_with_rule(None, rule)  # type: ignore[arg-type]
        assert not result["matched"]


class TestNumberCoercionInRule:
    """End-to-end: extract_with_rule + Swiss apostrophe in fixture."""

    def test_swiss_total_extraction(self):
        # The fixture has "Total à payer ... CHF 1'234.50".
        rule = make_rule(
            keywords=["Swisscom"],
            fields={"amount": r"Total à payer\s+CHF\s+([\d'.,-]+)"},
        )
        result = extract_with_rule(SWISSCOM_TEXT, rule)
        f = result["fields"]["amount"]
        assert f["raw"] == "1'234.50"
        assert f["value"] == 1234.5
        assert f["type"] == "float"


# ── load_rules ────────────────────────────────────────────────────────


class TestLoader:
    def test_loads_yml_files_alphabetically(self, tmp_path):
        (tmp_path / "10_b.yml").write_text("issuer: B\nkeywords: [B]\n")
        (tmp_path / "01_a.yml").write_text("issuer: A\nkeywords: [A]\n")
        (tmp_path / "not_yaml.txt").write_text("ignore me")
        rules = load_rules(tmp_path)
        assert [name for name, _ in rules] == ["01_a.yml", "10_b.yml"]

    def test_accepts_yaml_extension(self, tmp_path):
        (tmp_path / "rule.yaml").write_text("issuer: X\nkeywords: [X]\n")
        rules = load_rules(tmp_path)
        assert len(rules) == 1

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "bad.yml").write_text("key: : not: valid: yaml: [unclosed")
        (tmp_path / "good.yml").write_text("issuer: G\nkeywords: [G]\n")
        rules = load_rules(tmp_path)
        assert [name for name, _ in rules] == ["good.yml"]

    def test_skips_non_dict_yaml(self, tmp_path):
        # Top-level list isn't a rule.
        (tmp_path / "list.yml").write_text("- one\n- two\n")
        (tmp_path / "good.yml").write_text("issuer: G\nkeywords: [G]\n")
        rules = load_rules(tmp_path)
        assert [name for name, _ in rules] == ["good.yml"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_rules(tmp_path / "does-not-exist") == []


# ── find_matching_rule ────────────────────────────────────────────────


class TestFindMatchingRule:
    def test_first_matching_wins(self):
        rule_a = (
            "01_specific.yml",
            {
                "keywords": ["Swisscom", "AbsentKeyword"],
                "fields": {"amount": r"Total à payer\s+CHF\s+([\d'.,-]+)"},
            },
        )
        rule_b = (
            "99_generic.yml",
            {
                "keywords": ["Swisscom"],
                "fields": {"amount": r"Total à payer\s+CHF\s+([\d'.,-]+)"},
            },
        )
        result = find_matching_rule(SWISSCOM_TEXT, [rule_a, rule_b])
        assert result is not None
        filename, _ = result
        assert filename == "99_generic.yml"

    def test_no_match_returns_none(self):
        rules = [
            (
                "nope.yml",
                {"keywords": ["Definitely not in fixture XYZ123"]},
            )
        ]
        assert find_matching_rule(SWISSCOM_TEXT, rules) is None

    def test_match_requires_required_ok(self):
        # Keywords match but no field defined → required_fields default to
        # empty list → required_ok = matched = True. Sanity check that path.
        rules = [("k.yml", {"keywords": ["Swisscom"]})]
        result = find_matching_rule(SWISSCOM_TEXT, rules)
        assert result is not None
