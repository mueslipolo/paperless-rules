"""YAML rule file I/O tests — path-traversal safety, atomic saves."""
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


@pytest.mark.parametrize("name", ["01_acme.yml", "rule.yaml"])
def test_validate_accepts(name):
    assert validate_filename(name) == name


@pytest.mark.parametrize("name", [
    "../etc/passwd", "subdir/rule.yml", r"subdir\rule.yml",
    "..yml", "", "règle.yml", "rule", "rule.txt",
])
def test_validate_rejects(name):
    with pytest.raises(RulesIOError):
        validate_filename(name)


def test_list_alphabetical_with_summary(tmp_path):
    (tmp_path / "10_b.yml").write_text("match: B\n")
    (tmp_path / "01_a.yml").write_text(
        "match: A\nfields:\n  amount: { regex: 'X', type: float }\n"
    )
    rules = list_rules(tmp_path)
    assert [r["filename"] for r in rules] == ["01_a.yml", "10_b.yml"]
    assert rules[0] == {"filename": "01_a.yml", "match": "A", "field_count": 1}


def test_list_skips_non_yaml_and_malformed(tmp_path):
    (tmp_path / "rule.yml").write_text("issuer: A\n")
    (tmp_path / "readme.txt").write_text("hello")
    (tmp_path / "bad.yml").write_text(":\n  : not valid")
    assert {r["filename"] for r in list_rules(tmp_path)} == {"rule.yml"}


def test_list_missing_dir_returns_empty(tmp_path):
    assert list_rules(tmp_path / "missing") == []


def test_read_round_trip(tmp_path):
    text = "issuer: Acme\nmatch: a\n"
    (tmp_path / "r.yml").write_text(text)
    assert read_rule(tmp_path, "r.yml") == text


@pytest.mark.parametrize("path", ["missing.yml", "../passwd.yml"])
def test_read_errors(tmp_path, path):
    with pytest.raises(RulesIOError):
        read_rule(tmp_path, path)


def test_write_creates_dir_and_overwrites(tmp_path):
    target = tmp_path / "rules"
    write_rule(target, "r.yml", "issuer: A\n")
    write_rule(target, "r.yml", "issuer: B\n")
    assert read_rule(target, "r.yml") == "issuer: B\n"


@pytest.mark.parametrize("yaml_text,err_match", [
    (":\n  : not valid", "invalid YAML"),
    ("- a\n- b\n", "mapping"),  # top level must be a mapping
])
def test_write_rejects(tmp_path, yaml_text, err_match):
    with pytest.raises(RulesIOError, match=err_match):
        write_rule(tmp_path, "r.yml", yaml_text)


def test_write_path_traversal_rejected(tmp_path):
    with pytest.raises(RulesIOError):
        write_rule(tmp_path, "../escape.yml", "issuer: X\n")
    assert not (tmp_path.parent / "escape.yml").exists()


def test_write_atomic_no_partial_on_failure(tmp_path):
    write_rule(tmp_path, "r.yml", "issuer: original\n")
    with pytest.raises(RulesIOError):
        write_rule(tmp_path, "r.yml", ":\n  : broken")
    assert read_rule(tmp_path, "r.yml") == "issuer: original\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_delete_removes_or_returns_false(tmp_path):
    write_rule(tmp_path, "r.yml", "issuer: A\n")
    assert delete_rule(tmp_path, "r.yml") is True
    assert not (tmp_path / "r.yml").exists()
    assert delete_rule(tmp_path, "r.yml") is False


def test_delete_path_traversal_rejected(tmp_path):
    with pytest.raises(RulesIOError):
        delete_rule(tmp_path, "../something.yml")
