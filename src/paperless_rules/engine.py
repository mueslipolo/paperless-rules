"""Rule engine: match documents and produce metadata. Pure function.

A rule has two top-level concerns:
  - `match` / `exclude` — does this rule apply to the document?
  - `fields` — a flat dict of named entries; each entry is a regex extraction,
    a constant value, or a template that combines other field values.

Reserved field names route to paperless built-ins (`correspondent`,
`document_type`, `tags`, `title`); any other name becomes a custom field.
`internal: true` on a field marks it as scratch — extracted/computed but
not published.

The engine evaluates fields in two passes per matched doc: first the
`regex:` and `value:` entries populate a name → value table, then
`template:` entries substitute `{name}` against that table. Templates can
reference other templates; cycles are detected and produce an error on
the offending field.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

Rule = dict[str, Any]
ExtractionResult = dict[str, Any]

# Per-rule diagnostic logger. Stays silent unless a rule opts in via
# top-level `trace: true` (or a caller explicitly passes trace=True).
# Configure once on app start: `logging.getLogger("paperless_rules.trace").setLevel(logging.INFO)`.
log_trace = logging.getLogger("paperless_rules.trace")

# Reserved field names that map to paperless built-in metadata. Anything
# else in `fields:` becomes a custom field of the same name.
RESERVED_FIELDS = ("correspondent", "document_type", "tags", "title", "created")

# Thousand-separator characters seen in real OCR output: ASCII apostrophe,
# typographic apostrophe, modifier letter apostrophe, NBSP. Stripped before
# float parsing.
_NOISE_RE = re.compile(r"[\s'’ʼ ]")

_BUILTIN_DATES = [
    "%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d/%m/%Y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d %B %Y", "%d. %B %Y", "%d %b %Y",
    # hyphen-separated abbreviated month: 13-Feb-2023, 23-Apr-2026
    "%d-%b-%Y", "%d-%B-%Y",
]

_FLOAT_HINTS = ("amount", "total", "price", "sum", "tva", "vat", "tax", "montant")
_DATE_HINTS = ("date", "due", "echeance", "échéance", "issued", "period", "fällig")

# Match `{name}` placeholders in templates.
_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _infer_type(name: str) -> str:
    n = name.lower()
    if any(h in n for h in _FLOAT_HINTS):
        return "float"
    if any(h in n for h in _DATE_HINTS):
        return "date"
    return "str"


def _coerce_float(raw: str) -> float | None:
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
        return None, "no value"
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


# ── field-spec normalisation ─────────────────────────────────────────


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


def _empty_result(ftype: str, internal: bool) -> dict[str, Any]:
    return {
        "ok": False, "value": None, "raw": None, "type": ftype,
        "kind": "regex", "internal": internal,
        "pattern": None, "groups": None, "error": None,
    }


def _spec_kind(spec: Any) -> str:
    """Priority: template > regex > value. When `value:` and `regex:` are
    both present the field is regex-mode with constant-on-match semantics."""
    if isinstance(spec, dict):
        if "template" in spec:
            return "template"
        if "regex" in spec:
            return "regex"
        if "value" in spec:
            return "value"
    return "regex"


# ── field evaluators ─────────────────────────────────────────────────


def _eval_regex_field(
    name: str, spec: Any, text: str, formats: list[str]
) -> dict[str, Any]:
    """The classic capture-group regex with optional transforms."""
    if isinstance(spec, str):
        patterns: list[str] = [spec]
        ftype = _infer_type(name)
        opts: dict[str, Any] = {}
        internal = False
    elif isinstance(spec, list):
        patterns = [str(p) for p in spec]
        ftype = _infer_type(name)
        opts = {}
        internal = False
    elif isinstance(spec, dict):
        regex = spec.get("regex")
        patterns = (
            [regex] if isinstance(regex, str)
            else [str(p) for p in regex] if isinstance(regex, list)
            else []
        )
        ftype = str(spec.get("type") or _infer_type(name))
        opts = {k: spec[k] for k in
                ("default", "match", "pick", "map", "aggregate", "combine")
                if k in spec}
        internal = bool(spec.get("internal", False))
    else:
        return _empty_result(_infer_type(name), False) | {"error": "invalid spec"}

    result = _empty_result(ftype, internal)

    # Mode: match (multi-arm)
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
                    error=coerce_err, ok=coerce_err is None,
                )
                break
        else:
            result["error"] = last_err or "no match"
        _apply_default(result, opts, ftype, formats)
        return result

    if not patterns:
        result["error"] = "no regex defined"
        _apply_default(result, opts, ftype, formats)
        return result

    # Mode: aggregate
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
            result.update(raw=str(len(captures)),
                          value=float(len(captures)) if ftype == "float" else len(captures),
                          ok=True)
        elif op in ("sum", "min", "max") and captures:
            nums = [n for n in (_coerce_float(c) for c in captures) if n is not None]
            if nums:
                agg = sum(nums) if op == "sum" else (min(nums) if op == "min" else max(nums))
                result.update(raw=str(agg), value=agg, ok=True)
            else:
                result["error"] = "no numeric matches"
        elif op in ("sum", "min", "max"):
            result["error"] = last_err or "no match"
        else:
            result["error"] = f"unknown aggregate: {op!r}"
        _apply_default(result, opts, ftype, formats)
        return result

    # Mode: combine
    if "combine" in opts:
        sep = str(opts["combine"])
        caps: list[str] = []
        first_pat: str | None = None
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
        return result

    # Mode: value-trigger (regex matches → set the constant from `value:`).
    # Only fires when `regex:` is present alongside `value:` — the value-only
    # form (no regex) goes to the value-mode evaluator.
    if isinstance(spec, dict) and "value" in spec:
        const = spec["value"]
        last_err = None
        for pat in patterns:
            m, err = _safe_search(pat, text)
            if err:
                last_err = err
                continue
            if m:
                value, coerce_err = _coerce(str(const), ftype, formats)
                result.update(
                    pattern=pat, raw=m.group(0),
                    value=value if coerce_err is None else const,
                    error=coerce_err, ok=coerce_err is None,
                )
                break
        else:
            result["error"] = last_err or "no match"
        _apply_default(result, opts, ftype, formats)
        return result

    # Default mode: capture-group extract, with optional pick + map
    pick = opts.get("pick", "first")
    all_matches: list[tuple[str, Any]] = []
    last_err = None
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
    return result


def _eval_value_field(
    name: str, spec: dict[str, Any], formats: list[str]
) -> dict[str, Any]:
    """Constant value, coerced by type. Lists pass through as-is (for tags)."""
    ftype = str(spec.get("type") or _infer_type(name))
    internal = bool(spec.get("internal", False))
    raw = spec["value"]
    if isinstance(raw, list):
        return {
            "ok": True, "value": list(raw), "raw": str(raw), "type": ftype,
            "kind": "value", "internal": internal,
            "pattern": None, "groups": None, "error": None,
        }
    if raw is None:
        return _empty_result(ftype, internal) | {"kind": "value", "error": "value is null"}
    value, err = _coerce(str(raw), ftype, formats)
    return {
        "ok": err is None,
        "value": value if err is None else raw,
        "raw": str(raw), "type": ftype,
        "kind": "value", "internal": internal,
        "pattern": None, "groups": None, "error": err,
    }


def _resolve_template(
    name: str,
    fields_spec: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    formats: list[str],
    visiting: set[str],
) -> dict[str, Any]:
    """Substitute `{name}` references against the populated fields table.
    Recursively resolves template-references-template; cycles → error."""
    spec = fields_spec.get(name)
    if not isinstance(spec, dict) or "template" not in spec:
        return _empty_result(_infer_type(name), False) | {"kind": "template", "error": "not a template"}
    if name in visiting:
        return _empty_result(str(spec.get("type") or _infer_type(name)), bool(spec.get("internal"))) | {
            "kind": "template", "error": "template cycle"
        }
    visiting.add(name)
    template = str(spec["template"])
    ftype = str(spec.get("type") or _infer_type(name))
    internal = bool(spec.get("internal", False))

    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        # Lazy-resolve a template-referenced template if it hasn't been computed yet.
        if ref in fields_spec and ref not in fields and isinstance(fields_spec[ref], dict) \
                and "template" in fields_spec[ref]:
            fields[ref] = _resolve_template(ref, fields_spec, fields, formats, visiting)
        f = fields.get(ref)
        if f and f.get("ok") and f["value"] is not None:
            return str(f["value"])
        return ""

    rendered = _TEMPLATE_RE.sub(sub, template)
    visiting.discard(name)
    value, err = _coerce(rendered, ftype, formats)
    return {
        "ok": err is None and bool(rendered),
        "value": value if err is None else rendered,
        "raw": rendered, "type": ftype,
        "kind": "template", "internal": internal,
        "pattern": None, "groups": None,
        "error": err if err else (None if rendered else "template rendered empty"),
    }


# ── transform helpers ────────────────────────────────────────────────


def _apply_map(value: Any, mapping: Any) -> Any:
    if not isinstance(mapping, dict):
        return value
    return mapping.get(value, value)


def _apply_default(
    result: dict[str, Any], opts: dict[str, Any], ftype: str, formats: list[str]
) -> None:
    if result["ok"] or "default" not in opts:
        return
    raw = str(opts["default"])
    value, err = _coerce(raw, ftype, formats)
    result.update(
        raw=raw,
        value=value if err is None else opts["default"],
        error=err, ok=err is None,
    )


# ── main entry ───────────────────────────────────────────────────────


def extract_with_rule(text: str, rule: Rule, *, trace: bool | None = None) -> ExtractionResult:
    """Match a document against the rule and produce a fields table.

    Returns:
      {
        'matched': bool,                 # match passed and exclude didn't fire
        'missing_match': [str],          # match patterns that didn't match
        'excluded_by': str | None,
        'fields': {                      # one entry per fields[<name>] in the rule
          name: { ok, value, raw, type, kind, internal, error, pattern?, groups? }
        },
        'required_ok': bool,             # matched and every `required` field is ok
        'trace': [str]?,                 # only when trace=True (or rule has `trace: true`)
      }

    Tracing: when enabled (explicit ``trace=True`` or top-level ``trace: true`` in
    the rule), every match/exclude/field outcome is appended to a per-call trace
    list AND emitted via the ``paperless_rules.trace`` logger. The editor's
    /api/test passes ``trace=True`` unconditionally so the SPA can render the
    trace inline; the runtime (poller, post-consume, backfill) honors only the
    rule-level flag, so noisy diagnostics stay scoped to the rules you're
    actively investigating.
    """
    if trace is None:
        trace = rule.get("trace") is True
    trace_lines: list[str] = [] if trace else []  # placeholder; see uses below

    rule_label = rule.get("name") or rule.get("match") or "<rule>"
    if trace:
        log_trace.info("─── %s — extract_with_rule ───", rule_label)

    text = unicodedata.normalize("NFC", text or "")

    # Match phase.
    match_spec = rule.get("match")
    match_patterns = (
        [match_spec] if isinstance(match_spec, str)
        else [str(p) for p in match_spec] if isinstance(match_spec, list)
        else []
    )
    match_patterns = [p for p in match_patterns if p]
    missing = [p for p in match_patterns
               if not re.search(p, text, re.MULTILINE | re.DOTALL)]

    # Exclude phase — empty patterns ignored (they'd match everything).
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

    if trace:
        for p in match_patterns:
            hit = p not in missing
            line = f"match {p!r} → {'HIT' if hit else 'MISS'}"
            trace_lines.append(line); log_trace.info(line)
        for e in excludes:
            fired = e == excluded_by
            line = f"exclude {e!r} → {'FIRED (rule disqualified)' if fired else 'no match'}"
            trace_lines.append(line); log_trace.info(line)
        verdict = "MATCHED" if matched else (
            f"EXCLUDED by {excluded_by!r}" if excluded_by else "no match"
        )
        line = f"verdict: {verdict}"
        trace_lines.append(line); log_trace.info(line)

    # Field evaluation — two passes.
    fields_spec = rule.get("fields") or {}
    options = rule.get("options") or {}
    formats = list(options.get("date_formats") or []) + _BUILTIN_DATES
    fields: dict[str, dict[str, Any]] = {}

    # Pass 1: regex extractions and constant values.
    for fname, fspec in fields_spec.items():
        kind = _spec_kind(fspec)
        if kind == "value":
            fields[fname] = _eval_value_field(fname, fspec, formats)
        elif kind == "regex":
            fields[fname] = _eval_regex_field(fname, fspec, text, formats)
        # templates deferred to pass 2

    # Pass 2: templates (lazy-resolves cross-template references). Use
    # setdefault so a result the recursion already stored (e.g. a cycle
    # error encountered while resolving a sibling) survives.
    for fname, fspec in fields_spec.items():
        if isinstance(fspec, dict) and "template" in fspec and fname not in fields:
            result = _resolve_template(fname, fields_spec, fields, formats, set())
            fields.setdefault(fname, result)

    # `required` — list of field names whose `ok` gates the rule firing.
    required = rule.get("required") or []
    required_ok = matched and all(fields.get(f, {}).get("ok") for f in required)

    if trace:
        for fname, fres in fields.items():
            sample = fres.get("value")
            if isinstance(sample, str) and len(sample) > 80:
                sample = sample[:77] + "…"
            err = fres.get("error")
            line = (
                f"field {fname!r} ({fres.get('kind')}, type={fres.get('type')}): "
                f"{'OK' if fres.get('ok') else 'FAIL'} value={sample!r}"
                + (f" error={err!r}" if err else "")
            )
            trace_lines.append(line); log_trace.info(line)
        if required:
            line = f"required {required} → {'OK' if required_ok else 'NOT MET'}"
            trace_lines.append(line); log_trace.info(line)

    result: ExtractionResult = {
        "matched": matched,
        "missing_match": missing,
        "excluded_by": excluded_by,
        "fields": fields,
        "required_ok": required_ok,
    }
    if trace:
        result["trace"] = trace_lines
    return result


def load_rules(rules_dir: Path) -> list[tuple[str, Rule]]:
    """Load all *.yml/*.yaml files from `rules_dir` as Rule mappings.

    Rules with explicit top-level ``enabled: false`` are skipped — useful
    for parking a half-baked rule without renaming it (which would break
    the editor URL, churn git history, and lose the file's place in the
    sort order). Default is enabled, so existing rules without the field
    keep working unchanged.
    """
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
        if not isinstance(data, dict):
            continue
        if data.get("enabled") is False:
            continue
        out.append((path.name, data))
    return out


def rules_dir_signature(rules_dir: Path) -> tuple[int, ...]:
    """Cheap fingerprint of every YAML rule file's mtime, used by the poller
    to detect rule changes without reloading on every iteration."""
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return ()
    return tuple(
        int(p.stat().st_mtime_ns)
        for p in sorted(rules_dir.iterdir())
        if p.suffix.lower() in (".yml", ".yaml")
    )


def find_matching_rule(
    text: str, rules: list[tuple[str, Rule]]
) -> tuple[str, ExtractionResult] | None:
    for filename, rule in rules:
        result = extract_with_rule(text, rule)
        if result["required_ok"]:
            return filename, result
    return None
