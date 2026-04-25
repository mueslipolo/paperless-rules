"""Engine unit tests. Pure Python, fast."""
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
ACME = (FIXTURES / "acme_invoice.txt").read_text(encoding="utf-8")


# ── coercion ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("89.50", 89.5),
    ("1'234.50", 1234.5),                # ASCII apostrophe thousand sep
    ("1’234.50", 1234.5),                # typographic apostrophe
    ("1ʼ234.50", 1234.5),                # modifier letter apostrophe (OCR)
    ("1 234.50", 1234.5),                # NBSP
    ("89,50", 89.5),                     # comma decimal
    ("1.234,50", 1234.5),                # dot-thousand, comma-decimal
    ("1,234.50", 1234.5),                # comma-thousand, dot-decimal
    ("-10.00", -10.0),
    ("not a number", None),
    ("", None),
])
def test_float_coercion(raw, expected):
    assert coerce_value(raw, "float") == expected


@pytest.mark.parametrize("raw,formats,expected", [
    ("15.03.2024", None, "2024-03-15"),
    ("2024-03-15", None, "2024-03-15"),
    ("15/03/2024", None, "2024-03-15"),
    ("15.03.24", None, "2024-03-15"),
    ("03/15/2024", ["%m/%d/%Y"], "2024-03-15"),  # user format takes precedence
    ("not a date", None, None),
])
def test_date_coercion(raw, formats, expected):
    assert coerce_value(raw, "date", formats) == expected


def test_str_coercion_trims():
    assert coerce_value("  hello  ", "str") == "hello"


# ── extraction ───────────────────────────────────────────────────────


def make_rule(**kw):
    return {
        "issuer": "Test", "keywords": [], "exclude_keywords": [],
        "fields": {}, "required_fields": None, "options": {}, **kw,
    }


@pytest.mark.parametrize("keywords,matched,missing", [
    (["Acme", "Facture"], True, []),
    (["Acme", "AbsentWord"], False, ["AbsentWord"]),
    (["Acm.", "Facture"], True, []),                    # regex semantics
    ([r"^Total à payer"], True, []),                    # MULTILINE
])
def test_keyword_matching(keywords, matched, missing):
    r = extract_with_rule(ACME, make_rule(keywords=keywords))
    assert r["matched"] == matched
    assert r["missing_keywords"] == missing


@pytest.mark.parametrize("excludes,matched,excluded_by", [
    (["Mahnung", "Rappel"], True, None),
    (["Facture"], False, "Facture"),
])
def test_exclude_keywords(excludes, matched, excluded_by):
    r = extract_with_rule(ACME, make_rule(keywords=["Acme"], exclude_keywords=excludes))
    assert r["matched"] == matched
    assert r["excluded_by"] == excluded_by


@pytest.mark.parametrize("fname,spec,expected_type,expected_value", [
    ("amount", r"Total à payer\s+EUR\s+([\d ,-]+)", "float", 1234.5),
    ("date", r"Date d'émission\s+(\d{2}\.\d{2}\.\d{4})", "date", "2024-03-15"),
    # list of patterns: first misses, second hits → first-match-wins
    ("invoice_number", [r"NotPresent\s+(\d+)", r"Numéro de facture\s+(\d+)"], "str", "987654321"),
    # dict with explicit type override
    ("ref", {"regex": r"Numéro de client\s+(\d+)", "type": "str"}, "str", "1234567890"),
])
def test_field_extraction(fname, spec, expected_type, expected_value):
    f = extract_with_rule(ACME, make_rule(keywords=["Acme"], fields={fname: spec}))
    f = f["fields"][fname]
    assert f["ok"]
    assert f["type"] == expected_type
    assert f["value"] == expected_value


# ── field transforms: value (constant on match) ─────────────────────


def test_value_constant_on_match():
    rule = make_rule(keywords=["Acme"], fields={
        "is_invoice": {"regex": "Facture", "value": "yes", "type": "str"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["is_invoice"]
    assert f["ok"] and f["value"] == "yes"


def test_value_constant_on_no_match():
    rule = make_rule(keywords=["Acme"], fields={
        "is_overdue": {"regex": "OVERDUE", "value": "yes", "type": "str"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["is_overdue"]
    assert not f["ok"]


def test_value_constant_with_pattern_list():
    # Constant is set when ANY pattern matches.
    rule = make_rule(keywords=["Acme"], fields={
        "category": {
            "regex": ["Facture", "Invoice"],   # either trigger
            "value": "billing",
            "type": "str",
        },
    })
    f = extract_with_rule(ACME, rule)["fields"]["category"]
    assert f["ok"] and f["value"] == "billing"


# ── field transforms: combine (concatenate captures) ────────────────


def test_combine_captures_with_separator():
    text = "First name: Alice\nLast name: Smith\n"
    rule = make_rule(keywords=[], fields={
        "full_name": {
            "regex": [r"First name:\s*(\w+)", r"Last name:\s*(\w+)"],
            "combine": " ",
            "type": "str",
        },
    })
    f = extract_with_rule(text, rule)["fields"]["full_name"]
    assert f["ok"] and f["value"] == "Alice Smith"


def test_combine_partial_match():
    # Only the first regex matches — that capture is used alone.
    text = "First name: Alice\n(no last name on this doc)"
    rule = make_rule(keywords=[], fields={
        "full_name": {
            "regex": [r"First name:\s*(\w+)", r"Last name:\s*(\w+)"],
            "combine": " ",
            "type": "str",
        },
    })
    f = extract_with_rule(text, rule)["fields"]["full_name"]
    assert f["ok"] and f["value"] == "Alice"


def test_combine_no_match_fails():
    text = "Just some unrelated text\n"
    rule = make_rule(keywords=[], fields={
        "full_name": {
            "regex": [r"First name:\s*(\w+)", r"Last name:\s*(\w+)"],
            "combine": " ",
            "type": "str",
        },
    })
    f = extract_with_rule(text, rule)["fields"]["full_name"]
    assert not f["ok"]


# ── required_fields semantics ────────────────────────────────────────


def test_default_all_fields_required():
    rule = make_rule(
        keywords=["Acme"],
        fields={
            "amount": r"Total à payer\s+EUR\s+([\d ,-]+)",
            "missing": r"XYZ_does_not_match",
        },
    )
    r = extract_with_rule(ACME, rule)
    assert r["matched"] and not r["required_ok"]


def test_explicit_required_only():
    rule = make_rule(
        keywords=["Acme"],
        fields={"amount": r"Total à payer\s+EUR\s+([\d ,-]+)", "opt": r"XYZ"},
        required_fields=["amount"],
    )
    assert extract_with_rule(ACME, rule)["required_ok"]


def test_empty_required_passes_with_keywords_alone():
    rule = make_rule(keywords=["Acme"], fields={"missing": r"XYZ"}, required_fields=[])
    assert extract_with_rule(ACME, rule)["required_ok"]


# ── error / edge cases ───────────────────────────────────────────────


def test_invalid_regex_reports_error():
    f = extract_with_rule(ACME, make_rule(
        keywords=["Acme"], fields={"x": {"regex": "[unclosed", "type": "float"}},
    ))["fields"]["x"]
    assert not f["ok"] and "invalid regex" in (f["error"] or "")


def test_unknown_yaml_keys_ignored():
    rule = make_rule(keywords=["Acme"])
    rule["future_feature"] = {"some": "data"}
    assert extract_with_rule(ACME, rule)["matched"]


def test_no_regex_in_dict_spec():
    f = extract_with_rule(ACME, make_rule(
        keywords=["Acme"], fields={"x": {"type": "float"}},
    ))["fields"]["x"]
    assert f["error"] == "no regex defined"


def test_none_text_treated_as_empty():
    assert not extract_with_rule(None, make_rule(keywords=["Acme"]))["matched"]  # type: ignore[arg-type]


# ── load_rules ───────────────────────────────────────────────────────


def test_loads_yml_files_alphabetically(tmp_path):
    (tmp_path / "10_b.yml").write_text("issuer: B\nkeywords: [B]\n")
    (tmp_path / "01_a.yml").write_text("issuer: A\nkeywords: [A]\n")
    (tmp_path / "rule.yaml").write_text("issuer: C\nkeywords: [C]\n")
    (tmp_path / "ignore.txt").write_text("nope")
    assert [n for n, _ in load_rules(tmp_path)] == ["01_a.yml", "10_b.yml", "rule.yaml"]


def test_skips_malformed_or_non_mapping(tmp_path):
    (tmp_path / "bad.yml").write_text(":\n  : not valid")
    (tmp_path / "list.yml").write_text("- one\n- two\n")  # list, not mapping
    (tmp_path / "good.yml").write_text("issuer: G\n")
    assert {n for n, _ in load_rules(tmp_path)} == {"good.yml"}


def test_missing_dir_returns_empty(tmp_path):
    assert load_rules(tmp_path / "missing") == []


# ── find_matching_rule ───────────────────────────────────────────────


def test_first_matching_rule_wins():
    a = ("01.yml", {"keywords": ["Acme", "AbsentKW"]})
    b = ("99.yml", {"keywords": ["Acme"]})
    result = find_matching_rule(ACME, [a, b])
    assert result is not None and result[0] == "99.yml"


def test_no_match_returns_none():
    assert find_matching_rule(ACME, [("x.yml", {"keywords": ["XYZ_no_match"]})]) is None
