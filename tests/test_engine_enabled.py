"""enabled: false parks a rule (load_rules skips it; rules_io still lists it).

Plus a smoke test for rules_dir_signature — used by the poller's hot-reload.
"""
from __future__ import annotations

import time
from pathlib import Path

from paperless_rules.engine import load_rules, rules_dir_signature
from paperless_rules.rules_io import list_rules


def _write(p: Path, name: str, body: str) -> Path:
    f = p / name
    f.write_text(body, encoding="utf-8")
    return f


def test_enabled_default_true_loaded(tmp_path: Path):
    _write(tmp_path, "01.yml", "match: 'foo'\nfields: {}\n")
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0][0] == "01.yml"


def test_enabled_false_skipped_by_loader(tmp_path: Path):
    _write(tmp_path, "01.yml", "match: 'foo'\nfields: {}\n")
    _write(tmp_path, "02.yml", "enabled: false\nmatch: 'bar'\nfields: {}\n")
    rules = load_rules(tmp_path)
    assert [name for name, _ in rules] == ["01.yml"]


def test_enabled_false_explicit_true_loaded(tmp_path: Path):
    _write(tmp_path, "01.yml", "enabled: true\nmatch: 'foo'\nfields: {}\n")
    rules = load_rules(tmp_path)
    assert len(rules) == 1


def test_list_rules_surfaces_disabled(tmp_path: Path):
    _write(tmp_path, "01.yml", "match: 'foo'\nfields: {}\n")
    _write(tmp_path, "02.yml", "enabled: false\nmatch: 'bar'\nfields: {}\n")
    listing = {r["filename"]: r for r in list_rules(tmp_path)}
    # Both surface in the editor's rule list...
    assert set(listing) == {"01.yml", "02.yml"}
    # ...with the runtime-relevant flag.
    assert listing["01.yml"]["enabled"] is True
    assert listing["02.yml"]["enabled"] is False


# ── rules_dir_signature (poller hot-reload fingerprint) ──────────────


def test_signature_stable_when_unchanged(tmp_path: Path):
    _write(tmp_path, "01.yml", "match: 'foo'\n")
    _write(tmp_path, "02.yml", "match: 'bar'\n")
    s1 = rules_dir_signature(tmp_path)
    s2 = rules_dir_signature(tmp_path)
    assert s1 == s2


def test_signature_shifts_when_file_modified(tmp_path: Path):
    p = _write(tmp_path, "01.yml", "match: 'foo'\n")
    s1 = rules_dir_signature(tmp_path)
    time.sleep(0.01)               # mtime resolution; ns granularity covers most FS
    p.write_text("match: 'foo updated'\n", encoding="utf-8")
    s2 = rules_dir_signature(tmp_path)
    assert s1 != s2


def test_signature_shifts_when_file_added(tmp_path: Path):
    _write(tmp_path, "01.yml", "match: 'foo'\n")
    s1 = rules_dir_signature(tmp_path)
    _write(tmp_path, "02.yml", "match: 'bar'\n")
    s2 = rules_dir_signature(tmp_path)
    assert s1 != s2


def test_signature_empty_dir(tmp_path: Path):
    assert rules_dir_signature(tmp_path) == ()


def test_signature_missing_dir(tmp_path: Path):
    assert rules_dir_signature(tmp_path / "does-not-exist") == ()
