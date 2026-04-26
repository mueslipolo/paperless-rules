"""Bootstrap a starter rule from one document. Heuristic, no LLM.

Produces three things and three only: the detected issuer, a single
discriminative match regex (issuer-word + doctype-hint joined with `.*?`),
and a sensible filename. Fields are added by the user in the editor —
the presets sidebar carries the common regex shapes (amount, date, IBAN…).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:AG|SA|S\.A\.|GmbH|S(?:à|a)rl|Inc|Ltd|LLC|Corp|SAS|SARL|SRL)\b",
    re.IGNORECASE,
)

_GENERIC_HEADERS = frozenset({
    "rechnung", "facture", "fattura", "invoice", "statement",
    "kontoauszug", "relevé", "quittung", "rappel", "mahnung",
})

_DOCTYPE_HINTS = (
    "facture", "rechnung", "fattura", "invoice", "rappel", "mahnung",
    "reminder", "kontoauszug", "relevé", "statement",
)

_LANG_STOPWORDS: dict[str, frozenset[str]] = {
    "fr": frozenset({"le", "la", "les", "de", "du", "des", "et", "à", "pour", "dans", "votre"}),
    "de": frozenset({"der", "die", "das", "und", "oder", "von", "zu", "den", "ist", "für"}),
    "it": frozenset({"il", "lo", "la", "i", "gli", "le", "di", "del", "della", "e", "un", "per", "con"}),
    "en": frozenset({"the", "and", "or", "of", "to", "in", "on", "at", "for", "with"}),
}
_ALL_STOPS = frozenset().union(*_LANG_STOPWORDS.values())


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "field"


def _detect_issuer(text: str) -> str:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()][:8]
    if not lines:
        return ""
    candidates = [ln for ln in lines if ln.lower() not in _GENERIC_HEADERS]
    for ln in candidates:
        if _COMPANY_SUFFIX_RE.search(ln):
            return ln
    for ln in candidates:
        words = ln.split()
        if sum(1 for w in words if w[:1].isupper()) >= 2:
            return ln
    return max(lines[:5], key=len) if lines else ""


def _detect_language(text: str) -> str:
    words = set(re.findall(r"[a-zà-ÿ]+", text.lower()))
    best, best_score = "fr", -1
    for lang in ("fr", "de", "it", "en"):
        score = len(words & _LANG_STOPWORDS[lang])
        if score > best_score:
            best, best_score = lang, score
    return best


def _detect_currency(text: str) -> str:
    m = re.search(r"\b(CHF|EUR|USD|GBP|JPY|CAD|AUD)\b", text)
    return m.group(1) if m else "EUR"


def _build_match_regex(text: str, issuer: str) -> str:
    """One discriminative regex: issuer's first non-stopword non-suffix word,
    joined to the first doctype hint found in the text via `.*?`. Engine runs
    `match` with re.DOTALL so the two anchors can be on different lines."""
    issuer_words = [w.strip("().,;:") for w in issuer.split()
                    if not _COMPANY_SUFFIX_RE.match(w)]
    issuer_word = next(
        (w for w in issuer_words
         if re.match(r"^[A-Za-zÀ-ÿ][\w'-]*$", w) and w.lower() not in _ALL_STOPS),
        None,
    )
    doctype_word = None
    for hint in _DOCTYPE_HINTS:
        m = re.search(rf"\b({hint})\b", text, re.IGNORECASE)
        if m:
            doctype_word = m.group(1)
            break
    if issuer_word and doctype_word:
        return f"{issuer_word}.*?{doctype_word}"
    return issuer_word or doctype_word or ""


def _suggest_filename(issuer: str, text: str) -> str:
    base = issuer.split("(")[0].strip() if issuer else "rule"
    slug = _slugify(base)
    slug = re.sub(r"_+(sa|ag|gmbh|sarl|inc|ltd|llc|corp)$", "", slug) or "rule"
    text_l = text.lower()
    for hint, label in [
        ("facture", "invoice"), ("rechnung", "invoice"), ("invoice", "invoice"),
        ("fattura", "invoice"),
        ("rappel", "reminder"), ("mahnung", "reminder"), ("reminder", "reminder"),
        ("kontoauszug", "statement"), ("relevé", "statement"), ("statement", "statement"),
    ]:
        if hint in text_l:
            return f"01_{slug}_{label}.yml"
    return f"01_{slug}_rule.yml"


def bootstrap_from_text(text: str) -> dict[str, Any]:
    """Analyze OCR text and return a minimal rule skeleton: issuer, match
    regex, filename, plus detected language and currency for the YAML
    `options` block. Fields are filled in by the user in the editor."""
    text = unicodedata.normalize("NFC", text or "")
    issuer = _detect_issuer(text)
    return {
        "issuer": issuer,
        "language": _detect_language(text),
        "currency": _detect_currency(text),
        "match": _build_match_regex(text, issuer),
        "filename_suggestion": _suggest_filename(issuer, text),
    }


def _quote_yaml(s: str) -> str:
    if s == "":
        return "''"
    if re.match(r"^[A-Za-z][A-Za-z0-9 _.\-/()]*$", s) and ":" not in s and "#" not in s:
        return s
    return "'" + s.replace("'", "''") + "'"


def render_yaml(
    suggestion: dict[str, Any],
    match: str | None = None,
    filename: str | None = None,  # noqa: ARG001
) -> str:
    """Render a YAML skeleton with the match regex set and an empty fields
    block. The user populates `fields:` in the editor — the presets sidebar
    gives them the regex shapes for amounts, dates, IBANs, etc."""
    if match is None:
        match = suggestion.get("match", "") or ""
    return (
        f"issuer: {_quote_yaml(suggestion.get('issuer') or '')}\n"
        f"# MATCH — single regex; rule fires when this matches the doc\n"
        f"match: {_quote_yaml(match)}\n"
        f"exclude: ''\n"
        f"# FIELDS — per-field regexes that extract paperless metadata\n"
        f"fields: {{}}\n"
        f"required_fields: []\n"
        f"options:\n"
        f"  currency: {_quote_yaml(suggestion.get('currency') or 'EUR')}\n"
        f"  date_formats:\n    - '%d.%m.%Y'\n"
        f"  languages:\n    - {suggestion.get('language') or 'fr'}\n"
    )
