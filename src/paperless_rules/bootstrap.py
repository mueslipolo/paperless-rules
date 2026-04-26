"""Bootstrap a starter rule from one document. Heuristic, no LLM.

A rule describes a *kind of document*, not a sender. So bootstrap proposes
only what's generic: a starter `match` regex (a doctype hint found in the
text) and a sensible filename. Fields, exclude, and any sender-specific
narrowing are added by the user in the editor.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_DOCTYPE_HINTS = (
    "facture", "rechnung", "fattura", "invoice", "rappel", "mahnung",
    "reminder", "kontoauszug", "relevé", "statement",
)

_DOCTYPE_TO_LABEL = {
    "facture": "invoice", "rechnung": "invoice", "invoice": "invoice", "fattura": "invoice",
    "rappel": "reminder", "mahnung": "reminder", "reminder": "reminder",
    "kontoauszug": "statement", "relevé": "statement", "statement": "statement",
}

_LANG_STOPWORDS: dict[str, frozenset[str]] = {
    "fr": frozenset({"le", "la", "les", "de", "du", "des", "et", "à", "pour", "dans", "votre"}),
    "de": frozenset({"der", "die", "das", "und", "oder", "von", "zu", "den", "ist", "für"}),
    "it": frozenset({"il", "lo", "la", "i", "gli", "le", "di", "del", "della", "e", "un", "per", "con"}),
    "en": frozenset({"the", "and", "or", "of", "to", "in", "on", "at", "for", "with"}),
}


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


def _detect_doctype(text: str) -> str:
    for hint in _DOCTYPE_HINTS:
        m = re.search(rf"\b({hint})\b", text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _suggest_filename(text: str) -> str:
    hint = _detect_doctype(text).lower()
    label = _DOCTYPE_TO_LABEL.get(hint, "rule")
    return f"01_{label}.yml"


def bootstrap_from_text(text: str) -> dict[str, Any]:
    """Return a generic starter for the document's kind.

    Output: {match, exclude, filename_suggestion, language, currency}.
    The match seed is just the detected doctype word — the user makes it
    more specific in the editor (it'll usually need to be).
    """
    text = unicodedata.normalize("NFC", text or "")
    return {
        "match": _detect_doctype(text),
        "exclude": "",
        "filename_suggestion": _suggest_filename(text),
        "language": _detect_language(text),
        "currency": _detect_currency(text),
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
    exclude: str | None = None,
    filename: str | None = None,  # noqa: ARG001 — the editor uses this for its filename input
) -> str:
    """Render a YAML skeleton: match + exclude up top, empty fields below.

    The user populates `fields:` in the editor — reserved names
    (`correspondent`, `document_type`, `tags`, `title`) become paperless
    built-ins; everything else becomes a custom field of the same name.
    """
    if match is None:
        match = suggestion.get("match", "") or ""
    if exclude is None:
        exclude = suggestion.get("exclude", "") or ""
    return (
        f"# MATCH — single regex; rule fires when this matches the doc\n"
        f"match: {_quote_yaml(match)}\n"
        f"exclude: {_quote_yaml(exclude)}\n"
        f"# FIELDS — reserved names (correspondent, document_type, tags, title)\n"
        f"# write to paperless built-ins; anything else becomes a custom field.\n"
        f"# Forms: regex / value / template (with {{name}} placeholders).\n"
        f"fields: {{}}\n"
        f"required: []\n"
        f"options:\n"
        f"  currency: {_quote_yaml(suggestion.get('currency') or 'EUR')}\n"
        f"  date_formats:\n    - '%d.%m.%Y'\n"
        f"  languages:\n    - {suggestion.get('language') or 'fr'}\n"
    )
