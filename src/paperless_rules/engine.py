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


_TRANSFORM_KEYS = ("value", "combine", "match", "default", "pick", "map", "aggregate")


def _spec_parts(spec: Any, name: str) -> tuple[list[str], str, dict[str, Any]]:
    """Normalize a field-spec into (patterns, type, transform_opts).

    Transform keys (mode-selecting are mutually exclusive; precedence order
    `match > aggregate > combine > value > default-extract`):
      match     — list of {regex, value} alternatives; first arm wins
      aggregate — sum/count/min/max across all matches of all patterns
      combine   — concatenate captures of all patterns with a separator
      value     — set a constant when any pattern matches (regex is trigger)
      pick      — within default-extract, choose first|last|N match
      map       — lookup table applied to a captured value
      default   — fallback used when nothing else produced a value
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
        opts: dict[str, Any] = {k: spec[k] for k in _TRANSFORM_KEYS if k in spec}
        return patterns, ftype, opts
    return [], _infer_type(name), {}


def _safe_search(pattern: str, text: str) -> tuple[Any, str | None]:
    try:
        return re.compile(pattern, re.MULTILINE).search(text), None
    except re.error as e:
        return None, f"invalid regex {pattern!r}: {e}"


def _safe_finditer(pattern: str, text: str) -> tuple[list[Any], str | None]:
    try:
        return list(re.compile(pattern, re.MULTILINE).finditer(text)), None
    except re.error as e:
        return [], f"invalid regex {pattern!r}: {e}"


def _apply_map(value: Any, mapping: Any) -> Any:
    """Look up a value in a mapping; if present return the mapped result, else
    return the original. Non-dict mappings are silently ignored."""
    if not isinstance(mapping, dict):
        return value
    return mapping.get(value, value)


def _apply_default(
    result: dict[str, Any], opts: dict[str, Any], ftype: str, formats: list[str]
) -> None:
    """Set the field value to opts['default'] if extraction did not succeed."""
    if result["ok"] or "default" not in opts:
        return
    raw = str(opts["default"])
    value, err = _coerce(raw, ftype, formats)
    result.update(
        raw=raw,
        value=value if err is None else opts["default"],
        error=err,
        ok=err is None,
    )


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
    """Returns {matched, missing_keywords, excluded_by, fields, required_ok}.

    Match phase: `match` is a single regex (or list — all must match), run
    with re.MULTILINE | re.DOTALL so `.` spans lines. `exclude` disqualifies
    the rule when it matches. Empty patterns are skipped (an empty regex
    would otherwise match everything, never the user's intent).
    """
    text = unicodedata.normalize("NFC", text or "")

    match_spec = rule.get("match")
    match_patterns = (
        [match_spec] if isinstance(match_spec, str)
        else [str(p) for p in match_spec] if isinstance(match_spec, list)
        else []
    )
    match_patterns = [p for p in match_patterns if p]
    missing = [p for p in match_patterns
               if not re.search(p, text, re.MULTILINE | re.DOTALL)]

    exclude_spec = rule.get("exclude")
    excludes = (
        [exclude_spec] if isinstance(exclude_spec, str)
        else [str(p) for p in exclude_spec] if isinstance(exclude_spec, list)
        else []
    )
    excludes = [e for e in excludes if e]
    excluded_by = next(
        (e for e in excludes if re.search(e, text, re.MULTILINE | re.DOTALL)),
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

        # ── mode: match (highest precedence) ─────────────────────────
        if "match" in opts:
            last_err: str | None = None
            for entry in opts["match"] or []:
                if not isinstance(entry, dict):
                    continue
                pat = entry.get("regex")
                if not pat:
                    continue
                m, err = _safe_search(str(pat), text)
                if err:
                    last_err = err
                    continue
                if m:
                    const = entry.get("value", "")
                    value, coerce_err = _coerce(str(const), ftype, formats)
                    result.update(
                        pattern=str(pat),
                        groups=list(m.groups()) if m.groups() else None,
                        raw=m.group(0),
                        value=value if coerce_err is None else const,
                        error=coerce_err,
                        ok=coerce_err is None,
                    )
                    break
            else:
                result["error"] = last_err or "no match"
            _apply_default(result, opts, ftype, formats)
            fields[fname] = result
            continue

        if not patterns:
            result["error"] = "no regex defined"
            _apply_default(result, opts, ftype, formats)
            fields[fname] = result
            continue

        # ── mode: aggregate ──────────────────────────────────────────
        if "aggregate" in opts:
            op = str(opts["aggregate"])
            captures: list[str] = []
            last_err = None
            for pat in patterns:
                ms, err = _safe_finditer(pat, text)
                if err:
                    last_err = err
                    continue
                for m in ms:
                    captures.append(m.group(1) if m.groups() else m.group(0))
            if op == "count":
                # count always succeeds (0 is a valid result, not an error)
                value, err = _coerce(str(len(captures)), ftype, formats)
                result.update(
                    raw=str(len(captures)),
                    value=value if err is None else len(captures),
                    error=err, ok=err is None,
                )
            elif op in ("sum", "min", "max") and captures:
                nums = [n for n in (_coerce_float(c) for c in captures) if n is not None]
                if nums:
                    agg = sum(nums) if op == "sum" else (min(nums) if op == "min" else max(nums))
                    value, err = _coerce(str(agg), ftype, formats)
                    result.update(
                        raw=str(agg),
                        value=value if err is None else agg,
                        error=err, ok=err is None,
                    )
                else:
                    result["error"] = "no numeric matches"
            elif op in ("sum", "min", "max"):
                result["error"] = last_err or "no match"
            else:
                result["error"] = f"unknown aggregate: {op!r}"
            _apply_default(result, opts, ftype, formats)
            fields[fname] = result
            continue

        # ── mode: combine ────────────────────────────────────────────
        if "combine" in opts:
            sep = str(opts["combine"])
            caps: list[str] = []
            first_pat = None
            last_err = None
            for pat in patterns:
                m, err = _safe_search(pat, text)
                if err:
                    last_err = err
                    continue
                if m:
                    cap = m.group(1) if m.groups() else m.group(0)
                    cap = _apply_map(cap, opts.get("map"))
                    caps.append(str(cap))
                    if first_pat is None:
                        first_pat = pat
            if caps:
                combined = sep.join(caps)
                value, err = _coerce(combined, ftype, formats)
                result.update(
                    pattern=first_pat, raw=combined, value=value, error=err,
                    ok=value is not None and err is None,
                )
            else:
                result["error"] = last_err or "no match"
            _apply_default(result, opts, ftype, formats)
            fields[fname] = result
            continue

        # ── mode: value (constant on match) ──────────────────────────
        if "value" in opts:
            last_err = None
            for pat in patterns:
                m, err = _safe_search(pat, text)
                if err:
                    last_err = err
                    continue
                if m:
                    value, coerce_err = _coerce(str(opts["value"]), ftype, formats)
                    result.update(
                        pattern=pat, raw=m.group(0),
                        value=value if coerce_err is None else opts["value"],
                        error=coerce_err, ok=coerce_err is None,
                    )
                    break
            else:
                result["error"] = last_err or "no match"
            _apply_default(result, opts, ftype, formats)
            fields[fname] = result
            continue

        # ── default extract mode (with optional pick + map) ──────────
        pick = opts.get("pick", "first")
        all_matches: list[tuple[str, Any]] = []
        last_err = None
        # `pick=first` short-circuits at the first matching pattern; other
        # picks need every match across every pattern, sorted by position.
        if pick == "first":
            for pat in patterns:
                m, err = _safe_search(pat, text)
                if err:
                    last_err = err
                    continue
                if m:
                    all_matches.append((pat, m))
                    break
        else:
            for pat in patterns:
                ms, err = _safe_finditer(pat, text)
                if err:
                    last_err = err
                    continue
                for m in ms:
                    all_matches.append((pat, m))
            all_matches.sort(key=lambda pm: pm[1].start())

        chosen: tuple[str, Any] | None = None
        if all_matches:
            if pick == "first":
                chosen = all_matches[0]
            elif pick == "last":
                chosen = all_matches[-1]
            elif isinstance(pick, int):
                idx = pick if pick >= 0 else len(all_matches) + pick
                if 0 <= idx < len(all_matches):
                    chosen = all_matches[idx]
                else:
                    result["error"] = f"pick index {pick} out of range"
            else:
                result["error"] = f"unknown pick: {pick!r}"

        if chosen is not None:
            chosen_pat, m = chosen
            cap = m.group(1) if m.groups() else m.group(0)
            cap = _apply_map(cap, opts.get("map"))
            value, err = _coerce(str(cap), ftype, formats)
            result.update(
                pattern=chosen_pat,
                groups=list(m.groups()) if m.groups() else None,
                raw=str(cap), value=value, error=err,
                ok=value is not None and err is None,
            )
        elif result["error"] is None:
            result["error"] = last_err or "no match"
        _apply_default(result, opts, ftype, formats)
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
