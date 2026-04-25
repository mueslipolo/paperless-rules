"""Rule engine: match keywords and extract fields from text. Pure function."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

Rule = dict[str, Any]
ExtractionResult = dict[str, Any]

# Thousand-separator characters seen in real OCR output: ASCII apostrophe,
# typographic apostrophe, modifier letter apostrophe (some OCR engines emit
# this in place of U+0027), and NBSP. Stripped before float parsing.
_NOISE_RE = re.compile(r"[\s'’ʼ ]")

_BUILTIN_DATES = [
    "%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d/%m/%Y",
    "%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d. %B %Y", "%d %b %Y",
]

_FLOAT_HINTS = ("amount", "total", "price", "sum", "tva", "vat", "tax", "montant")
_DATE_HINTS = ("date", "due", "echeance", "échéance", "issued", "period", "fällig")


def _infer_type(name: str) -> str:
    n = name.lower()
    if any(h in n for h in _FLOAT_HINTS):
        return "float"
    if any(h in n for h in _DATE_HINTS):
        return "date"
    return "str"


def _spec_parts(spec: Any, name: str) -> tuple[list[str], str, dict[str, Any]]:
    """Normalize a field-spec into (patterns, type, transform_opts).

    transform_opts may contain:
      `value`   — constant to assign when any pattern matches (regex acts
                  as a trigger; the captured text is ignored)
      `combine` — separator string; run every pattern and concatenate their
                  captures with this separator (instead of first-match-wins)
    """
    if isinstance(spec, str):
        return [spec], _infer_type(name), {}
    if isinstance(spec, list):
        return [str(p) for p in spec], _infer_type(name), {}
    if isinstance(spec, dict):
        regex = spec.get("regex")
        patterns = (
            [regex] if isinstance(regex, str)
            else [str(p) for p in regex] if isinstance(regex, list)
            else []
        )
        ftype = str(spec.get("type") or _infer_type(name))
        opts: dict[str, Any] = {}
        if "value" in spec:
            opts["value"] = spec["value"]
        if "combine" in spec:
            opts["combine"] = str(spec["combine"])
        return patterns, ftype, opts
    return [], _infer_type(name), {}


def _coerce_float(raw: str) -> float | None:
    # When both "." and "," appear, the rightmost is decimal and the other
    # is a thousand separator that gets stripped.
    s = _NOISE_RE.sub("", raw.strip())
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _coerce_date(raw: str, formats: list[str]) -> str | None:
    for fmt in formats:
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _coerce(raw: str | None, ftype: str, formats: list[str]) -> tuple[Any, str | None]:
    if raw is None:
        return None, "no match"
    if ftype == "float":
        v = _coerce_float(raw)
        return (v, None) if v is not None else (None, f"could not parse {raw!r} as float")
    if ftype == "date":
        v = _coerce_date(raw, formats)
        return (v, None) if v is not None else (None, f"could not parse {raw!r} as date")
    return raw.strip(), None


def coerce_value(raw: str, ftype: str, date_formats: list[str] | None = None) -> Any:
    """Public coercion — used by /api/regex/test so previews match runtime writes."""
    formats = (date_formats or []) + _BUILTIN_DATES
    value, _ = _coerce(raw, ftype, formats)
    return value


def extract_with_rule(text: str, rule: Rule) -> ExtractionResult:
    """Returns {matched, missing_keywords, excluded_by, fields, required_ok}."""
    text = unicodedata.normalize("NFC", text or "")

    keywords = rule.get("keywords") or []
    missing = [k for k in keywords if not re.search(k, text, re.MULTILINE)]
    excluded_by = next(
        (k for k in (rule.get("exclude_keywords") or [])
         if re.search(k, text, re.MULTILINE)),
        None,
    )
    matched = not missing and excluded_by is None

    formats = list((rule.get("options") or {}).get("date_formats") or []) + _BUILTIN_DATES
    fields_spec = rule.get("fields") or {}

    fields: dict[str, dict[str, Any]] = {}
    for fname, fspec in fields_spec.items():
        patterns, ftype, opts = _spec_parts(fspec, fname)
        result: dict[str, Any] = {
            "ok": False, "raw": None, "value": None, "type": ftype,
            "pattern": None, "groups": None, "error": None,
        }
        if not patterns:
            result["error"] = "no regex defined"
            fields[fname] = result
            continue

        last_err: str | None = None

        if "combine" in opts:
            # Run every pattern, concatenate captures with the separator.
            sep = opts["combine"]
            captures: list[str] = []
            first_pat = None
            for pat in patterns:
                try:
                    m = re.search(pat, text, re.MULTILINE)
                except re.error as e:
                    last_err = f"invalid regex {pat!r}: {e}"
                    continue
                if not m:
                    continue
                cap = m.group(1) if m.groups() else m.group(0)
                captures.append(cap)
                if first_pat is None:
                    first_pat = pat
            if captures:
                combined = sep.join(captures)
                value, err = _coerce(combined, ftype, formats)
                result.update(
                    pattern=first_pat, raw=combined, value=value, error=err,
                    ok=value is not None and err is None,
                )
            else:
                result["error"] = last_err or "no match"
            fields[fname] = result
            continue

        if "value" in opts:
            # Regex is a trigger; the field's value is the constant.
            for pat in patterns:
                try:
                    m = re.search(pat, text, re.MULTILINE)
                except re.error as e:
                    last_err = f"invalid regex {pat!r}: {e}"
                    continue
                if not m:
                    continue
                value, err = _coerce(str(opts["value"]), ftype, formats)
                result.update(
                    pattern=pat, raw=m.group(0),
                    value=value if err is None else opts["value"],
                    error=err, ok=err is None,
                )
                break
            else:
                result["error"] = last_err or "no match"
            fields[fname] = result
            continue

        # Default: first match wins, capture group becomes value.
        for pat in patterns:
            try:
                m = re.search(pat, text, re.MULTILINE)
            except re.error as e:
                last_err = f"invalid regex {pat!r}: {e}"
                continue
            if not m:
                continue
            raw = m.group(1) if m.groups() else m.group(0)
            value, err = _coerce(raw, ftype, formats)
            result.update(
                pattern=pat,
                groups=list(m.groups()) if m.groups() else None,
                raw=raw, value=value, error=err,
                ok=value is not None and err is None,
            )
            break
        else:
            result["error"] = last_err or "no match"
        fields[fname] = result

    required = rule.get("required_fields")
    if required is None:
        required = list(fields_spec.keys())
    required_ok = matched and all(fields.get(f, {}).get("ok") for f in required)

    return {
        "matched": matched,
        "missing_keywords": missing,
        "excluded_by": excluded_by,
        "fields": fields,
        "required_ok": required_ok,
    }


def load_rules(rules_dir: Path) -> list[tuple[str, Rule]]:
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    out = []
    for path in sorted(rules_dir.iterdir()):
        if path.suffix.lower() not in (".yml", ".yaml"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(data, dict):
            out.append((path.name, data))
    return out


def find_matching_rule(
    text: str, rules: list[tuple[str, Rule]]
) -> tuple[str, ExtractionResult] | None:
    for filename, rule in rules:
        result = extract_with_rule(text, rule)
        if result["required_ok"]:
            return filename, result
    return None
