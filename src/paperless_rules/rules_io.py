"""Rule file I/O: list, read, save, delete YAML rules with safety checks.

Path traversal is the only adversarial input the editor exposes, so filenames
are tightly constrained: `[A-Za-z0-9._-]+\\.ya?ml`, no slashes, no `..`. The
allowed-character set is intentionally narrower than the OS would accept —
clean filenames make for clean repos.

Saves are atomic (write-temp-then-rename) so a partial write can't leave a
malformed YAML on disk if the editor crashes mid-save.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]+\.ya?ml$")
# Filenames the editor auto-derives have a NN_ prefix governing evaluation
# order, then a slugified body. The runtime sorts by filename, so the
# numeric prefix is the rule's effective priority.
_AUTO_FILENAME_RE = re.compile(r"^(\d{2})_(.+)\.ya?ml$")


class RulesIOError(Exception):
    """Raised on filename validation, YAML parse, or filesystem failure."""


def slugify(name: str) -> str:
    """Slugify a free-text rule name into the body of an auto-generated filename.

    ASCII-only, lowercase, alphanumeric + underscore. Empty input falls back
    to ``rule`` so the caller can still produce a valid filename.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return s or "rule"


def auto_filename(name: str, rules_dir: Path, *, prefix: int | None = None) -> str:
    """Compose a NN_slug.yml filename for a rule with display ``name``.

    If ``prefix`` is None, picks the next available 2-digit prefix (one past
    the highest used in ``rules_dir``). Collisions on the slug itself get a
    numeric suffix (``_2``, ``_3``…) so two rules can share a display name
    without clobbering each other on disk.
    """
    rules_dir = Path(rules_dir)
    existing = list_rule_filenames(rules_dir)

    if prefix is None:
        used_prefixes: list[int] = []
        for f in existing:
            m = _AUTO_FILENAME_RE.match(f)
            if m:
                used_prefixes.append(int(m.group(1)))
        prefix = max(used_prefixes) + 1 if used_prefixes else 1

    base = slugify(name)
    candidate = f"{prefix:02d}_{base}.yml"
    n = 2
    while candidate in existing:
        candidate = f"{prefix:02d}_{base}_{n}.yml"
        n += 1
    return candidate


def list_rule_filenames(rules_dir: Path) -> list[str]:
    """Bare ls of yml/yaml files in ``rules_dir`` (sorted)."""
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    return sorted(
        p.name for p in rules_dir.iterdir()
        if p.suffix.lower() in (".yml", ".yaml")
    )


def validate_filename(filename: str) -> str:
    """Return the validated filename. Raise RulesIOError on any bad input."""
    if not filename:
        raise RulesIOError("filename is empty")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise RulesIOError("filename must not contain path separators")
    if not _FILENAME_RE.match(filename):
        raise RulesIOError(
            "filename must match [A-Za-z0-9._-]+.ya?ml"
        )
    return filename


def list_rules(rules_dir: Path) -> list[dict[str, Any]]:
    """Return summary info per rule file (filename, issuer, keywords, field_count).

    Files that fail to parse are skipped silently — surface broken rules in
    the UI by displaying their filenames separately if needed (out of scope
    for v1).
    """
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(rules_dir.iterdir()):
        if path.suffix.lower() not in (".yml", ".yaml"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "filename": path.name,
                # Display name: explicit `name:` field wins; otherwise derive
                # from the slug part of the auto-filename ("01_acme_telecom"
                # → "Acme telecom"). The SPA shows this everywhere instead
                # of the .yml filename.
                "name": _display_name(data, path.name),
                "match": data.get("match", "") or "",
                "field_count": len(data.get("fields") or {}),
                # `enabled: false` parks the rule without renaming it; the
                # runtime skips it but the editor still lists it.
                "enabled": data.get("enabled") is not False,
            }
        )
    return out


def _display_name(data: dict[str, Any], filename: str) -> str:
    """Pick the best human label for a rule. Explicit name wins; else
    derive a Title-Case-ish label from the auto-filename's slug body."""
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    m = _AUTO_FILENAME_RE.match(filename)
    body = m.group(2) if m else filename.rsplit(".", 1)[0]
    return body.replace("_", " ").strip() or filename


def read_rule(rules_dir: Path, filename: str) -> str:
    """Return the raw YAML text. Raises if the file is missing or unsafe."""
    filename = validate_filename(filename)
    path = Path(rules_dir) / filename
    if not path.is_file():
        raise RulesIOError(f"rule {filename!r} not found")
    return path.read_text(encoding="utf-8")


def write_rule(rules_dir: Path, filename: str, yaml_text: str) -> None:
    """Validate the YAML text and atomically write it to `<rules_dir>/<filename>`."""
    filename = validate_filename(filename)
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise RulesIOError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RulesIOError("rule must be a YAML mapping at the top level")

    rules_dir = Path(rules_dir)
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / filename
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml_text, encoding="utf-8")
    tmp.replace(path)


def delete_rule(rules_dir: Path, filename: str) -> bool:
    """Return True if the file was removed, False if it didn't exist."""
    filename = validate_filename(filename)
    path = Path(rules_dir) / filename
    if path.is_file():
        path.unlink()
        return True
    return False


def rename_rule(rules_dir: Path, old_filename: str, new_filename: str) -> str:
    """Rename a rule file in place, preserving the ``NN_`` prefix.

    Returns the actual new filename (slug-collision resolution may have
    appended a suffix). Raises if the source doesn't exist or the
    destination is already taken by a *different* file.
    """
    old_filename = validate_filename(old_filename)
    new_filename = validate_filename(new_filename)
    rules_dir = Path(rules_dir)
    src = rules_dir / old_filename
    dst = rules_dir / new_filename
    if not src.is_file():
        raise RulesIOError(f"rule {old_filename!r} not found")
    if dst.exists() and dst.resolve() != src.resolve():
        raise RulesIOError(f"target {new_filename!r} already exists")
    src.rename(dst)
    return new_filename


def reorder_rules(rules_dir: Path, ordered_filenames: list[str]) -> dict[str, str]:
    """Re-prefix the given files with sequential ``NN_`` numbers and rename.

    Used by the editor's drag-to-reorder. The list is the desired order;
    files not in the list are appended after, keeping their relative order
    (so a partial reorder is safe). Returns a mapping {old_name: new_name}
    for everything that actually changed on disk; unchanged entries are
    omitted. Raises ``RulesIOError`` on any unknown name.

    Implementation note: rename in two passes via temp names so an in-place
    swap (e.g. ``01_a.yml`` ↔ ``02_b.yml``) doesn't trip on the filesystem
    refusing to clobber an existing target.
    """
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        raise RulesIOError(f"{rules_dir!r} is not a directory")

    on_disk = list_rule_filenames(rules_dir)
    on_disk_set = set(on_disk)
    for f in ordered_filenames:
        validate_filename(f)
        if f not in on_disk_set:
            raise RulesIOError(f"unknown rule {f!r}")

    # Append files the user didn't include — keeps partial reorders safe.
    seen = set(ordered_filenames)
    rest = [f for f in on_disk if f not in seen]
    target_order = list(ordered_filenames) + rest

    # Build the desired (old → new) map, keeping the slug body but
    # rewriting the prefix to position+1.
    rename_map: dict[str, str] = {}
    for i, old in enumerate(target_order):
        m = _AUTO_FILENAME_RE.match(old)
        if m:
            slug_body = m.group(2)
            ext = old.rsplit(".", 1)[1]
        else:
            slug_body = slugify(old.rsplit(".", 1)[0])
            ext = old.rsplit(".", 1)[1] if "." in old else "yml"
        new_name = f"{i + 1:02d}_{slug_body}.{ext}"
        if new_name != old:
            rename_map[old] = new_name

    if not rename_map:
        return {}

    # Two-pass rename through .reorder.tmp suffixes to avoid clobber
    # races during in-place permutations.
    tmp_suffix = ".reorder.tmp"
    for old, new in rename_map.items():
        (rules_dir / old).rename(rules_dir / (new + tmp_suffix))
    for old, new in rename_map.items():
        (rules_dir / (new + tmp_suffix)).rename(rules_dir / new)

    return rename_map
