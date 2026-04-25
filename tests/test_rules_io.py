"""Unit tests for the YAML rule file I/O layer (path-traversal safety, atomic
saves, list/read/delete behaviour). Pure stdlib + pyyaml."""
from __future__ import annotations

import pytest

from paperless_rules.rules_io import (
    RulesIOError,
    delete_rule,
    list_rules,
    read_rule,
    validate_filename,
    write_rule,
)


class TestValidateFilename:
    def test_accepts_yml(self):
        assert validate_filename("01_swisscom.yml") == "01_swisscom.yml"

    def test_accepts_yaml(self):
        assert validate_filename("rule.yaml") == "rule.yaml"

    def test_rejects_path_traversal(self):
        with pytest.raises(RulesIOError):
            validate_filename("../etc/passwd")

    def test_rejects_slash(self):
        with pytest.raises(RulesIOError):
            validate_filename("subdir/rule.yml")

    def test_rejects_backslash(self):
        with pytest.raises(RulesIOError):
            validate_filename(r"subdir\rule.yml")

    def test_rejects_dotdot(self):
        with pytest.raises(RulesIOError):
            validate_filename("..yml")

    def test_rejects_empty(self):
        with pytest.raises(RulesIOError):
            validate_filename("")

    def test_rejects_unicode(self):
        # We deliberately restrict to ASCII for clean repos.
        with pytest.raises(RulesIOError):
            validate_filename("règle.yml")

    def test_rejects_no_extension(self):
        with pytest.raises(RulesIOError):
            validate_filename("rule")

    def test_rejects_wrong_extension(self):
        with pytest.raises(RulesIOError):
            validate_filename("rule.txt")


class TestList:
    def test_alphabetical_order(self, tmp_path):
        (tmp_path / "10_b.yml").write_text("issuer: B\nkeywords: [B]\n")
        (tmp_path / "01_a.yml").write_text("issuer: A\nkeywords: [A]\n")
        out = list_rules(tmp_path)
        assert [r["filename"] for r in out] == ["01_a.yml", "10_b.yml"]

    def test_returns_summary_fields(self, tmp_path):
        (tmp_path / "rule.yml").write_text(
            "issuer: Acme\n"
            "keywords: [Acme, Invoice]\n"
            "fields:\n"
            "  amount: { regex: 'X', type: float }\n"
            "  date: { regex: 'Y', type: date }\n"
        )
        rules = list_rules(tmp_path)
        assert rules[0] == {
            "filename": "rule.yml",
            "issuer": "Acme",
            "keywords": ["Acme", "Invoice"],
            "field_count": 2,
        }

    def test_skips_non_yaml_files(self, tmp_path):
        (tmp_path / "rule.yml").write_text("issuer: A\n")
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "config.json").write_text("{}")
        assert len(list_rules(tmp_path)) == 1

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "bad.yml").write_text(":\n  : not valid")
        (tmp_path / "good.yml").write_text("issuer: G\n")
        assert {r["filename"] for r in list_rules(tmp_path)} == {"good.yml"}

    def test_missing_dir_returns_empty(self, tmp_path):
        assert list_rules(tmp_path / "missing") == []


class TestRead:
    def test_round_trip(self, tmp_path):
        text = "issuer: Acme\nkeywords: [a, b]\n"
        (tmp_path / "r.yml").write_text(text)
        assert read_rule(tmp_path, "r.yml") == text

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RulesIOError):
            read_rule(tmp_path, "missing.yml")

    def test_path_traversal_raises(self, tmp_path):
        with pytest.raises(RulesIOError):
            read_rule(tmp_path, "../passwd.yml")


class TestWrite:
    def test_creates_dir(self, tmp_path):
        target = tmp_path / "rules"
        write_rule(target, "r.yml", "issuer: A\n")
        assert (target / "r.yml").read_text() == "issuer: A\n"

    def test_overwrites_existing(self, tmp_path):
        write_rule(tmp_path, "r.yml", "issuer: A\n")
        write_rule(tmp_path, "r.yml", "issuer: B\n")
        assert read_rule(tmp_path, "r.yml") == "issuer: B\n"

    def test_invalid_yaml_rejected(self, tmp_path):
        with pytest.raises(RulesIOError, match="invalid YAML"):
            write_rule(tmp_path, "r.yml", ":\n  : not valid")

    def test_top_level_must_be_mapping(self, tmp_path):
        with pytest.raises(RulesIOError, match="mapping"):
            write_rule(tmp_path, "r.yml", "- a\n- b\n")

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(RulesIOError):
            write_rule(tmp_path, "../escape.yml", "issuer: X\n")
        # Confirm file did NOT land at the traversal target.
        assert not (tmp_path.parent / "escape.yml").exists()

    def test_atomic_no_partial_file_on_yaml_failure(self, tmp_path):
        write_rule(tmp_path, "r.yml", "issuer: original\n")
        with pytest.raises(RulesIOError):
            write_rule(tmp_path, "r.yml", ":\n  : broken")
        # Original content preserved.
        assert read_rule(tmp_path, "r.yml") == "issuer: original\n"
        # No leftover .tmp.
        assert not list(tmp_path.glob("*.tmp"))


class TestDelete:
    def test_removes_file(self, tmp_path):
        write_rule(tmp_path, "r.yml", "issuer: A\n")
        assert delete_rule(tmp_path, "r.yml") is True
        assert not (tmp_path / "r.yml").exists()

    def test_missing_returns_false(self, tmp_path):
        assert delete_rule(tmp_path, "missing.yml") is False

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(RulesIOError):
            delete_rule(tmp_path, "../something.yml")
