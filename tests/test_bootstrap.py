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


def test_top_level_keys(sug):
    assert set(sug) == {"match", "exclude", "filename_suggestion", "language", "currency"}


def test_match_seed_is_doctype_hint(sug):
    # Acme fixture has "Facture" — that's the seed. The user makes it more
    # specific (e.g. "Acme.*?Facture") in the editor.
    assert sug["match"].lower() == "facture"


def test_exclude_starts_empty(sug):
    assert sug["exclude"] == ""


def test_match_when_no_doctype_hint():
    sug = bootstrap_from_text("Some unrelated correspondence.\n")
    assert sug["match"] == ""


def test_empty_text():
    assert bootstrap_from_text("")["match"] == ""


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


# ── filename ─────────────────────────────────────────────────────────


def test_acme_filename(sug):
    assert sug["filename_suggestion"] == "01_invoice.yml"


def test_reminder_filename():
    sug = bootstrap_from_text("Globex Inc\nReminder\nOverdue USD 100\n")
    assert "reminder" in sug["filename_suggestion"]


def test_default_filename_when_no_doctype():
    sug = bootstrap_from_text("Random text without a doctype hint.\n")
    assert sug["filename_suggestion"] == "01_rule.yml"


# ── render_yaml ──────────────────────────────────────────────────────


def test_skeleton_is_valid_yaml(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert {"match", "exclude", "fields", "required", "options"} <= parsed.keys()


def test_skeleton_match_uses_detected_seed(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert parsed["match"] == sug["match"]


def test_skeleton_fields_block_is_empty(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert parsed["fields"] == {}
    assert parsed["required"] == []


def test_user_overrides_match_and_exclude(sug):
    parsed = yaml.safe_load(render_yaml(sug, match="custom-regex", exclude="reminder"))
    assert parsed["match"] == "custom-regex"
    assert parsed["exclude"] == "reminder"


def test_engine_accepts_skeleton(sug):
    parsed = yaml.safe_load(render_yaml(sug))
    assert extract_with_rule(ACME, parsed)["matched"]
