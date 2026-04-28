"""Rule file I/O: list, read, save, rename, reorder, delete YAML rules.

Filenames are tightly constrained (``[A-Za-z0-9._-]+\\.ya?ml``, no slashes,
no ``..``) — that's the only adversarial input the editor exposes. Saves are
atomic (write-temp-then-rename).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]+\.ya?ml$")
# Auto-derived filenames carry a NN_ prefix that governs evaluation order
# (load_rules sorts by filename).
_AUTO_FILENAME_RE = re.compile(r"^(\d{2})_(.+)\.ya?ml$")


class RulesIOError(Exception):
    pass


def slugify(name: str) -> str:
    """ASCII-only lowercase slug; empty input falls back to ``rule``."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return s or "rule"


def auto_filename(name: str, rules_dir: Path, *, prefix: int | None = None) -> str:
    """Compose ``NN_slug.yml`` for ``name``. If ``prefix`` is None, picks the
    next free 2-digit prefix; slug collisions get a ``_2``/``_3``/… suffix."""
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
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    return sorted(p.name for p in rules_dir.iterdir() if p.suffix.lower() in (".yml", ".yaml"))


def validate_filename(filename: str) -> str:
    if not filename:
        raise RulesIOError("filename is empty")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise RulesIOError("filename must not contain path separators")
    if not _FILENAME_RE.match(filename):
        raise RulesIOError("filename must match [A-Za-z0-9._-]+.ya?ml")
    return filename


def list_rules(rules_dir: Path) -> list[dict[str, Any]]:
    """Per-rule summary {filename, name, match, field_count, enabled}.
    Unparseable files are silently skipped."""
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
    """Explicit `name:` wins; otherwise derive from the auto-filename slug."""
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    m = _AUTO_FILENAME_RE.match(filename)
    body = m.group(2) if m else filename.rsplit(".", 1)[0]
    return body.replace("_", " ").strip() or filename


def read_rule(rules_dir: Path, filename: str) -> str:
    filename = validate_filename(filename)
    path = Path(rules_dir) / filename
    if not path.is_file():
        raise RulesIOError(f"rule {filename!r} not found")
    return path.read_text(encoding="utf-8")


def write_rule(rules_dir: Path, filename: str, yaml_text: str) -> None:
    """Atomic write of validated YAML to ``<rules_dir>/<filename>``."""
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
    try:
        tmp.write_text(yaml_text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def delete_rule(rules_dir: Path, filename: str) -> bool:
    filename = validate_filename(filename)
    path = Path(rules_dir) / filename
    if path.is_file():
        path.unlink()
        return True
    return False


def rename_rule(rules_dir: Path, old_filename: str, new_filename: str) -> str:
    """Rename ``old_filename`` to ``new_filename`` and return the latter."""
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


_REORDER_TMP_SUFFIX = ".reorder.tmp"


def _recover_reorder_tmps(rules_dir: Path) -> None:
    """Promote any stranded ``*.reorder.tmp`` files to their final names.

    A previous call that crashed between the rename loops would leave tmps
    on disk; without recovery those rules would be invisible to the engine.
    Strip the suffix and rename — collisions are resolved by leaving the
    tmp in place (caller decides what to do)."""
    if not rules_dir.is_dir():
        return
    for p in rules_dir.iterdir():
        if not p.name.endswith(_REORDER_TMP_SUFFIX):
            continue
        target = rules_dir / p.name[: -len(_REORDER_TMP_SUFFIX)]
        if not target.exists():
            try:
                p.rename(target)
            except OSError:
                pass


def reorder_rules(rules_dir: Path, ordered_filenames: list[str]) -> dict[str, str]:
    """Renumber NN_ prefixes to match ``ordered_filenames``. Files not in
    the list keep their relative order, appended after. Returns the
    {old: new} map of files that moved on disk."""
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        raise RulesIOError(f"{rules_dir!r} is not a directory")

    _recover_reorder_tmps(rules_dir)
    on_disk = list_rule_filenames(rules_dir)
    on_disk_set = set(on_disk)
    for f in ordered_filenames:
        validate_filename(f)
        if f not in on_disk_set:
            raise RulesIOError(f"unknown rule {f!r}")

    seen = set(ordered_filenames)
    rest = [f for f in on_disk if f not in seen]
    target_order = list(ordered_filenames) + rest

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

    # Two-pass rename via temp suffixes — in-place swaps would otherwise
    # collide on the FS refusing to clobber. If we crash between the two
    # passes, _recover_reorder_tmps() at the start of the next call
    # promotes the stranded tmps to their final names.
    for old, new in rename_map.items():
        (rules_dir / old).rename(rules_dir / (new + _REORDER_TMP_SUFFIX))
    for new in rename_map.values():
        (rules_dir / (new + _REORDER_TMP_SUFFIX)).rename(rules_dir / new)

    return rename_map
