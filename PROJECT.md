# paperless-rules — Project Specification

A paperless-ngx companion app that lets you build, test, and run rule-based document classification and extraction. You author rules interactively against your real OCR text in a web editor; a runtime applies those rules to incoming paperless documents and writes the extracted metadata back as paperless correspondents, document types, tags, and custom fields.

This document is the design specification. It is intended to be passed to a code generation agent (e.g. Claude Code) as the source of truth for what to build.

---

## 1. Context and motivation

The user runs paperless-ngx on a home server to manage Swiss family administrative documents — telecom bills (Swisscom, Sunrise, Salt), bank statements (UBS, PostFinance, Raiffeisen, ZKB), insurance (CSS, Helsana, SWICA), medical bills (Tarif 590 / LAMal forms), tax correspondence (cantonal Steueramt), payslips, contracts, and a long tail of one-off letters.

Three observations drive the design:

1. **Most documents come from a small, recurring set of senders.** ~70% of the household's documents come from <15 issuers. Their templates are stable for years at a time.
2. **Rule-based extraction beats LLMs for predictable templates** on every dimension: accuracy, speed, cost, determinism, auditability. Exact pattern matching is what regex is for.
3. **Documents that don't match any rule are not a problem.** Unmatched documents simply pass through paperless untouched — they're handled the way they always were (paperless's own auto-matching, manual triage, or just sitting in the inbox). No fallback layer, no flagging, no tagging.

This tool — **paperless-rules** — provides a tightly-scoped two-part system:

- **Editor**: a web UI for authoring and testing rules against real paperless OCR text.
- **Runtime**: a script that applies those rules to documents at consumption time, writing extracted metadata back to paperless.

Both ship in the same repo and the same Docker image, sharing a common rules folder and a common rule engine.

### Why not a paperless-ngx plugin?

paperless-ngx does not have a plugin system, by deliberate maintainer decision. The supported integration surface is the REST API. Tools like paperless-ai and paperless-gpt are external companion services that talk to paperless via API — that's the established pattern, and it's what we follow.

The companion-app approach has real advantages over a hypothetical plugin:
- Survives paperless-ngx upgrades (we pin to API version headers).
- Independent release cycle and tech stack.
- Easier to package, distribute, and contribute to.

We can still make the integration *feel* native by sharing a Docker Compose stack, using paperless API tokens for auth, writing extracted data to paperless custom fields (queryable in paperless's own UI), and following the same install pattern users already know from paperless-ai/paperless-gpt.

### Why not just use invoice2data?

invoice2data uses the right rule format (keywords, exclude_keywords, fields with regexes, required_fields, type-coercion options). However:

- The project is not actively maintained (two competing forks, slow release cadence, Python 2.7 baggage).
- The "invoice" framing leaks throughout the codebase (default required fields, line-item plugins, vocabulary).
- The actual extraction engine is small enough (~200 lines) that depending on a stale third-party project is a worse trade than reimplementing it.

**Decision: borrow invoice2data's YAML rule format, write our own engine.** Extend the format slightly to support setting `document_type` and `tags` on match — concepts paperless cares about that invoice2data doesn't model.

### Why not paperless-ngx's built-in matching rules?

paperless-ngx supports regex-based matching for tags / correspondents / document_types, but each rule is independent and only assigns one piece of metadata. There's no concept of "this regex identifies a document type, and *because* it matched, this bundle of other regexes runs to extract fields into custom fields." The two-stage classify-then-extract structure is the actual point of this project.

---

## 2. Components

```
┌───────────────────────────────────────────────────────────┐
│                   paperless-rules                         │
│                  (single Docker image)                    │
│                                                           │
│   ┌──────────────────┐         ┌──────────────────────┐   │
│   │   editor         │         │  runtime             │   │
│   │   (FastAPI +     │         │  (post-consume       │   │
│   │    web UI)       │         │   script / poller)   │   │
│   └────────┬─────────┘         └──────────┬───────────┘   │
│            │                              │               │
│            │  share engine + rules dir    │               │
│            ▼                              ▼               │
│   ┌──────────────────────────────────────────────┐        │
│   │  rules/                                      │        │
│   │   ├─ 01_swisscom_invoice.yml                 │        │
│   │   ├─ 02_swisscom_reminder.yml                │        │
│   │   ├─ 10_css_premium.yml                      │        │
│   │   └─ ...                                     │        │
│   └──────────────────────────────────────────────┘        │
└───────────────────────────────────────────────────────────┘
              │                              │
              │ REST API (read)              │ REST API (read+write)
              ▼                              ▼
                   ┌──────────────────┐
                   │  paperless-ngx   │
                   └──────────────────┘
```

### 2.1 Editor

Web UI for authoring and testing rules. Connects read-only to paperless. Lets the user:
- Browse and search paperless documents.
- View OCR text with whitespace visualization.
- Bootstrap a starter rule from a chosen document (issuer guess + candidate keywords + suggested fields).
- Edit a rule in YAML.
- Test the rule live against a corpus of selected paperless documents and see per-field results.
- Save rules as `.yml` files in the shared `rules/` folder.

### 2.2 Runtime

A pluggable component that applies the saved rules to documents in paperless. Two operating modes, configurable:

- **Post-consume script mode**: invoked by paperless via `PAPERLESS_POST_CONSUME_SCRIPT`. Receives the document ID via env var, reads the OCR text from the API, runs rules, PATCHes back metadata. Synchronous in paperless's pipeline.
- **Poller mode**: a long-running process that periodically queries paperless for recently-added documents (or documents bearing a specific trigger tag, optional) and processes them out-of-band. Useful when post-consume scripts feel risky or when reprocessing existing documents.

Both modes share the same rule-application logic.

### 2.3 Engine

Pure Python module, no I/O dependencies. Given a YAML rule (parsed) and a string of text, returns a structured extraction result. Imported by both the editor (for `/api/test`) and the runtime (for actual consumption).

### 2.4 Rules folder

Plain directory of YAML files. The editor writes; the runtime reads. Templates load in alphabetical filename order, so users prefix with `NN_` to control specificity (`01_swisscom_invoice.yml` before `99_generic_invoice.yml`).

---

## 3. Tech stack

- **Backend**: Python 3.11+, FastAPI, uvicorn, httpx (paperless API client), pyyaml.
- **Frontend**: vanilla HTML / CSS / JavaScript. No build step. No framework. Single `index.html`.
- **Storage**: flat YAML files on disk.
- **Config**: `.env` file (paperless URL + token, runtime mode, paths).
- **Packaging**: single Docker image, single `docker-compose.yml` snippet drop-in for an existing paperless stack.

Rationale: minimal dependencies, single-process simplicity, stack the user can read and modify themselves. No frontend build pipeline keeps the tool approachable.

---

## 4. Aesthetic and UX direction (editor)

This is a developer tool the user will stare at while debugging regex. Aesthetic should be **utilitarian / technical**, not editorial:

- Monospace-heavy. Information density over breathing room.
- Restrained palette: warm off-white background, dark ink, single accent color used **only** semantically (red = fail, green = pass, amber = partial). No purple gradients.
- Typography: a refined serif for headings (Iowan Old Style, Charter, or Palatino), JetBrains Mono / SF Mono / Menlo for everything else.
- Hand-rolled CSS with CSS variables for design tokens. No frameworks.
- Buttons are flat, uppercase, with letter-spacing. Closer to a terminal than a SaaS dashboard.

Not chaos — precision. The aesthetic supports the workflow.

---

## 5. Editor layout

The editor is a **regex playground first, YAML editor second**. The user spends their time iterating on patterns against a live multi-doc corpus with regex101-style feedback; the YAML rule file is the durable artifact, but it sits behind a tab. A working pattern is promoted into a YAML field with one click.

A single page with a top bar and three vertical panes.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  paperless·rules  [ rule picker ▼ ] [new] [bootstrap] [save] [del]   ●ok   │
├──────────────┬─────────────────────────────────┬───────────────────────────┤
│  CORPUS      │  Doc #42 — Swisscom Mar  ◀ ▶   │  PATTERN MATCHES          │
│  ─────────── │  ☐ show whitespace              │  ──────────────────────── │
│  [search…]   │  ─────────────────────────────  │  ┌─ #42 ────── 1/1 ─────┐ │
│              │  Swisscom (Suisse) SA           │  │ …Total à payer:      │ │
│  ☑ Doc #42   │  Postfach·····  3050·Bern       │  │   CHF [89.50]…       │ │
│  ☑ Doc #41   │  Total·à·payer:·CHF·[89.50]…   │  │ → 89.50 (float)      │ │
│  ☑ Doc #40   │  ─────────────────────────────  │  └──────────────────────┘ │
│  ☐ Doc #39   │  REGEX                ▼ hist   │  ┌─ #41 ────── 0/1 ─────┐ │
│              │  / Total à payer:\s*CHF…  /m    │  │ no match             │ │
│  PRESETS     │  flags  [✓]m  [ ]i  [ ]s        │  └──────────────────────┘ │
│  CHF amount  │  field [amount ▼]  type [float] │  ┌─ #40 ────── 1/1 ─────┐ │
│  DD.MM.YYYY  │  2/3 docs · 2 matches           │  │ …Total à payer:      │ │
│  Swiss IBAN  │  ── groups ──                   │  │   CHF [123.40]…      │ │
│  AHV         │  $0  full match                 │  │ → 123.40 (float)     │ │
│  GLN         │  $1  "89.50" → 89.50 (float)   │  └──────────────────────┘ │
│  Ref number  │  [apply to amount] [run rule]   │                           │
│              │  ─────────────────────────────  │                           │
│              │  ▶ YAML (01_swisscom.yml)       │                           │
└──────────────┴─────────────────────────────────┴───────────────────────────┘
```

### Pane 1 — Corpus + presets (left, ~280px)
- Search box (proxies to paperless `?query=`).
- Scrollable doc list (id, title, created). Checkbox to include in the **active corpus** — the set of docs the current pattern is tested against on every keystroke. Active doc gets accent-colored left border.
- **Swiss presets** sidebar at the bottom — one-click insertion of common patterns: `CHF amount`, `DD.MM.YYYY`, `Swiss IBAN`, `AHV` (756.xxxx.xxxx.xx), `GLN` (7601…), `reference number`. The same pattern library powers the bootstrap heuristics (section 6) — write it once, reuse it in both places.

### Pane 2 — OCR view + regex tester + YAML drawer (middle, flex)

**Top subpane — OCR view of the active document**
- Doc switcher arrows (`◀ ▶`) flip through corpus docs without leaving the tester.
- `show whitespace` toggle: spaces → `·`, tabs → `→`, newlines → `¶` via CSS pseudo-elements. Selectable text underneath is unaffected.
- **Live highlighting** of the current pattern's matches, inline. Capture groups get distinct background colors; the same colors echo in the match details panel below and in the right-pane result cards.
- Selecting text and clicking `promote to pattern` pre-fills the tester with a regex derived from the selection (literal escaping + sensible wildcards for digits/whitespace).

**Middle subpane — regex tester (the focal point)**
- Pattern input with `/…/` delimiter coloring.
- Flag toggles: `m` always on (locked — rules run with `re.MULTILINE`), `i`, `s`, `x` optional.
- Field assignment: dropdown to pick which field of the rule the pattern belongs to (`amount`, `date`, `invoice_number`, …) plus a `type` dropdown (`float` / `date` / `str`).
- Live corpus stats: `2/3 docs · 2 matches`, recomputed on every keystroke (debounced ~150ms).
- **Group breakdown**: `$0` full match, `$1`, `$2` … with per-match **coerced value preview** (`"89.50" → 89.50 (float)`, `"15.03.2024" → 2024-03-15 (date)`). The preview uses the same coercion code as the engine; the user sees the final value, not just the raw capture.
- **Per-field regex history (in-session versioning).** A `▼ hist` dropdown next to the pattern input keeps the last 10 patterns tried for the current `(rule, field)` pair, persisted client-side in `localStorage`. Each entry shows timestamp, flags, and most-recent corpus stats (`"v3 — 14:32 — 4/5 ✓"`). Click to restore; click 📌 to pin (pinned entries don't get evicted by the FIFO cap). Storage key: `paperless-rules:regex-history:<rule-filename>:<field-name>`. Cleared when the rule is deleted. Catches the "I had it working two minutes ago" mistake. *Scope note: this is in-session, client-side history only — durable file-level versioning (git-backed, cross-session) is out of scope for v1; see section 14.*
- Buttons: `apply to <field>` (writes the current pattern into the YAML at `fields.<field>.regex`); `run full rule` (switches the right pane to extraction-results mode).

**Bottom subpane — YAML drawer (collapsed by default)**
- Click `▶ YAML` to expand. Plain `<textarea>`, spellcheck off, tab preservation.
- Toolbar: filename input, valid-YAML indicator, line count, `save` button.
- The YAML is the source of truth on disk; the regex tester is a live editing surface for individual fields. Saves go through `POST /api/rules`.

### Pane 3 — Results (right, flex)

Two modes, switched by which button is pressed in the regex tester:

**Pattern mode** (default while editing a regex):
- Per-doc card: doc title + ID, match count (e.g. `1/1`), surrounding context for each match with capture groups colored to match the OCR highlights, coerced value preview.
- Color-coded left border: green if at least one match, grey if no match, red on regex error.

**Extraction mode** (after `run full rule`):
- Per-doc card: doc title + ID, overall status badge (`ok` / `partial` / `excluded` / `no match` / `error`), per-field rows with extracted value or error reason, optional detail line for missing keywords or `excluded_by`.
- Color-coded left border: green=ok, amber=partial, grey=unmatched, red=error.

### Top bar
- Brand: `paperless·rules`.
- Rule picker dropdown.
- Buttons: `new`, `bootstrap` (= bootstrap from currently-loaded document), `save`, `del`.
- Health status: `paperless: connected` (green) or error message (red).

### Bootstrap overlay

Triggered by `bootstrap` button (in top bar or OCR toolbar). Modal panel:

```
┌───────────────────────────────────────────────────────────┐
│  BOOTSTRAP RULE FROM DOCUMENT #42                         │
│  ─────────────────────────────────────────────────────    │
│                                                           │
│  Detected issuer:  [ Swisscom (Suisse) SA           ]     │
│                                                           │
│  Suggested keywords:                                      │
│    [✓] Swisscom                                           │
│    [✓] Facture                                            │
│    [ ] Période                                            │
│    [ ] CHF                                                │
│                                                           │
│  Suggested fields:                                        │
│    [✓] amount   → CHF 89.50           (float)             │
│    [✓] date     → 15.03.2024          (date)              │
│    [✓] inv_num  → 123456              (str)               │
│    [ ] period   → 15.02.2024 - 14.…   (str)               │
│                                                           │
│  Filename:  [ 01_swisscom_invoice.yml             ]       │
│                                                           │
│              [ cancel ]    [ generate rule ]              │
└───────────────────────────────────────────────────────────┘
```

User can toggle suggestions on/off, edit detected values inline, and pick a filename. Clicking `generate rule` produces a YAML skeleton in the editor (empty regexes for now — the user fills those in using the OCR view) and pre-loads the source document into the test corpus.

---

## 6. Bootstrap heuristics

The bootstrap feature is **fully heuristic, no LLM**. It scans the OCR text of one document and proposes a starter rule. Quality bar: "good enough that the user has fewer blank fields to fill in," not "complete and ready to ship."

### Issuer detection
- Take the first 5–10 non-empty lines.
- Pick the line that:
  - Looks like a company name (multiple capitalized words, possibly with `AG`, `SA`, `GmbH`, `Sàrl`, `Inc`, `Ltd`).
  - Is not a generic header word (`RECHNUNG`, `FACTURE`, `STATEMENT`, etc.).
  - Is not the user's own address (no detection — the user can correct).
- Fall back to "the longest line in the first 5 lines" if no clear match.

### Keyword candidates
Score multi-word phrases (2–4 words) in the document by:
- **Specificity**: rare-looking proper nouns and trademarked terms outrank common words. Boost any phrase containing the detected issuer name. Penalize phrases entirely composed of common stop-words across DE/FR/IT/EN.
- **Position**: phrases appearing in the top third of the document score higher.
- **Stability hint**: phrases like "Facture", "Rechnung", "Fattura", "Invoice" — the document type words — are good keywords. Pre-seed a small list per language.

Return top 4–6 candidates, with the first 2 pre-checked.

### Field candidates
Scan the OCR text for value patterns and pair them with their nearest preceding label:

- **Amounts**: `CHF\s*[\d'’\u02BC.,]+`, `EUR\s*…`, `[\d'’\u02BC.,]+\s*CHF`, etc. Detect currency.
- **Dates**: `\d{1,2}\.\d{1,2}\.\d{2,4}`, `\d{4}-\d{1,2}-\d{1,2}`, `\d{1,2}/\d{1,2}/\d{2,4}`.
- **Reference numbers**: `(?:Nr\.|No\.?|Numéro|Ref\.?|Référence)\s*[A-Z0-9-]{3,}`.
- **IBANs**: Swiss IBAN format `CH\d{2} ?\d{4} ?\d{4} ?\d{4} ?\d{4} ?\d{1,2}`.
- **GLN** (Tarif 590 medical): `7601\d{9}`.
- **AHV**: `756\.\d{4}\.\d{4}\.\d{2}`.

For each detected value, walk back on the same line for the closest "label-like" token (alphabetic word, possibly followed by `:` or `-`). Suggest a field name derived from the label, normalized (`Total à payer` → `amount`, `Facture Nr.` → `invoice_number`, `Date` → `date`, `Échéance` → `due_date`, etc., with a small alias dictionary covering DE/FR/IT/EN).

Return up to 6 detected fields. Pre-check those whose label is unambiguous (`amount`, `date`, `invoice_number`, `due_date`); leave others unchecked.

### Output
Generate a YAML skeleton:

```yaml
issuer: <detected issuer or empty>
keywords:
  - <selected keyword 1>
  - <selected keyword 2>
exclude_keywords: []
fields:
  amount:
    regex: ''            # user fills in
    type: float
  date:
    regex: ''
    type: date
  ...
required_fields:
  - amount
  - date
options:
  currency: <detected currency or CHF>
  date_formats:
    - '%d.%m.%Y'
  languages:
    - <detected dominant language>
```

Empty regex strings are intentional. The user uses the OCR text and the regex tester to fill them in. Bootstrap saves time on structure, not on the regex itself.

---

## 7. Backend API (editor)

All endpoints under `/api/`. JSON requests/responses unless noted.

### Documents (proxy to paperless)
- `GET /api/documents?query=&page=1&page_size=25` — `{count, results: [{id, title, created, correspondent, document_type, tags}]}`.
- `GET /api/documents/{doc_id}/text` — `{id, title, content, created}` where `content` is the OCR text.

### Rules
- `GET /api/rules` — `{rules: [{filename, issuer, keywords, field_count}]}`.
- `GET /api/rules/{filename}` — `{filename, yaml}` (raw YAML string).
- `POST /api/rules` — body `{filename, yaml}`. Validates YAML, writes to disk.
- `DELETE /api/rules/{filename}` — removes the file.

`filename` must be a bare `*.yml` (no slashes, no `..`).

### Engine
- `POST /api/test` — body `{yaml, doc_ids: [int]}`. Parses the YAML, fetches each document's OCR from paperless, runs the engine, returns per-document results.
- `POST /api/regex/test` — body `{pattern, flags?: str, doc_ids?: [int], text?: str, type?: "float"|"date"|"str", date_formats?: [str]}`. Either `doc_ids` or `text` must be provided. When `doc_ids` is given, the backend fetches each document's OCR from paperless and runs the pattern per-doc — used by the regex tester for live multi-doc highlighting on every keystroke. When `type` is given, the backend also runs the engine's coercion on each captured value and returns the final coerced result (so the editor can show `"89.50" → 89.50 (float)` previews using the same code path the runtime uses). Returns:
  ```json
  {
    "ok": true,
    "error": null,
    "results": [
      {
        "doc_id": 42,
        "source": "doc",
        "match_count": 1,
        "matches": [
          {"start": 247, "end": 273, "match": "Total à payer: CHF 89.50",
           "groups": ["89.50"], "coerced": 89.5}
        ]
      }
    ]
  }
  ```
  On a regex compile error, `ok: false` with `error` describing the problem and `results: []`.

### Bootstrap
- `POST /api/bootstrap` — body `{doc_id}`. Returns a suggested rule skeleton: `{issuer, keywords: [{phrase, score, suggested}], fields: [{name, label, sample_value, regex_hint, type, suggested}], language, currency, filename_suggestion}`. Frontend renders the overlay from this.

### Health
- `GET /api/health` — `{app, rules_dir, paperless}`. Used by the frontend on load.

---

## 8. Rule format

YAML, in the shared `rules/` folder. Borrowed from invoice2data, extended for paperless-native concepts.

```yaml
# What this rule sets on paperless when it matches:
issuer: Swisscom (Suisse) SA            # → paperless `correspondent` (created if missing)
document_type: Invoice                  # → paperless `document_type` (created if missing)
tags: [telecom, mobile, monthly]        # → paperless tags (created if missing, additive)

# What makes the rule fire:
keywords:                               # ALL must match (re.search, MULTILINE)
  - Swisscom
  - Facture

exclude_keywords:                       # ANY match disqualifies
  - Rappel
  - Mahnung

# What gets extracted into paperless custom fields:
fields:
  amount:
    regex: 'Total à payer:\s*CHF\s*([\d''.,]+)'
    type: float
  date:                                 # bare regex string; type inferred from name
    regex: 'du\s+(\d{1,2}\.\d{1,2}\.\d{4})'
  invoice_number:
    regex:                              # list = first match wins
      - 'Facture\s+Nr\.\s*(\d+)'
      - 'No\s+facture\s*(\d+)'
    type: str

required_fields:                        # rule fails if any are missing
  - amount
  - date

options:
  currency: CHF
  date_formats:
    - '%d.%m.%Y'
  languages: [fr]
```

### Field spec accepts three forms
- **Bare string** → single regex, type inferred from field name (`*amount*` / `*total*` / `*price*` → float; `*date*` → date; else str).
- **List of strings** → multiple regexes, first match wins.
- **Dict** with `regex` (string or list) and optional `type`.

### Type coercion
- `float`: handles Swiss number formats. Strip `[\s'’\u02BC\u00A0]`, then handle `,` vs `.` decimal separator (rightmost wins if both present). Returns `None` on parse failure.
- `date`: tries each `options.date_formats` entry, then a built-in fallback list (`%d.%m.%Y`, `%d-%m-%Y`, `%d/%m/%Y`, `%Y-%m-%d`, `%d %B %Y`, `%d. %B %Y`, etc.). Returns ISO `YYYY-MM-DD`.
- `str`: trim whitespace.

### Match semantics
A rule matches when:
1. ALL `keywords` match (`re.search`, `MULTILINE`).
2. NO `exclude_keywords` match.
3. Every `required_fields` entry extracted successfully.

If `required_fields` is omitted, every field declared in `fields` is treated as required.

### Rule loading order
Rules load in alphabetical filename order. The runtime tries rules in that order and **stops at the first one that fully matches** (including required fields). Convention: prefix filenames with `NN_` to control specificity.

### Custom-field naming
Field names in rules map to paperless custom fields with the same name. The runtime auto-creates missing custom fields on paperless (with appropriate types: `monetary` for float, `date` for date, `string` for str), or skips writing if creation fails. See section 10 for runtime behavior.

---

## 9. Engine spec

Pure function. No I/O. No paperless calls. Used by both editor (`/api/test`) and runtime.

```python
def extract_with_rule(text: str, rule: dict) -> dict:
    """
    Returns:
      {
        'matched': bool,            # all keywords match, no excludes
        'missing_keywords': [str],
        'excluded_by': str | None,
        'fields': {
            field_name: {
                'ok': bool,         # raw value found AND coerced successfully
                'raw': str | None,  # captured group as raw string
                'value': Any,       # coerced value (float, ISO date, str)
                'type': 'float' | 'date' | 'str',
                'pattern': str | None,
                'groups': [str] | None,
                'error': str | None,
            }, ...
        },
        'required_ok': bool,        # matched AND all required fields ok
      }
    """
```

Implementation notes:
- All regex evaluation uses `re.search` with `re.MULTILINE`.
- Multiple regex patterns per field iterate in declared order, first match wins.
- Unicode NFC normalization on input text before matching.
- Unknown YAML keys are ignored, not errors — keeps the format extensible.

Loader function:

```python
def load_rules(rules_dir: Path) -> list[tuple[str, dict]]:
    """
    Returns [(filename, parsed_rule_dict), ...] sorted alphabetically.
    Skips files that fail YAML parse (with a warning).
    """
```

Top-level runner used by the runtime:

```python
def find_matching_rule(
    text: str, rules: list[tuple[str, dict]]
) -> tuple[str, dict] | None:
    """
    Iterate rules in order, return (filename, extraction_result) for
    the first one whose extraction has required_ok=True. Return None
    if no rule matches.
    """
```

---

## 10. Runtime

### Mode 1: post-consume script

Invoked by paperless via `PAPERLESS_POST_CONSUME_SCRIPT`. Paperless passes document context via env vars:
- `DOCUMENT_ID` — paperless document ID (integer).
- (Other vars exist; we only need the ID.)

Script flow:

1. Read `DOCUMENT_ID`.
2. Fetch the document from paperless: `GET /api/documents/{id}/`. Need the OCR `content`.
3. Load rules from `RULES_DIR`.
4. Run `find_matching_rule(content, rules)`.
5. If no match: exit 0 silently. Document is left untouched.
6. If match:
   - Resolve / create the `correspondent` (paperless `GET/POST /api/correspondents/`). Cache by name in-memory for the run.
   - Resolve / create the `document_type` similarly.
   - Resolve / create each tag in `tags`. Existing tags on the doc are preserved; new ones are added.
   - For each extracted field with `ok=True`, resolve / create the matching custom field (paperless `GET/POST /api/custom_fields/`) with the appropriate data type. Cache.
   - PATCH the document: `PATCH /api/documents/{id}/` with `correspondent`, `document_type`, `tags`, and `custom_fields` payloads. The custom_fields payload format is documented in paperless API docs; use the field-id-keyed object form.
7. Exit 0.

Errors during PATCH are logged but do not fail the script (paperless treats post-consume failures as document-consumption failures, which is more drastic than warranted here). Log to stderr; paperless captures it.

### Mode 2: poller

A long-running process that periodically scans paperless for new documents and runs the same logic. Useful when the user doesn't want to set `PAPERLESS_POST_CONSUME_SCRIPT` (which is global), or wants to re-process existing documents.

Configuration:
- `POLL_INTERVAL_SECONDS` (default 60).
- `POLL_FILTER` (paperless query string, optional — e.g. `tag:needs-rules` if the user wants explicit opt-in, or `created__gte:<datetime>` for time-windowed). Default: documents added since process start.

Flow:
1. Every `POLL_INTERVAL_SECONDS`, query paperless for matching documents.
2. For each, check if it's already been processed (via a small local sqlite or json state file: `{doc_id, last_seen_modified, processed: bool}`). Skip if already processed and unchanged.
3. Otherwise apply the same logic as mode 1.

The state file is the only persistent state outside of the rules folder. Stored in `STATE_DIR` (default `./state/`).

### Mode selection

`RUNTIME_MODE=post_consume|poller|disabled`. Default `disabled` (editor-only deployment).

---

## 11. Project layout

```
paperless-rules/
├── pyproject.toml                 # or requirements.txt — pick one
├── README.md
├── docker-compose.example.yml     # drop-in addition to user's paperless stack
├── docker-compose.test.yml        # full stack for e2e tests (isolated)
├── Dockerfile
├── .env.example
├── .gitignore                     # ignores .env, state/, optionally rules/
│
├── src/
│   ├── paperless_rules/
│   │   ├── __init__.py
│   │   ├── engine.py              # rule engine — pure logic
│   │   ├── bootstrap.py           # heuristic rule generation
│   │   ├── paperless_client.py    # httpx wrapper for paperless API
│   │   ├── rules_io.py            # load/save rule YAML files
│   │   ├── editor/
│   │   │   ├── __init__.py
│   │   │   ├── app.py             # FastAPI app
│   │   │   └── static/
│   │   │       └── index.html     # the SPA
│   │   ├── runtime/
│   │   │   ├── __init__.py
│   │   │   ├── apply.py           # shared logic — match, resolve, PATCH
│   │   │   ├── post_consume.py    # entry point for post-consume mode
│   │   │   └── poller.py          # entry point for poller mode
│   │   └── cli.py                 # `paperless-rules <subcommand>`
│
├── rules/                         # default rules folder (mounted as volume)
│   └── .gitkeep
│
├── state/                         # poller state (mounted as volume)
│   └── .gitkeep
│
└── tests/
    ├── test_engine.py             # unit tests with realistic CH text fixtures
    ├── test_bootstrap.py
    ├── fixtures/
    │   ├── swisscom_invoice.txt
    │   ├── css_premium.txt
    │   ├── ubs_statement.txt
    │   └── tarif590_medical.txt
    └── e2e/
        ├── conftest.py            # compose orchestration + token minting
        ├── test_api.py            # FastAPI endpoints
        ├── test_runtime.py        # post-consume / poller / backfill
        ├── test_integration.py    # cross-component flows
        ├── rules/                 # rule YAMLs created during tests (gitignored)
        └── ui/
            └── test_editor.py     # Playwright UI flows
```

### CLI

`paperless-rules` is the single entry point with subcommands:

- `paperless-rules editor` — start the FastAPI editor on `$EDITOR_PORT` (default 8765).
- `paperless-rules post-consume` — apply rules to `$DOCUMENT_ID`. Used as the paperless post-consume script.
- `paperless-rules poller` — long-running poller process.
- `paperless-rules apply <doc_id>` — apply rules to one document on-demand (useful for testing or backfill).
- `paperless-rules backfill [--filter QUERY]` — apply rules to all documents matching a paperless query. For initial population after writing a new rule.

The Docker image's default entrypoint reads `RUNTIME_MODE` and runs the editor + the configured runtime mode in a small supervisor (e.g. `python -m paperless_rules.cli supervisor`). Single image, one container, both services.

---

## 12. Configuration

### `.env`
```
# Paperless connection
PAPERLESS_URL=http://paperless:8000
PAPERLESS_TOKEN=your_api_token_here

# Paths (defaults shown)
RULES_DIR=/data/rules
STATE_DIR=/data/state

# Editor
EDITOR_ENABLED=true
EDITOR_HOST=0.0.0.0
EDITOR_PORT=8765

# Runtime
RUNTIME_MODE=post_consume      # post_consume | poller | disabled
POLL_INTERVAL_SECONDS=60
POLL_FILTER=                   # optional paperless query, e.g. "tag:needs-rules"
```

### `docker-compose.example.yml`

Designed to drop into the user's existing paperless docker-compose alongside their `paperless` service:

```yaml
services:
  paperless-rules:
    image: paperless-rules:latest
    container_name: paperless-rules
    environment:
      PAPERLESS_URL: http://paperless:8000
      PAPERLESS_TOKEN: ${PAPERLESS_RULES_TOKEN}
      RULES_DIR: /data/rules
      STATE_DIR: /data/state
      RUNTIME_MODE: poller
    volumes:
      - ./paperless-rules/rules:/data/rules
      - ./paperless-rules/state:/data/state
    ports:
      - "8765:8765"
    depends_on:
      - paperless
    restart: unless-stopped
```

For post-consume mode, additionally mount the rules dir and state dir into the paperless container, and set `PAPERLESS_POST_CONSUME_SCRIPT=/data/post_consume.sh` in paperless's env (the shell script just exec's into our CLI). README documents this.

### Paperless API token
The user creates a dedicated token in paperless (Settings → API tokens). Read+write access required for the runtime; editor-only deployments can use a read-only token.

---

## 13. Testing

Four-tier suite. Tiers 1–2 catch logic bugs fast; tiers 3–4 catch integration bugs against real paperless. The test stack lives in `docker-compose.test.yml` — fully isolated from the user's production paperless (separate container names, volumes, host ports).

### Tier 1 — Engine unit tests (no Docker)
- `tests/test_engine.py`. Pure Python, fast (<1s for the suite).
- Plain `.txt` fixtures with realistic OCR artifacts: Swiss thousand-separator apostrophes (`1'234.50`), modifier letter apostrophe (U+02BC), non-breaking spaces (U+00A0), broken accented characters, numbers split across lines.
- Coverage: keyword and exclude_keywords matching (case-sensitive, multiline), type coercion (float/date/str) on Swiss formats, multiple regex per field (first match wins), required_fields semantics, unknown-keys-ignored, malformed YAML.

### Tier 2 — Bootstrap unit tests (no Docker)
- `tests/test_bootstrap.py`. Issuer detection, keyword scoring, field detection (amount, date, IBAN, AHV, GLN, reference number), language and currency detection, output schema stability.

### Tier 3 — Runtime + API integration tests (Docker, real paperless)
- `tests/e2e/` driven by pytest, orchestrated via `docker-compose.test.yml`.
- Stack per session: paperless-ngx + PostgreSQL + Redis + paperless-rules. No Tika/Gotenberg (we don't consume PDFs in tests).
- **Seeding strategy**: drop fixture `.txt` files into paperless's consume directory. Paperless ingests `.txt` files directly without running OCR, so `content` is deterministic and matches the engine fixtures byte-for-byte. Same fixtures power tiers 1 and 3 — no duplication.
- API token minted in a session-scoped pytest fixture via `docker compose exec test-paperless python manage.py shell` and shared with the rules service via a bind-mounted file.
- Scenarios:
  - Health: `GET /api/health` reflects paperless connectivity.
  - Document proxy: `GET /api/documents` paginates correctly.
  - Engine: `POST /api/test` runs a rule against N seeded docs.
  - Regex tester: `POST /api/regex/test` with `doc_ids` returns per-doc highlight ranges and coerced values.
  - Rules CRUD: write/read/delete; rejects path traversal in `filename`.
  - **Post-consume mode**: drop fixture, run CLI with `DOCUMENT_ID`, verify correspondent/document_type/tags/custom_fields are PATCHed.
  - **Poller mode**: enable poller, drop fixture, wait one cycle, verify metadata.
  - **Backfill**: `paperless-rules backfill --filter "..."` over pre-seeded docs.
  - **No-match path**: docs without a matching rule stay untouched (no tag, no log entry above debug level).
  - **Idempotency**: running the runtime twice on the same doc doesn't duplicate tags, doesn't revert manually-edited fields.
  - **Error paths**: invalid YAML on save, regex compile error in a rule, paperless unreachable mid-run, custom field type collision (existing field of different type).

### Tier 4 — UI end-to-end tests (Docker + Playwright)
- `tests/e2e/ui/` runs against the same compose stack.
- Editor flows:
  - Editor loads → `paperless: connected`.
  - Search corpus, click doc, OCR view loads, whitespace toggle works.
  - Type regex → live multi-doc highlighting + corpus stats update on every keystroke.
  - Capture-group colors are consistent between OCR pane and right-pane cards.
  - Per-field history (`▼ hist`): try three patterns, switch fields, come back, all three listed with correct stats. Pin one. Reload page. Pinned entry persists; oldest unpinned falls off when an 11th pattern is added.
  - `apply to amount` writes the pattern into the YAML drawer at `fields.amount.regex`.
  - `run full rule` switches right pane to extraction mode.
  - Bootstrap overlay accepts defaults and produces a YAML skeleton.
  - Save → rule appears in picker → reload → content matches what was saved.

### Test compose

`docker-compose.test.yml` brings up:

| Service | Image | Purpose |
|---|---|---|
| `test-redis` | `redis:7` | paperless task broker |
| `test-db` | `postgres:16` | paperless database |
| `test-paperless` | `ghcr.io/paperless-ngx/paperless-ngx:latest` | integration target |
| `test-paperless-rules` | built from local `Dockerfile` | system under test |

Host port mapping: `18000` → paperless, `18765` → rules editor (offset by `+10000` so they never collide with a developer's local production stack on `8000` / `8765`). Volumes are named with a `test-` prefix and pruned between sessions. Healthchecks gate dependent services. A small init step copies fixtures into paperless's consume volume and mints the API token before tests begin.

### CI
Tier 1+2 run on every push (~seconds, no Docker required). Tier 3+4 run on PRs and main branch (~3–5 minutes with the paperless-ngx image cached). CI provider-agnostic — anything that supports Docker Compose works (GitHub Actions, GitLab CI, Woodpecker, Drone).

---

## 14. Out-of-scope behaviors (do NOT implement)

Explicit non-list to prevent scope creep:

- ❌ **No LLM integration of any kind.** No "prepare for LLM" feature, no anonymization, no `secrets.yml`, no LLM fallback, no API key config for any LLM provider, no `needs-llm` tag. The tool is rule-based only.
- ❌ **No flagging of unmatched documents.** When no rule matches, the runtime does nothing — no tag, no custom field, no log entry beyond debug-level. Unmatched documents are normal.
- ❌ **No "smart" anonymization, regex-based PII detection, NER, or similar.** Removed entirely.
- ❌ **No similarity search across documents** (no Phase B inbox bootstrap). Bootstrap operates on one document at a time.
- ❌ **No iframe embedding into paperless.** Paperless has no plugin system; we don't try to fake one.
- ❌ **No database.** Flat YAML for rules, optional sqlite or JSON only for the poller's processed-doc state file. Nothing else.
- ❌ **No user accounts / auth / multi-tenancy.** Single user, local tool. The paperless API token is the only credential.
- ❌ **No live LLM-assisted regex generation in the editor** (heuristic bootstrap is in scope; LLM is not).
- ❌ **No build step / TypeScript / React / Tailwind.** Vanilla JS, vanilla CSS, single HTML file.
- ❌ **No automatic rule "graduation"** (LLM-handled docs becoming rules). There's no LLM layer to graduate from.
- ❌ **No paperless-ngx code modifications.** Companion app only.

---

## 15. Working with the user (Claude Code prompting hints)

The user is technical, prefers reading code over wading through abstractions, and is allergic to over-engineering. When generating this project:

- Prefer fewer, longer files over many small ones. A 600-line `engine.py` is fine. Splitting it into ten 60-line modules is not.
- Comment the *why*, not the *what*. Especially for surprising bits like Swiss number coercion (which Unicode codepoints to strip and why), date format precedence, or paperless API quirks.
- Choose the simpler option when in doubt. The user has consistently pushed back on additional automation.
- Implement the engine first. Verify it with realistic Swiss-document text fixtures (Swisscom invoice, CSS premium with `1'234.50` thousands separator, exclude_keywords on a Mahnung) before building the UI or runtime. The engine is load-bearing.
- The frontend is one HTML file. No bundler. CSS variables for tokens. Hand-written.
- Aesthetic is utilitarian/technical (section 4).
- **Write tests for the engine and bootstrap.** Realistic fixtures over toy strings. The test suite is what gives confidence the runtime won't silently corrupt metadata in production.
- The runtime should be conservative — log generously, never crash paperless's consumption pipeline, never overwrite metadata fields that already have values unless the rule explicitly opts in (TBD: probably a `overwrite: false` flag per rule, defaulting to "additive" semantics).

---

## 16. Definition of done

The user can:

1. `docker compose up` and have both editor (port 8765) and runtime running.
2. Open the editor, see "paperless: connected" in the top right.
3. Browse and search their paperless documents.
4. Open a document and view its OCR text with whitespace toggle.
5. Click `bootstrap from this doc`, see a suggested rule skeleton with detected issuer + keyword + field candidates, accept/reject suggestions, generate a YAML skeleton in the editor.
6. Edit the rule's regexes against the OCR text using the regex tester.
7. Add 2–5 documents to a test corpus.
8. Click `run test` and see per-document, per-field results.
9. Save the rule to `rules/01_swisscom_invoice.yml`.
10. Reopen the saved rule via the picker and continue editing.
11. Configure paperless's post-consume script (or enable poller mode) and have new incoming documents auto-matched and metadata-populated.
12. Run `paperless-rules backfill --filter "correspondent:Swisscom"` to apply a newly-written rule to existing documents.
13. Verify in paperless's own UI that custom fields, correspondent, document_type, and tags are all populated correctly.

If all thirteen of those work end-to-end, the project is done.

---

## 17. Future considerations (informational, do not build now)

These are not part of this project but the architecture should not preclude them:

- **Cluster-based bootstrap** (the original interpretation B): "find similar inbox docs and propose a rule from the cluster." Architecturally, this is just a richer bootstrap heuristic; the API surface stays the same.
- **LLM-assisted bootstrap or regex suggestions**: a future user might want to add an LLM call to improve regex suggestions. The bootstrap module's interface (text in, suggestion struct out) is stable enough that an LLM-backed implementation could swap in without disrupting the rest of the system.
- **Rule sharing**: anonymized rule library that users can pull from for common Swiss issuers. Just a folder of YAML files; no schema changes.
- **Metrics/observability**: how often each rule fires, which fields fail extraction most often, etc. Would inform which rules to refine.

None of these are in scope. They're listed only so the chosen architecture (engine as pure function, rules as flat YAML, runtime as a thin glue layer) doesn't accidentally close doors.