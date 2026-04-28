"""Slugify + auto_filename + rename_rule + reorder_rules.

Backs the editor's "hide the filename, show a display name" + "drag-to-reorder"
features. The numeric NN_ prefix is the rule's effective evaluation order
(load_rules sorts by filename), so renumbering on drop is the actual semantic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_rules.rules_io import (
    RulesIOError,
    auto_filename,
    list_rules,
    rename_rule,
    reorder_rules,
    slugify,
)


def _w(p: Path, name: str, body: str = "match: 'foo'\nfields: {}\n") -> Path:
    f = p / name
    f.write_text(body, encoding="utf-8")
    return f


# ── slugify ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("Acme Telecom", "acme_telecom"),
        ("Acme — Télécom!", "acme_t_l_com"),  # ASCII-only
        ("  spaces  trim  ", "spaces_trim"),
        ("UPPERCASE", "uppercase"),
        ("dash-separated", "dash_separated"),
        ("", "rule"),  # empty fallback
        ("///", "rule"),  # all-non-alnum fallback
        ("Foo123", "foo123"),
        ("Multiple___under", "multiple_under"),
    ],
)
def test_slugify(inp, out):
    assert slugify(inp) == out


# ── auto_filename ───────────────────────────────────────────────────


def test_auto_filename_picks_next_prefix(tmp_path: Path):
    _w(tmp_path, "01_first.yml")
    _w(tmp_path, "02_second.yml")
    assert auto_filename("Third Rule", tmp_path) == "03_third_rule.yml"


def test_auto_filename_first_rule(tmp_path: Path):
    assert auto_filename("My Rule", tmp_path) == "01_my_rule.yml"


def test_auto_filename_collision_appends_suffix(tmp_path: Path):
    _w(tmp_path, "01_acme.yml")
    # Same prefix, same slug → bumps to _2.
    name = auto_filename("Acme", tmp_path, prefix=1)
    assert name == "01_acme_2.yml"


def test_auto_filename_explicit_prefix(tmp_path: Path):
    assert auto_filename("Whatever", tmp_path, prefix=42) == "42_whatever.yml"


# ── rename_rule ─────────────────────────────────────────────────────


def test_rename_simple(tmp_path: Path):
    _w(tmp_path, "01_old.yml")
    new = rename_rule(tmp_path, "01_old.yml", "01_brand_new.yml")
    assert new == "01_brand_new.yml"
    assert (tmp_path / new).exists()
    assert not (tmp_path / "01_old.yml").exists()


def test_rename_to_self_is_noop(tmp_path: Path):
    _w(tmp_path, "01_x.yml")
    rename_rule(tmp_path, "01_x.yml", "01_x.yml")
    assert (tmp_path / "01_x.yml").exists()


def test_rename_missing_source_raises(tmp_path: Path):
    with pytest.raises(RulesIOError, match="not found"):
        rename_rule(tmp_path, "01_missing.yml", "02_target.yml")


def test_rename_clobber_protected(tmp_path: Path):
    _w(tmp_path, "01_a.yml")
    _w(tmp_path, "02_b.yml")
    with pytest.raises(RulesIOError, match="already exists"):
        rename_rule(tmp_path, "01_a.yml", "02_b.yml")


# ── reorder_rules ───────────────────────────────────────────────────


def test_reorder_renumbers_prefixes(tmp_path: Path):
    _w(tmp_path, "01_alpha.yml")
    _w(tmp_path, "02_beta.yml")
    _w(tmp_path, "03_gamma.yml")
    # Move gamma to the top.
    renamed = reorder_rules(tmp_path, ["03_gamma.yml", "01_alpha.yml", "02_beta.yml"])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["01_gamma.yml", "02_alpha.yml", "03_beta.yml"]
    # Mapping reflects what actually moved on disk.
    assert renamed == {
        "03_gamma.yml": "01_gamma.yml",
        "01_alpha.yml": "02_alpha.yml",
        "02_beta.yml": "03_beta.yml",
    }


def test_reorder_inplace_swap(tmp_path: Path):
    """Two-pass rename via .reorder.tmp avoids the FS refusing to overwrite
    when an in-place swap would otherwise collide."""
    _w(tmp_path, "01_a.yml")
    _w(tmp_path, "02_b.yml")
    reorder_rules(tmp_path, ["02_b.yml", "01_a.yml"])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["01_b.yml", "02_a.yml"]


def test_reorder_partial_keeps_unspecified_at_tail(tmp_path: Path):
    _w(tmp_path, "01_a.yml")
    _w(tmp_path, "02_b.yml")
    _w(tmp_path, "03_c.yml")
    # Only mention `c` — the rest keep their relative order, appended after.
    reorder_rules(tmp_path, ["03_c.yml"])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["01_c.yml", "02_a.yml", "03_b.yml"]


def test_reorder_unknown_filename_raises(tmp_path: Path):
    _w(tmp_path, "01_a.yml")
    with pytest.raises(RulesIOError, match="unknown rule"):
        reorder_rules(tmp_path, ["02_does_not_exist.yml"])


def test_reorder_no_changes_returns_empty(tmp_path: Path):
    _w(tmp_path, "01_a.yml")
    _w(tmp_path, "02_b.yml")
    assert reorder_rules(tmp_path, ["01_a.yml", "02_b.yml"]) == {}


# ── list_rules surfaces display name ────────────────────────────────


def test_list_rules_derives_name_from_filename(tmp_path: Path):
    _w(tmp_path, "01_acme_telecom.yml")
    [r] = list_rules(tmp_path)
    assert r["name"] == "acme telecom"


def test_list_rules_explicit_name_wins(tmp_path: Path):
    _w(tmp_path, "01_anything.yml", "name: 'Acme Télécom (Europe)'\nmatch: 'x'\n")
    [r] = list_rules(tmp_path)
    assert r["name"] == "Acme Télécom (Europe)"


def test_reorder_recovers_stranded_tmps(tmp_path: Path):
    """A crash between the two rename passes leaves *.reorder.tmp files; the
    next reorder must promote them so the rule doesn't disappear."""
    _w(tmp_path, "01_a.yml")
    stranded = tmp_path / "02_b.yml.reorder.tmp"
    stranded.write_text("match: 'foo'\nfields: {}\n", encoding="utf-8")

    reorder_rules(tmp_path, ["01_a.yml"])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["01_a.yml", "02_b.yml"]
