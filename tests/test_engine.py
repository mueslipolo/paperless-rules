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
    ("1'234.50", 1234.5),                # ASCII apostrophe
    ("1’234.50", 1234.5),                # typographic apostrophe
    ("1ʼ234.50", 1234.5),                # modifier letter apostrophe (OCR)
    ("1 234.50", 1234.5),                # NBSP
    ("89,50", 89.5),                     # comma decimal
    ("1.234,50", 1234.5),
    ("1,234.50", 1234.5),
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
    ("03/15/2024", ["%m/%d/%Y"], "2024-03-15"),
    ("not a date", None, None),
])
def test_date_coercion(raw, formats, expected):
    assert coerce_value(raw, "date", formats) == expected


def test_str_coercion_trims():
    assert coerce_value("  hello  ", "str") == "hello"


# ── match / exclude ──────────────────────────────────────────────────


def make_rule(**kw):
    return {"match": "", "exclude": "", "fields": {}, "required": None, "options": {}, **kw}


@pytest.mark.parametrize("match,matched", [
    ("Acme.*?Facture", True),
    ("Facture", True),
    ("Acme XYZ_does_not_match", False),
    (["Acme", "Facture"], True),
    (["Acme", "XYZ_no_match"], False),
    ("", True),
])
def test_match_field(match, matched):
    assert extract_with_rule(ACME, make_rule(match=match))["matched"] == matched


@pytest.mark.parametrize("exclude,matched,excluded_by", [
    ("Mahnung", True, None),
    ("Facture", False, "Facture"),
    (["Mahnung", "Facture"], False, "Facture"),
])
def test_exclude_field(exclude, matched, excluded_by):
    rule = make_rule(match="Acme", exclude=exclude)
    r = extract_with_rule(ACME, rule)
    assert r["matched"] == matched and r["excluded_by"] == excluded_by


def test_empty_exclude_is_ignored():
    assert extract_with_rule(ACME, make_rule(match="Acme", exclude=""))["matched"]


# ── fields: regex form ──────────────────────────────────────────────


@pytest.mark.parametrize("fname,spec,expected_type,expected_value", [
    ("amount", r"Total à payer\s+EUR\s+([\d ,-]+)", "float", 1234.5),
    ("date", r"Date d'émission\s+(\d{2}\.\d{2}\.\d{4})", "date", "2024-03-15"),
    ("invoice_number",
     [r"NotPresent\s+(\d+)", r"Numéro de facture\s+(\d+)"], "str", "987654321"),
    ("ref", {"regex": r"Numéro de client\s+(\d+)", "type": "str"},
     "str", "1234567890"),
])
def test_field_extraction(fname, spec, expected_type, expected_value):
    f = extract_with_rule(ACME, make_rule(match="Acme", fields={fname: spec}))
    f = f["fields"][fname]
    assert f["ok"] and f["type"] == expected_type and f["value"] == expected_value


# ── fields: value form ──────────────────────────────────────────────


def test_value_constant_string():
    rule = make_rule(match="Acme", fields={
        "correspondent": {"value": "Acme Corporation"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["correspondent"]
    assert f["ok"] and f["value"] == "Acme Corporation"


def test_value_list_passes_through():
    rule = make_rule(match="Acme", fields={"tags": {"value": ["invoice", "monthly"]}})
    f = extract_with_rule(ACME, rule)["fields"]["tags"]
    assert f["ok"] and f["value"] == ["invoice", "monthly"]


def test_value_coerces_by_type():
    rule = make_rule(match="Acme", fields={
        "amount": {"value": "1234.50", "type": "float"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["amount"]
    assert f["ok"] and f["value"] == 1234.5


# ── fields: template form ───────────────────────────────────────────


def test_template_substitutes_other_fields():
    rule = make_rule(match="Acme", fields={
        "amount": {"regex": r"Total à payer\s+EUR\s+([\d ,-]+)", "type": "float"},
        "date":   {"regex": r"Date d'émission\s+(\d{2}\.\d{2}\.\d{4})", "type": "date"},
        "title":  {"template": "{date} Acme EUR{amount}"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["title"]
    assert f["ok"] and f["value"] == "2024-03-15 Acme EUR1234.5"


def test_template_with_constant_value():
    rule = make_rule(match="Acme", fields={
        "vendor":  {"value": "Acme"},
        "title":   {"template": "Invoice from {vendor}"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["title"]
    assert f["ok"] and f["value"] == "Invoice from Acme"


def test_template_missing_var_renders_empty():
    rule = make_rule(match="Acme", fields={
        "title": {"template": "Hello {nonexistent}"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["title"]
    # str type strips trailing whitespace from the rendered template.
    assert f["value"] == "Hello"


def test_template_chains_to_template():
    rule = make_rule(match="Acme", fields={
        "vendor":   {"value": "Acme"},
        "year":     {"value": "2024"},
        "label":    {"template": "{vendor} {year}"},
        "filename": {"template": "{label}_invoice"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["filename"]
    assert f["ok"] and f["value"] == "Acme 2024_invoice"


def test_template_cycle_produces_error():
    rule = make_rule(match="Acme", fields={
        "a": {"template": "{b}"},
        "b": {"template": "{a}"},
    })
    fields = extract_with_rule(ACME, rule)["fields"]
    # The second-visited template in the cycle surfaces "template cycle"; the
    # first-visited bottoms out with an empty render. At least one of them
    # must report the cycle for the cycle detection to be doing its job.
    errors = " | ".join(f.get("error") or "" for f in fields.values())
    assert "cycle" in errors


# ── internal flag ────────────────────────────────────────────────────


def test_internal_flag_preserved_in_result():
    rule = make_rule(match="Acme", fields={
        "raw_ref": {"regex": r"facture\s+(\d+)", "internal": True},
    })
    f = extract_with_rule(ACME, rule)["fields"]["raw_ref"]
    assert f["ok"] and f["internal"] is True


# ── required ─────────────────────────────────────────────────────────


def test_required_blocks_when_field_missing():
    rule = make_rule(match="Acme", fields={
        "amount":  {"regex": r"Total à payer\s+EUR\s+([\d ,-]+)", "type": "float"},
        "missing": {"regex": r"XYZ_does_not_match"},
    }, required=["amount", "missing"])
    r = extract_with_rule(ACME, rule)
    assert r["matched"] and not r["required_ok"]


def test_required_ok_when_listed_fields_match():
    rule = make_rule(match="Acme", fields={
        "amount":  {"regex": r"Total à payer\s+EUR\s+([\d ,-]+)", "type": "float"},
        "missing": {"regex": r"XYZ"},
    }, required=["amount"])
    assert extract_with_rule(ACME, rule)["required_ok"]


def test_no_required_means_match_is_enough():
    assert extract_with_rule(ACME, make_rule(match="Acme"))["required_ok"]


# ── error / edge cases ──────────────────────────────────────────────


def test_invalid_regex_reports_error():
    f = extract_with_rule(ACME, make_rule(
        match="Acme", fields={"x": {"regex": "[unclosed", "type": "float"}},
    ))["fields"]["x"]
    assert not f["ok"] and "invalid regex" in (f["error"] or "")


def test_unknown_yaml_keys_ignored():
    rule = make_rule(match="Acme")
    rule["future_feature"] = {"some": "data"}
    assert extract_with_rule(ACME, rule)["matched"]


def test_no_regex_in_dict_spec():
    f = extract_with_rule(ACME, make_rule(match="Acme", fields={"x": {"type": "float"}}))["fields"]["x"]
    assert f["error"] == "no regex defined"


def test_none_text_treated_as_empty():
    assert not extract_with_rule(None, make_rule(match="Acme"))["matched"]  # type: ignore[arg-type]


# ── transforms (existing — still apply to regex fields) ─────────────


def test_value_on_match_constant_when_pattern_hits():
    # When `regex:` and `value:` are both present, value is the constant
    # assigned when the regex matches. Regex acts as a trigger.
    rule = make_rule(match="Acme", fields={
        "is_invoice": {"regex": "Facture", "value": "yes", "type": "str"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["is_invoice"]
    assert f["ok"] and f["value"] == "yes"


def test_value_alone_is_a_constant():
    # Without `regex:`, value: is just a constant assignment.
    rule = make_rule(match="Acme", fields={
        "document_type": {"value": "Invoice"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["document_type"]
    assert f["ok"] and f["value"] == "Invoice"


def test_default_fallback_for_missing_regex():
    rule = make_rule(match="Acme", fields={
        "x": {"regex": "XYZ_does_not_match", "default": "fallback", "type": "str"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["x"]
    assert f["ok"] and f["value"] == "fallback"


def test_pick_last_match():
    rule = make_rule(match="Acme", fields={
        "d": {"regex": r"(\d{2}\.\d{2}\.\d{4})", "pick": "last", "type": "str"},
    })
    f = extract_with_rule(ACME, rule)["fields"]["d"]
    assert f["ok"] and f["value"] == "14.04.2024"


def test_map_substitutes_known_value():
    text = "Country: DE\n"
    rule = make_rule(match="", fields={
        "country": {"regex": r"Country:\s*(\w+)", "map": {"DE": "Germany"}, "type": "str"},
    })
    f = extract_with_rule(text, rule)["fields"]["country"]
    assert f["ok"] and f["value"] == "Germany"


@pytest.mark.parametrize("op,expected", [
    ("sum",  35.5), ("min", 5.0), ("max", 20.5), ("count", 3),
])
def test_aggregate_over_line_items(op, expected):
    text = "Item A: $10.00\nItem B: $20.50\nItem C: $5.00\n"
    rule = make_rule(match="", fields={
        "agg": {"regex": r"\$([\d.]+)", "aggregate": op, "type": "float"},
    })
    assert abs(extract_with_rule(text, rule)["fields"]["agg"]["value"] - expected) < 0.001


def test_combine_captures():
    text = "First: Alice\nLast: Smith\n"
    rule = make_rule(match="", fields={
        "name": {"regex": [r"First:\s*(\w+)", r"Last:\s*(\w+)"],
                 "combine": " ", "type": "str"},
    })
    f = extract_with_rule(text, rule)["fields"]["name"]
    assert f["ok"] and f["value"] == "Alice Smith"


# ── load_rules / find_matching_rule ─────────────────────────────────


def test_loads_yml_files_alphabetically(tmp_path):
    (tmp_path / "10_b.yml").write_text("match: B\n")
    (tmp_path / "01_a.yml").write_text("match: A\n")
    (tmp_path / "rule.yaml").write_text("match: C\n")
    (tmp_path / "ignore.txt").write_text("nope")
    assert [n for n, _ in load_rules(tmp_path)] == ["01_a.yml", "10_b.yml", "rule.yaml"]


def test_skips_malformed_or_non_mapping(tmp_path):
    (tmp_path / "bad.yml").write_text(":\n  : not valid")
    (tmp_path / "list.yml").write_text("- one\n- two\n")
    (tmp_path / "good.yml").write_text("match: G\n")
    assert {n for n, _ in load_rules(tmp_path)} == {"good.yml"}


def test_first_matching_rule_wins():
    a = ("01.yml", {"match": "Acme.*?XYZ_no_match"})
    b = ("99.yml", {"match": "Acme"})
    result = find_matching_rule(ACME, [a, b])
    assert result is not None and result[0] == "99.yml"


def test_no_match_returns_none():
    assert find_matching_rule(ACME, [("x.yml", {"match": "XYZ_no_match"})]) is None
