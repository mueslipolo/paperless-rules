"""Bootstrap a starter rule from one document's OCR text. Heuristic, no LLM."""

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
    "it": frozenset({"il", "la", "le", "di", "del", "della", "e", "un", "per", "con"}),
    "en": frozenset({"the", "and", "or", "of", "to", "in", "on", "at", "for", "with"}),
}

_LABEL_TO_NAME = [
    ("total à payer", "amount"), ("total a payer", "amount"),
    ("rechnungsbetrag", "amount"), ("total", "amount"), ("montant", "amount"),
    ("betrag", "amount"), ("tva", "vat"), ("mwst", "vat"),
    ("échéance", "due_date"), ("echeance", "due_date"), ("fälligkeit", "due_date"),
    ("date", "date"), ("datum", "date"),
    ("numéro de facture", "invoice_number"), ("rechnungsnummer", "invoice_number"),
    ("invoice number", "invoice_number"),
    ("numéro de client", "customer_number"), ("kundennummer", "customer_number"),
    ("référence", "reference"), ("reference", "reference"), ("referenz", "reference"),
]


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "field"


def _label_to_name(label: str) -> str:
    key = label.lower().strip()
    for needle, name in _LABEL_TO_NAME:
        if needle in key:
            return name
    return _slugify(key)


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


_ALL_STOPS = frozenset().union(*_LANG_STOPWORDS.values())


def _candidate_keywords(text: str, issuer: str) -> list[dict[str, Any]]:
    # Two strong candidates: the issuer's first non-suffix non-stopword word,
    # and any doc-type word found in the text.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    issuer_words = [w.strip("().,;:") for w in issuer.split()
                    if not _COMPANY_SUFFIX_RE.match(w)]
    for w in issuer_words:
        if (re.match(r"^[A-Za-zÀ-ÿ][\w'-]*$", w)
                and w.lower() not in _ALL_STOPS):
            out.append({"phrase": w, "score": 100.0, "suggested": True})
            seen.add(w)
            break
    for hint in _DOCTYPE_HINTS:
        m = re.search(rf"\b({hint})\b", text, re.IGNORECASE)
        if m and m.group(1) not in seen:
            phrase = m.group(1)
            out.append({"phrase": phrase, "score": 80.0, "suggested": len(out) < 2})
            seen.add(phrase)
            if len(out) >= 4:
                break
    return out


# Only emit fields whose canonical name is one we recognise — otherwise
# every CHF-line on a line-item invoice would crowd out the real "Total à
# payer" amount before it gets reached.
_AMOUNT_NAMES = frozenset({"amount", "vat", "subtotal", "amount_ht", "amount_ttc"})
_DATE_NAMES = frozenset({"date", "due_date", "period"})
_REF_NAMES = frozenset({"invoice_number", "customer_number", "reference"})


# Currency-prefix list is broad on purpose; users can add country-specific
# patterns directly in their rule's regex if they need anything more exotic.
_AMOUNT_RE = re.compile(
    r"(?:CHF|EUR|USD|GBP|JPY|CAD|AUD|Fr\.?)\s*([+\-]?\d[\d'’ʼ., ]*)|"
    r"([+\-]?\d[\d'’ʼ., ]*)\s*(?:CHF|EUR|USD|GBP|JPY|CAD|AUD|Fr\.?)"
)
_DATE_RE = re.compile(
    r"\b(\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b"
)
# International IBAN — country prefix + check + 11..30 alphanumeric chars.
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30})\b")
_REF_RE = re.compile(
    r"(?:Nr\.?|No\.?|Numéro|Ref\.?|Référence|Referenz)\s*[:\-]?\s*"
    r"([A-Z0-9][A-Z0-9\-/]{2,})", re.IGNORECASE,
)


def _label_before(line: str, pos: int) -> str:
    prefix = line[:pos].rstrip(":-> \t")
    tokens = re.findall(r"[A-Za-zÀ-ÿ'][A-Za-zÀ-ÿ'\.]*", prefix)
    return " ".join(tokens[-4:]) if tokens else ""


def _candidate_fields(text: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(name: str, ftype: str, value: str, label: str, suggested: bool) -> None:
        if name not in found:
            found[name] = {
                "name": name, "label": label, "sample_value": value,
                "regex_hint": "", "type": ftype, "suggested": suggested,
            }

    for m in _IBAN_RE.finditer(text):
        add("iban", "str", m.group(1), "IBAN", False)

    for line in text.split("\n"):
        for m in _AMOUNT_RE.finditer(line):
            value = m.group(1) or m.group(2)
            label = _label_before(line, m.start())
            if not value or not label:
                continue
            name = _label_to_name(label)
            if name not in _AMOUNT_NAMES:
                continue
            add(name, "float", value.strip(), label.strip(),
                suggested=(name == "amount"))
        for m in _DATE_RE.finditer(line):
            label = _label_before(line, m.start())
            if not label:
                continue
            name = _label_to_name(label)
            if name not in _DATE_NAMES:
                continue
            add(name, "date", m.group(1), label.strip(), suggested=True)
        for m in _REF_RE.finditer(line):
            label = _label_before(line, m.start()) or line[:m.start()].strip().rstrip(":")
            name = _label_to_name(label)
            if name not in _REF_NAMES:
                continue
            add(name, "str", m.group(1), label.strip(), suggested=False)

    return list(found.values())[:6]


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
    """Analyze OCR text and return a suggested rule skeleton (matches /api/bootstrap)."""
    text = unicodedata.normalize("NFC", text or "")
    issuer = _detect_issuer(text)
    return {
        "issuer": issuer,
        "language": _detect_language(text),
        "currency": _detect_currency(text),
        "keywords": _candidate_keywords(text, issuer),
        "fields": _candidate_fields(text),
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
    selected_keywords: list[str] | None = None,
    selected_fields: list[str] | None = None,
) -> str:
    """Render a YAML skeleton with empty regexes — the user fills them in the editor."""
    if selected_keywords is None:
        selected_keywords = [k["phrase"] for k in suggestion["keywords"] if k["suggested"]]
    if selected_fields is None:
        selected_fields = [f["name"] for f in suggestion["fields"] if f["suggested"]]

    fields_by_name = {f["name"]: f for f in suggestion["fields"]}
    required = []
    field_block = ""
    for fname in selected_fields:
        ftype = fields_by_name.get(fname, {}).get("type", "str")
        field_block += f"  {fname}:\n    regex: ''\n    type: {ftype}\n"
        if ftype in ("float", "date"):
            required.append(fname)

    kw_lines = "\n".join(f"  - {_quote_yaml(k)}" for k in selected_keywords) or "  []"
    req_lines = "\n".join(f"  - {r}" for r in required) or "  []"

    return (
        f"issuer: {_quote_yaml(suggestion.get('issuer') or '')}\n"
        f"keywords:\n{kw_lines}\n"
        "exclude_keywords: []\n"
        f"fields:\n{field_block}"
        f"required_fields:\n{req_lines}\n"
        "options:\n"
        f"  currency: {_quote_yaml(suggestion.get('currency') or 'CHF')}\n"
        "  date_formats:\n    - '%d.%m.%Y'\n"
        f"  languages:\n    - {suggestion.get('language') or 'fr'}\n"
    )
