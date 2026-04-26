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


class RulesIOError(Exception):
    """Raised on filename validation, YAML parse, or filesystem failure."""


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
                "match": data.get("match", "") or "",
                "field_count": len(data.get("fields") or {}),
            }
        )
    return out


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
